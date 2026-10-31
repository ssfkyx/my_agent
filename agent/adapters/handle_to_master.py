"""handle_to_master — 场外 SOTA 模型适配器（OpenAI 兼容 HTTP）。

Module: agent.adapters.handle_to_master
Responsibility: 实现 MasterPort：从环境变量读取端点/密钥/模型名，把已脱敏的 payload
    作为一次 chat/completions 请求发出，并返回纯文本答案。
Depends on: stdlib (json, os, urllib) + agent.ports
Depended by: agent.app
ADRs: ADR-021（未配置即失败、绝不内置默认地址）, ADR-022（只接收已脱敏 payload）
"""

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping

from agent.ports import EscalationError

_ENV_BASE_URL = "HANDLE_TO_MASTER_BASE_URL"
_ENV_API_KEY = "HANDLE_TO_MASTER_API_KEY"
_ENV_MODEL = "HANDLE_TO_MASTER_MODEL"

_SYSTEM_PROMPT = """You are the external master model called by a local agent after its own tool
attempts failed. You receive the user's original request and a redacted failure summary.
Answer the user's request directly and concretely. Do not ask for secrets, tokens or local paths;
they have been intentionally removed. If the request cannot be answered from the given context,
say exactly what information is missing."""


class HttpMaster:
    """通过 OpenAI 兼容接口调用场外模型（实现 MasterPort）。

    前置条件：base_url/api_key/model 非空；timeout_s > 0；max_tokens >= 1。
    后置条件：complete 只发一次请求；成功返回非空文本。
    禁止事项：不要内置任何默认远端地址；不要把 API key 写进日志或异常信息。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = 120.0,
        max_tokens: int = 1024,
    ) -> None:
        # 环境变量只在构造时读取：运行中改环境不会让同一进程悄悄换端点。
        self._base_url = (base_url if base_url is not None else os.environ.get(_ENV_BASE_URL, "")).strip()
        self._api_key = (api_key if api_key is not None else os.environ.get(_ENV_API_KEY, "")).strip()
        self._model = (model if model is not None else os.environ.get(_ENV_MODEL, "")).strip()
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        self._timeout_s = timeout_s
        self._max_tokens = max_tokens

    @classmethod
    def from_env(cls) -> "HttpMaster":
        """按环境变量构造；缺变量不会在构造期报错，只在真正外发时报 EscalationError。

        前置条件：无。
        后置条件：返回 HttpMaster；configured 反映三个变量是否齐全。
        禁止事项：不要在构造期打印或记录密钥。
        异常：无。
        """
        return cls()

    @property
    def configured(self) -> bool:
        """端点、密钥与模型名是否都已配置。

        前置条件：无。
        后置条件：三者均非空时为 True。
        禁止事项：不要用“有端点”代替“已配置”（缺密钥时外发必失败）。
        异常：无。
        """
        return bool(self._base_url and self._api_key and self._model)

    @property
    def model(self) -> str:
        """场外模型标识（未配置时为空串）。

        前置条件：无。
        后置条件：返回构造时读取的 model。
        禁止事项：不要在此回落到任何默认模型名。
        异常：无。
        """
        return self._model

    def complete(self, payload: Mapping[str, Any]) -> str:
        """把脱敏 payload 发给场外模型并返回答案文本。

        前置条件：payload 已由 Redactor 处理且可 JSON 序列化。
        后置条件：返回去除首尾空白的非空字符串。
        禁止事项：不要重试、不要修改 payload、不要回显 API key。
        异常：EscalationError（未配置、HTTP 非 2xx、网络失败、响应为空）。
        """
        missing = [
            name
            for name, value in (
                (_ENV_BASE_URL, self._base_url),
                (_ENV_API_KEY, self._api_key),
                (_ENV_MODEL, self._model),
            )
            if not value
        ]
        if missing:
            raise EscalationError(f"master is not configured: missing {', '.join(missing)}")

        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(dict(payload), ensure_ascii=False, indent=2),
                },
            ],
            "temperature": 0.2,
            "max_tokens": self._max_tokens,
            "stream": False,
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint(),
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # 不回显响应体：远端可能把请求内容（含路径）再抄回来。
            raise EscalationError(f"master returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EscalationError(f"cannot reach master: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EscalationError(f"master returned non-JSON envelope: {raw[:200]}") from exc
        if not isinstance(parsed, Mapping):
            raise EscalationError("master response must be a JSON object")
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise EscalationError("master response has no choices")
        message = choices[0].get("message") or {}
        if not isinstance(message, Mapping):
            raise EscalationError("master response message must be a JSON object")
        text = _content_text(message.get("content"))
        if not text:
            raise EscalationError("master response content is empty")
        return text

    def _endpoint(self) -> str:
        """拼出 chat/completions 的完整 URL。

        前置条件：base_url 非空（complete 已校验）。
        后置条件：兼容 base_url 已带 /v1 或完整路径两种情况。
        禁止事项：不要在这里追加查询参数或密钥。
        异常：无。
        """
        base = self._base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"


def _content_text(content: Any) -> str:
    """把 OpenAI 风格的 content 统一成字符串。

    前置条件：content 可为 str、list[dict] 或 None。
    后置条件：返回去除首尾空白的文本；无法识别时返回空串。
    禁止事项：不要把非文本 part 强行 str 化（可能带内部结构）。
    异常：无。
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, Mapping) and part.get("type", "text") == "text"
        ]
        return "\n".join(str(part).strip() for part in parts if str(part).strip()).strip()
    return ""


# 兼容架构文档中的类名（R4）；新代码统一使用 HttpMaster。
HandleToMaster = HttpMaster
