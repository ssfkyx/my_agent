"""perception — 媒体感知工具：附件分类、语音转写与图片描述。

Module: agent.tools.perception
Responsibility: 把 MediaPort 暴露成主 agent 可调用的工具，并把 MediaError 翻译成 ToolResult。
Depends on: stdlib (time, pathlib) + agent.core.protocol + agent.ports
Depended by: app（注册）, core.harness（调用）
ADRs: ADR-020（感知工具与 mmproj 门禁）, ADR-009（路径由护栏归一化）, ADR-042（按模态注册）
"""

import time
from pathlib import Path

from agent.core.protocol import ErrorCode, ErrorInfo, ToolCall, ToolResult, ToolSpec
from agent.ports import MediaError, MediaPort, Tool


class TranscribeTool:
    """把音频转写成文本。

    前置条件：args.path 已由护栏归一化为工作区内绝对路径。
    后置条件：成功时 data.text 为转写结果（可能为空串）。
    禁止事项：不要在缺少 mmproj 时假装已转写（由 MediaPort 抛错，这里只翻译）。
    """

    spec = ToolSpec(
        name="perception.transcribe",
        category="perception",
        description=(
            "Transcribe speech from an audio file inside the workspace into text. "
            "Use it when the user refers to a recording (.wav/.mp3/.flac/.ogg)."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Audio file path; relative paths resolve against the workspace root.",
                }
            },
            "required": ["path"],
        },
        side_effect=False,
    )

    def __init__(self, *, media: MediaPort) -> None:
        self._media = media

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行转写。

        前置条件：path 为归一化绝对路径。
        后置条件：ok=True 时 data 含 text/model_alias；失败时 error.code 来自 MediaError。
        禁止事项：不要把空转写结果改写成"没有内容"的结论（那是模型的判断）。
        异常：无。
        """
        started = time.perf_counter()
        path = _require_absolute(call, "path")
        if isinstance(path, ToolResult):
            return path
        try:
            result = self._media.transcribe(path)
        except MediaError as exc:
            return _fail(call, exc, started)
        except OSError as exc:
            return _fail(call, MediaError(ErrorCode.INTERNAL, f"cannot read audio: {exc}"), started)
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"path": str(path), "text": result.text, "model": result.model_alias, "elapsed_s": result.elapsed_s},
            elapsed_s=time.perf_counter() - started,
        )


class DescribeImageTool:
    """描述一张图片。

    前置条件：args.path 已归一化；prompt 可为空（使用默认提问）。
    后置条件：成功时 data.text 为模型描述。
    禁止事项：不要让模型凭空补全看不到的细节（提示词已要求"factually"）。
    """

    spec = ToolSpec(
        name="perception.describe_image",
        category="perception",
        description=(
            "Look at an image file inside the workspace and describe it. Use it when the user "
            "refers to a screenshot/photo/diagram (.png/.jpg/.jpeg/.webp/.gif)."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Image path; relative paths resolve against the workspace root.",
                },
                "prompt": {"type": "string", "description": "Optional question about the image."},
            },
            "required": ["path"],
        },
        side_effect=False,
    )

    def __init__(self, *, media: MediaPort) -> None:
        self._media = media

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行看图。

        前置条件：path 为归一化绝对路径；prompt 为字符串或缺省。
        后置条件：ok=True 时 data.text 非空。
        禁止事项：不要在失败时返回缓存的旧描述。
        异常：无。
        """
        started = time.perf_counter()
        path = _require_absolute(call, "path")
        if isinstance(path, ToolResult):
            return path
        prompt = call.args.get("prompt", "")
        if not isinstance(prompt, str):
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                error=ErrorInfo(code=ErrorCode.BAD_ARGS, message="args.prompt must be a string"),
                elapsed_s=time.perf_counter() - started,
            )
        try:
            result = self._media.describe_image(path, prompt)
        except MediaError as exc:
            return _fail(call, exc, started)
        except OSError as exc:
            return _fail(call, MediaError(ErrorCode.INTERNAL, f"cannot read image: {exc}"), started)
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"path": str(path), "text": result.text, "model": result.model_alias, "elapsed_s": result.elapsed_s},
            elapsed_s=time.perf_counter() - started,
        )


class AttachFileTool:
    """检查并分类一个工作区文件，提示应使用哪种感知/读取工具。

    前置条件：args.path 已由护栏归一化为 workspace 内的绝对路径。
    后置条件：成功时返回 name/suffix/size/kind 与下一步 hint；不读取文件内容。
    禁止事项：不要在这里转写音频或描述图片（那属于 MediaPort 工具）。
    """

    spec = ToolSpec(
        name="perception.attach_file",
        category="perception",
        description=(
            "Inspect a file in the workspace and classify it as audio/image/text/other. "
            "Use it first when the user refers to an attachment, then call the matching tool."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path; relative paths resolve against the workspace root.",
                }
            },
            "required": ["path"],
        },
        side_effect=False,
    )

    _AUDIO = frozenset({".wav", ".mp3", ".flac", ".ogg", ".m4a"})
    _IMAGE = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
    _TEXT = frozenset(
        {
            ".md",
            ".txt",
            ".json",
            ".toml",
            ".yaml",
            ".yml",
            ".py",
            ".csv",
            ".log",
            ".ini",
            ".cfg",
        }
    )

    def invoke(self, call: ToolCall) -> ToolResult:
        """返回文件元数据与建议工具。

        前置条件：path 已归一化为绝对路径。
        后置条件：ok=True 时 data 含 path/name/suffix/size_bytes/kind/hint。
        禁止事项：不要读取正文（正文由 workspace.read_file / MediaPort 负责）。
        异常：无。
        """
        started = time.perf_counter()
        path = _require_absolute(call, "path")
        if isinstance(path, ToolResult):
            return path
        try:
            stat = path.stat()
        except FileNotFoundError:
            return _fail(call, MediaError(ErrorCode.NOT_FOUND, f"file not found: {path.name}"), started)
        except OSError as exc:
            return _fail(call, MediaError(ErrorCode.INTERNAL, f"cannot stat file: {exc}"), started)
        if not path.is_file():
            return _fail(call, MediaError(ErrorCode.BAD_ARGS, "path is not a regular file"), started)

        suffix = path.suffix.casefold()
        if suffix in self._AUDIO:
            kind, hint = "audio", "call perception.transcribe with this path"
        elif suffix in self._IMAGE:
            kind, hint = "image", "call perception.describe_image with this path"
        elif suffix in self._TEXT:
            kind, hint = "text", "call workspace.read_file with this path"
        else:
            kind, hint = "other", "read a bounded preview with workspace.read_file if it is text"
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "path": str(path),
                "name": path.name,
                "suffix": suffix,
                "size_bytes": stat.st_size,
                "kind": kind,
                "hint": hint,
            },
            elapsed_s=time.perf_counter() - started,
        )


def _require_absolute(call: ToolCall, key: str) -> Path | ToolResult:
    """校验路径参数已被护栏归一化。

    前置条件：key 为参数名。
    后置条件：返回绝对 Path；不合法时返回 BAD_ARGS 的 ToolResult。
    禁止事项：不要在此做路径解析（解析属于护栏，避免两套规则不一致）。
    异常：无。
    """
    raw = call.args.get(key)
    if not isinstance(raw, str) or not raw.strip():
        return _bad_args(call, f"args.{key} must be a non-empty string")
    path = Path(raw)
    if not path.is_absolute():
        return _bad_args(call, f"args.{key} must be normalised to an absolute path")
    return path


def _bad_args(call: ToolCall, message: str) -> ToolResult:
    """构造 BAD_ARGS 失败回执。

    前置条件：message 非空。
    后置条件：返回 ok=False 且 call_id 对齐的 ToolResult。
    禁止事项：不要吞掉 call_id。
    异常：无。
    """
    return ToolResult(
        call_id=call.call_id,
        ok=False,
        error=ErrorInfo(code=ErrorCode.BAD_ARGS, message=message),
        elapsed_s=0.0,
    )


def _fail(call: ToolCall, error: MediaError, started: float) -> ToolResult:
    """把 MediaError 翻译成 ToolResult。

    前置条件：error.code 为该错误应传播的协议码。
    后置条件：返回 ok=False；MISSING_MMPROJ 带补救提示。
    禁止事项：不要在这里改变错误码（上层用它做统计与决策）。
    异常：无。
    """
    hint = "add mmproj for this alias in config/defaults.toml" if error.code is ErrorCode.MISSING_MMPROJ else ""
    return ToolResult(
        call_id=call.call_id,
        ok=False,
        error=ErrorInfo(code=error.code, message=error.message, retryable=error.retryable, hint=hint),
        elapsed_s=time.perf_counter() - started,
    )


def build_tools(
    media: MediaPort,
    *,
    enable_asr: bool = True,
    enable_vl: bool = True,
) -> list[Tool]:
    """构造感知类工具列表；未启用的模态不注册对应工具（ADR-042）。

    前置条件：media 已就绪（mmproj 门禁在其内部）。
    后置条件：至少返回一个工具；Attach 在有媒体能力时可用。
    禁止事项：不要在工具内部自行构造 MediaPort（共享同一 supervisor）。
    异常：无。
    """
    tools: list[Tool] = [AttachFileTool()]
    if enable_asr:
        tools.append(TranscribeTool(media=media))
    if enable_vl:
        tools.append(DescribeImageTool(media=media))
    return tools
