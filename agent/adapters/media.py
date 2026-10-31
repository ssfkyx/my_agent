"""media — llama-server 的媒体感知实现（MediaPort：ASR + VL）。

Module: agent.adapters.media
Responsibility: 以 OpenAI 兼容的多模态消息把音频/图片送进已驻留的 asr/vl 模型，
    并在缺少 mmproj 时立即失败（MISSING_MMPROJ），避免静默退化。
Depends on: stdlib (base64, json, urllib, pathlib, time) + agent.ports + agent.config.ModelProfile
Depended by: app（装配）, tools.perception
ADRs: ADR-020（感知工具与 mmproj 门禁）, ADR-002（单常驻）, ADR-004（不流式）, ADR-003（FP64 计时）,
    ADR-036（Web 多模态附件上传 / ASR 输出归一化）, ADR-042（可选媒体模型）
"""

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from agent.adapters.llama_cpp import ServerSupervisor
from agent.config import ModelProfile
from agent.core.protocol import ErrorCode
from agent.ports import MediaError, MediaResult

_TRANSCRIBE_PROMPT = "Transcribe the speech in this audio verbatim. Output only the transcript."
_IMAGE_PROMPT = "Describe what you see in this image factually and briefly."

_MIME_BY_SUFFIX: Mapping[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}


class LlamaCppMedia:
    """ASR 与 VL 的共用实现（ADR-020）。

    前置条件：profiles 含 asr_alias 与 vl_alias；两者都必须配置 mmproj。
    后置条件：每次调用前 ensure 对应模型驻留；返回 FP64 计时。
    禁止事项：禁止同时拉起 asr 与 vl（显存只容一个，见 ADR-002）。
    """

    def __init__(
        self,
        *,
        supervisor: ServerSupervisor,
        profiles: Mapping[str, ModelProfile],
        asr_alias: str,
        vl_alias: str,
        timeout_s: float,
        max_audio_bytes: int,
        max_image_bytes: int,
    ) -> None:
        self._supervisor = supervisor
        self._profiles = dict(profiles)
        self._asr_alias = asr_alias
        self._vl_alias = vl_alias
        self._timeout_s = timeout_s
        self._max_audio_bytes = max_audio_bytes
        self._max_image_bytes = max_image_bytes

    def transcribe(self, path: Path) -> MediaResult:
        """把音频转成文本。

        前置条件：path 为工作区内文件。
        后置条件：返回转写文本；空音频得到空串而不是异常。
        禁止事项：不要在缺 mmproj 时退回文本模型。
        异常：MediaError（MISSING_MMPROJ/BAD_ARGS/UPSTREAM_ERROR）。
        """
        return self._call(alias=self._asr_alias, path=path, prompt=_TRANSCRIBE_PROMPT, kind="audio")

    def describe_image(self, path: Path, prompt: str) -> MediaResult:
        """描述图片。

        前置条件：prompt 非空。
        后置条件：返回模型描述。
        禁止事项：不要把 prompt 当"问题"重复拼接（一次调用只问一次）。
        异常：MediaError、OSError。
        """
        return self._call(alias=self._vl_alias, path=path, prompt=prompt.strip() or _IMAGE_PROMPT, kind="image")

    def _call(self, *, alias: str, path: Path, prompt: str, kind: str) -> MediaResult:
        """统一的"读文件 → 组多模态消息 → POST → 解析文本"流程。

        前置条件：kind ∈ {"audio","image"}。
        后置条件：返回 MediaResult；image 的 text 非空，audio 允许空串（静音）。
        禁止事项：不要在这里做重试（重试属于预算层）。
        异常：MediaError、OSError。
        """
        profile = self._profile(alias)
        self._require_mmproj(profile)

        limit = self._max_audio_bytes if kind == "audio" else self._max_image_bytes
        payload = _read_limited(path, limit)
        mime = _mime_for(path, kind)

        if kind == "audio":
            part = {
                "type": "input_audio",
                "input_audio": {"data": base64.b64encode(payload).decode("ascii"), "format": path.suffix.lstrip(".")},
            }
        else:
            part = {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"},
            }

        base_url = self._supervisor.ensure(alias)
        body = {
            "model": alias,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, part]}],
            "temperature": 0.0,
            "max_tokens": 512,
            "stream": False,
        }
        started = time.perf_counter()
        raw = self._post(base_url, body)
        elapsed = time.perf_counter() - started
        text = _normalise_asr_text(raw) if kind == "audio" else raw
        if kind == "image" and not text:
            raise MediaError(ErrorCode.UPSTREAM_ERROR, "media response content is empty")
        return MediaResult(text=text, model_alias=alias, elapsed_s=elapsed)

    def _profile(self, alias: str) -> ModelProfile:
        """取模型档案。

        前置条件：alias 已在配置中登记。
        后置条件：返回对应 profile。
        禁止事项：不要在缺失时构造默认 profile（会掩盖配置错误）。
        异常：MediaError（BAD_ARGS，未知 alias）。
        """
        profile = self._profiles.get(alias)
        if profile is None:
            raise MediaError(ErrorCode.BAD_ARGS, f"media alias {alias!r} is not configured")
        return profile

    def _require_mmproj(self, profile: ModelProfile) -> None:
        """校验 mmproj 存在（硬门禁）。

        前置条件：profile 来自配置。
        后置条件：有可用 mmproj 时返回；否则抛 MISSING_MMPROJ。
        禁止事项：不要把它降级成警告（静默退化的后果比报错严重得多）。
        异常：MediaError（MISSING_MMPROJ）。
        """
        if profile.mmproj is None:
            raise MediaError(ErrorCode.MISSING_MMPROJ, f"models.{profile.alias} has no mmproj configured")
        if not profile.mmproj.is_file():
            raise MediaError(ErrorCode.MISSING_MMPROJ, f"mmproj not found: {profile.mmproj}")

    def _post(self, base_url: str, body: Mapping[str, Any]) -> str:
        """发送多模态请求并抽取文本。

        前置条件：body 可序列化。
        后置条件：返回非空文本（已 strip）。
        禁止事项：不要在错误信息里包含 base64 数据。
        异常：MediaError（HTTP 非 2xx、网络错误、空内容）。
        """
        request = urllib.request.Request(
            f"{base_url}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise MediaError(ErrorCode.UPSTREAM_ERROR, f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MediaError(ErrorCode.UPSTREAM_ERROR, f"cannot reach {base_url}: {exc}", retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise MediaError(ErrorCode.UPSTREAM_ERROR, "media response is not JSON") from exc

        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            raise MediaError(ErrorCode.UPSTREAM_ERROR, "media response has no choices")
        return _content_text((choices[0].get("message") or {}).get("content"))


def _content_text(content: Any) -> str:
    """把 OpenAI 的 content 字段压成纯文本。

    前置条件：content 可为字符串、parts 列表或 None。
    后置条件：返回拼接后的文本（已 strip）。
    禁止事项：不要假设一定是字符串（多模态响应常为 parts 列表）。
    异常：无。
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts).strip()
    return ""


def _normalise_asr_text(text: str) -> str:
    """剥离 Qwen3-ASR 的 `<asr_text>` 前缀，只保留转写正文。

    前置条件：text 为模型原始 content。
    后置条件：有 marker 时返回 marker 之后的文本；无 marker 时返回 strip 后原文。
    禁止事项：不要把 `language None` 之类元数据当转写结果。
    异常：无。
    """
    marker = "<asr_text>"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return text.strip()


def _read_limited(path: Path, limit: int) -> bytes:
    """读取文件并在超限时拒绝。

    前置条件：limit > 0。
    后置条件：返回完整字节；超限抛错且不读入内存。
    禁止事项：不要在超限后"截断发送"（截断的媒体等于坏数据）。
    异常：MediaError（BAD_ARGS/NOT_FOUND）、OSError。
    """
    if not path.is_file():
        raise MediaError(ErrorCode.NOT_FOUND, f"media file not found: {path}")
    size = path.stat().st_size
    if size > limit:
        raise MediaError(ErrorCode.BAD_ARGS, f"media file too large: {size} bytes > {limit}")
    return path.read_bytes()


def _mime_for(path: Path, kind: str) -> str:
    """按后缀推断 MIME 类型。

    前置条件：kind ∈ {"audio","image"}。
    后置条件：返回 MIME 字符串。
    禁止事项：不要对未知后缀猜一个默认值（会让上游给出难以诊断的错误）。
    异常：MediaError（BAD_ARGS，未知或类型不符）。
    """
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower())
    if mime is None:
        raise MediaError(ErrorCode.BAD_ARGS, f"unsupported media suffix: {path.suffix!r}")
    if not mime.startswith(kind):
        raise MediaError(ErrorCode.BAD_ARGS, f"{path.name} is not {kind} ({mime})")
    return mime
