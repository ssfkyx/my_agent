"""workspace — 工作区类工具：echo / list_dir / read_file / write_file / run_command。

Module: agent.tools.workspace
Responsibility: 提供对 workspace 根目录的读写工具；写路径由 Guardrail 限制在 write_root，
    命令执行由 argv 前缀白名单、工作区参数检查与可选 bwrap 沙箱限制（ADR-026/028）。
Depends on: stdlib (os, shutil, subprocess, time, pathlib) + agent.core.protocol
Depended by: app（注册）, core.harness（调用）
ADRs: ADR-005（失败以 ToolResult 表达）, ADR-006（echo 是确定性探针）, ADR-009（路径由护栏归一化）,
    ADR-026（写/执行策略）, ADR-028（bwrap 命令沙箱）
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.core.protocol import ErrorCode, ErrorInfo, ToolCall, ToolResult, ToolSpec


class EchoTool:
    """回显文本的只读工具。

    前置条件：调用方在 args 中提供字符串 text。
    后置条件：返回 {text, length}；不改动任何外部状态。
    禁止事项：不要把它当"搜索"用；它不做任何检索。
    """

    spec = ToolSpec(
        name="workspace.echo",
        category="workspace",
        description=(
            "Echo the given text back unchanged and report its length. "
            "Use it to verify the tool pipeline when the request needs no external data."
        ),
        args_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo back."}},
            "required": ["text"],
        },
        side_effect=False,
    )

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行回显。

        前置条件：call.tool == "workspace.echo"。
        后置条件：ok=True 时 data 含 text 与 length；ok=False 时 error.code=BAD_ARGS。
        禁止事项：不要对缺失 text 做默认填充（会让模型的参数错误不可见）。
        异常：无（所有失败都转成 ToolResult.error）。
        """
        started = time.perf_counter()
        text: Any = call.args.get("text")
        if not isinstance(text, str):
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                error=ErrorInfo(
                    code=ErrorCode.BAD_ARGS,
                    message="args.text must be a string",
                    hint='call with {"text": "<something>"}',
                ),
                elapsed_s=time.perf_counter() - started,
            )
        payload: Mapping[str, Any] = {"text": text, "length": len(text)}
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data=payload,
            elapsed_s=time.perf_counter() - started,
        )


class ListDirTool:
    """列出目录条目（只读，非递归）。

    前置条件：args.path 已由 Guardrail 归一化为 workspace 内的绝对路径。
    后置条件：data = {path, entries:[{name, kind, size}], truncated}，按 name 排序。
    禁止事项：不要递归遍历（会在大目录上卡死）；不要跟随符号链接进入目录树。
    """

    spec = ToolSpec(
        name="workspace.list_dir",
        category="workspace",
        description=(
            "List entries of a directory inside the workspace (non-recursive, sorted by name). "
            "Use it to discover what documents and outputs exist before reading them."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path; relative paths are resolved against the workspace root.",
                }
            },
            "required": [],
        },
        side_effect=False,
    )

    def __init__(self, *, max_entries: int) -> None:
        self._max_entries = max_entries

    def invoke(self, call: ToolCall) -> ToolResult:
        """列出目录。

        前置条件：路径已由 Guardrail 归一化为绝对路径。
        后置条件：条目数不超过 max_entries，超出时 truncated=True。
        禁止事项：不要因个别条目不可 stat 而中断整个列表（_describe 已逐条兜底）。
        异常：无。
        """
        started = time.perf_counter()
        raw_path = call.args.get("path", ".")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return _fail(call, ErrorCode.BAD_ARGS, "args.path must be a non-empty string", started)

        directory = Path(raw_path)
        if not directory.is_absolute():
            return _fail(call, ErrorCode.BAD_ARGS, "args.path must be normalised to an absolute path", started)
        if not directory.exists():
            return _fail(call, ErrorCode.NOT_FOUND, f"directory not found: {directory}", started)
        if not directory.is_dir():
            return _fail(call, ErrorCode.BAD_ARGS, f"not a directory: {directory}", started)

        entries: list[dict[str, Any]] = []
        truncated = False
        try:
            for child in sorted(directory.iterdir(), key=lambda item: item.name):
                if len(entries) >= self._max_entries:
                    truncated = True
                    break
                entries.append(_describe(child))
        except OSError as exc:
            return _fail(call, ErrorCode.INTERNAL, f"cannot list {directory}: {exc}", started)

        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"path": str(directory), "entries": entries, "truncated": truncated},
            elapsed_s=time.perf_counter() - started,
        )


class ReadFileTool:
    """读取文本文件（有字节上限）。

    前置条件：args.path 已由 Guardrail 归一化为 workspace 内的绝对路径。
    后置条件：data = {path, bytes, truncated, text}；超限时只返回前 max_bytes 字节。
    禁止事项：不要一次性读入超过 max_bytes 的内容（用 read(n) 限额）。
    """

    spec = ToolSpec(
        name="workspace.read_file",
        category="workspace",
        description=(
            "Read a UTF-8 text file (Markdown, source, config) from the workspace. "
            "Content is truncated to a byte budget; use it before quoting a document."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path; relative paths are resolved against the workspace root.",
                }
            },
            "required": ["path"],
        },
        side_effect=False,
    )

    def __init__(self, *, max_bytes: int) -> None:
        self._max_bytes = max_bytes

    def invoke(self, call: ToolCall) -> ToolResult:
        """读取文件。

        前置条件：路径已归一化；文件可能是二进制。
        后置条件：text 为 UTF-8 解码结果（非法字节被替换），truncated 标明是否截断。
        禁止事项：不要把二进制内容当文本假装成功（解码替换字符已在 text 中体现）。
        异常：无。
        """
        started = time.perf_counter()
        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return _fail(call, ErrorCode.BAD_ARGS, "args.path must be a non-empty string", started)

        target = Path(raw_path)
        if not target.is_absolute():
            return _fail(call, ErrorCode.BAD_ARGS, "args.path must be normalised to an absolute path", started)
        if not target.exists():
            return _fail(call, ErrorCode.NOT_FOUND, f"file not found: {target}", started)
        if target.is_dir():
            return _fail(call, ErrorCode.BAD_ARGS, f"path is a directory: {target}", started)

        try:
            with target.open("rb") as handle:
                payload = handle.read(self._max_bytes + 1)
        except OSError as exc:
            return _fail(call, ErrorCode.INTERNAL, f"cannot read {target}: {exc}", started)

        truncated = len(payload) > self._max_bytes
        payload = payload[: self._max_bytes]
        text = payload.decode("utf-8", errors="replace")
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"path": str(target), "bytes": len(payload), "truncated": truncated, "text": text},
            elapsed_s=time.perf_counter() - started,
        )


class WriteFileTool:
    """把 UTF-8 文本写入 write_root 内的文件。

    前置条件：path 已由 Guardrail 归一化并确认位于 write_root；text 为字符串。
    后置条件：成功时文件内容等于 text；默认不覆盖已有文件。
    禁止事项：不要在这里绕过 Guardrail 的 write_root 检查；不要写二进制。
    """

    spec = ToolSpec(
        name="workspace.write_file",
        category="workspace",
        description=(
            "Write UTF-8 text to a file under the configured write root (artifacts by default). "
            "Existing files are protected unless overwrite=true."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Target file; relative paths resolve against the workspace root.",
                },
                "text": {"type": "string", "description": "UTF-8 text to write."},
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace an existing file. Default false.",
                },
            },
            "required": ["path", "text"],
        },
        side_effect=True,
    )

    def __init__(self, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        self._max_bytes = max_bytes

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行写入。

        前置条件：path 为绝对路径；text 为 str；overwrite 为 bool（缺省 False）。
        后置条件：ok=True 时 data 含 path/bytes/created/overwritten。
        禁止事项：不要在未设置 overwrite 时替换已有文件。
        异常：无。
        """
        started = time.perf_counter()
        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return _fail(call, ErrorCode.BAD_ARGS, "args.path must be a non-empty string", started)
        path = Path(raw_path)
        if not path.is_absolute():
            return _fail(call, ErrorCode.BAD_ARGS, "args.path must be normalised to an absolute path", started)
        text = call.args.get("text")
        if not isinstance(text, str):
            return _fail(call, ErrorCode.BAD_ARGS, "args.text must be a string", started)
        overwrite = call.args.get("overwrite", False)
        if not isinstance(overwrite, bool):
            return _fail(call, ErrorCode.BAD_ARGS, "args.overwrite must be a boolean", started)
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            return _fail(call, ErrorCode.BAD_ARGS, f"text is not valid UTF-8: {exc}", started)
        if len(encoded) > self._max_bytes:
            return _fail(
                call,
                ErrorCode.BAD_ARGS,
                f"text is {len(encoded)} bytes; limit is {self._max_bytes}",
                started,
            )

        existed = path.exists()
        if existed and not path.is_file():
            return _fail(call, ErrorCode.BAD_ARGS, "target exists and is not a regular file", started)
        if existed and not overwrite:
            return _fail(
                call,
                ErrorCode.BAD_ARGS,
                "target already exists; set overwrite=true to replace it",
                started,
                hint="write to a new path or pass overwrite=true",
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            return _fail(call, ErrorCode.INTERNAL, f"cannot write file: {exc}", started)
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "path": str(path),
                "bytes": len(encoded),
                "created": not existed,
                "overwritten": existed,
            },
            elapsed_s=time.perf_counter() - started,
        )


class RunCommandTool:
    """在工作区根目录执行一条白名单 argv（无 shell，可选 bwrap 沙箱）。

    前置条件：argv 为字符串数组；至少匹配一个配置的 argv 前缀；参数不得逃出 workspace。
    后置条件：成功时返回 returncode/stdout/stderr；超时返回 TIMEOUT 错误。
    禁止事项：不要使用 shell=True；不要接受绝对路径/`..` 参数；不要继承含密钥的环境。
    """

    spec = ToolSpec(
        name="workspace.run_command",
        category="workspace",
        description=(
            "Run an allow-listed command (argv array, no shell) in the workspace root. "
            "It runs in a bwrap sandbox when available: workspace is read-only, only the "
            "write root is writable, and networking is disabled. Use profile to pick a "
            "configured allow-list (default if omitted)."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Command and arguments; must match the selected profile's allow-list prefix.",
                },
                "profile": {
                    "type": "string",
                    "description": "Allow-list profile name (e.g. default, git, python). Omit for default.",
                },
                "timeout_s": {
                    "type": "number",
                    "description": "Optional timeout in seconds, clamped to the configured maximum.",
                },
            },
            "required": ["argv"],
        },
        side_effect=True,
    )

    def __init__(
        self,
        *,
        root: Path,
        allowlist: Sequence[Sequence[str]],
        timeout_s: float,
        max_output_bytes: int,
        write_root: Path | None = None,
        sandbox: str = "auto",
        profiles: Mapping[str, Sequence[Sequence[str]]] | None = None,
        default_profile: str = "default",
    ) -> None:
        if not root.is_absolute():
            raise ValueError("root must be an absolute path")
        if timeout_s <= 0 or max_output_bytes <= 0:
            raise ValueError("timeout and output limit must be positive")
        if sandbox not in {"none", "bwrap", "auto"}:
            raise ValueError("sandbox must be one of: none, bwrap, auto")
        if write_root is not None:
            if not write_root.is_absolute():
                raise ValueError("write_root must be an absolute path")
            if not write_root.is_relative_to(root):
                raise ValueError("write_root must be inside root")
        bwrap = shutil.which("bwrap") if sandbox in {"bwrap", "auto"} else None
        if sandbox == "bwrap" and bwrap is None:
            raise ValueError("command_sandbox=bwrap but bwrap was not found")
        self._root = root
        self._allowlist = tuple(tuple(prefix) for prefix in allowlist)
        self._timeout_s = timeout_s
        self._max_output_bytes = max_output_bytes
        self._write_root = write_root
        self._bwrap = bwrap
        self._sandbox = "bwrap" if bwrap is not None else "none"
        self._profiles: dict[str, tuple[tuple[str, ...], ...]] = {"default": self._allowlist}
        for name, entries in (profiles or {}).items():
            if not name.strip():
                raise ValueError("profile names must be non-empty")
            self._profiles[name] = tuple(tuple(prefix) for prefix in entries)
        if default_profile not in self._profiles:
            raise ValueError(f"unknown default command profile: {default_profile}")
        self._default_profile = default_profile
        if self._sandbox == "bwrap" and write_root is not None:
            write_root.mkdir(parents=True, exist_ok=True)

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行命令。

        前置条件：argv 为非空字符串数组。
        后置条件：ok=True 时 data 含 argv/cwd/returncode/stdout/stderr/truncated。
        禁止事项：不要在未命中白名单或参数越界时执行命令。
        异常：无。
        """
        started = time.perf_counter()
        raw_argv = call.args.get("argv")
        if not isinstance(raw_argv, (list, tuple)) or not raw_argv:
            return _fail(call, ErrorCode.BAD_ARGS, "args.argv must be a non-empty array of strings", started)
        if len(raw_argv) > 32:
            return _fail(call, ErrorCode.BAD_ARGS, "args.argv is too long (max 32 items)", started)
        argv: list[str] = []
        for item in raw_argv:
            if not isinstance(item, str) or not item.strip():
                return _fail(call, ErrorCode.BAD_ARGS, "every args.argv item must be a non-empty string", started)
            argv.append(item)
        raw_profile = call.args.get("profile")
        if raw_profile is None:
            profile = self._default_profile
        elif not isinstance(raw_profile, str) or not raw_profile.strip():
            return _fail(call, ErrorCode.BAD_ARGS, "args.profile must be a non-empty string", started)
        else:
            profile = raw_profile.strip()
        if profile not in self._profiles:
            available = ", ".join(sorted(self._profiles))
            return _fail(
                call,
                ErrorCode.BAD_ARGS,
                f"unknown command profile: {profile}",
                started,
                hint=f"available profiles: {available}",
            )
        allowlist = self._profiles[profile]
        if not self._allowed(argv, allowlist):
            allowed = " | ".join(" ".join(prefix) for prefix in allowlist) or "(none)"
            return _fail(
                call,
                ErrorCode.DENIED_BY_GUARDRAIL,
                f"command is not in the allow-list for profile {profile}",
                started,
                hint=f"allowed prefixes: {allowed}",
            )
        if not self._paths_allowed(argv):
            return _fail(
                call,
                ErrorCode.DENIED_BY_GUARDRAIL,
                "command argument escapes the workspace root",
                started,
                hint="use relative paths inside the workspace",
            )

        raw_timeout = call.args.get("timeout_s", self._timeout_s)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            return _fail(call, ErrorCode.BAD_ARGS, "args.timeout_s must be a number", started)
        timeout = min(max(float(raw_timeout), 0.5), self._timeout_s)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self._root),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONPATH": str(self._root),
            "PYTHONIOENCODING": "utf-8",
            # git status 只需要读；禁用可选锁，避免顺手改 .git/index。
            "GIT_OPTIONAL_LOCKS": "0",
        }
        try:
            completed = subprocess.run(
                self._wrap(argv, env),
                cwd=str(self._root),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            stdout, out_truncated = _decode_cap(exc.stdout, self._max_output_bytes)
            stderr, err_truncated = _decode_cap(exc.stderr, self._max_output_bytes)
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                data={
                    "argv": argv,
                    "cwd": str(self._root),
                    "sandbox": self._sandbox,
                    "profile": profile,
                    "stdout": stdout,
                    "stderr": stderr,
                    "truncated": out_truncated or err_truncated,
                    "timed_out": True,
                },
                error=ErrorInfo(
                    code=ErrorCode.TIMEOUT,
                    message=f"command timed out after {timeout:.1f}s",
                    retryable=False,
                ),
                elapsed_s=time.perf_counter() - started,
            )
        except FileNotFoundError:
            return _fail(call, ErrorCode.NOT_FOUND, f"command not found: {argv[0]}", started)
        except OSError as exc:
            return _fail(call, ErrorCode.INTERNAL, f"cannot execute command: {exc}", started)

        stdout, out_truncated = _decode_cap(completed.stdout, self._max_output_bytes)
        stderr, err_truncated = _decode_cap(completed.stderr, self._max_output_bytes)
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "argv": argv,
                "cwd": str(self._root),
                "sandbox": self._sandbox,
                "profile": profile,
                "returncode": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": out_truncated or err_truncated,
                "timed_out": False,
            },
            elapsed_s=time.perf_counter() - started,
        )

    def _wrap(self, argv: Sequence[str], env: Mapping[str, str]) -> list[str]:
        """按配置生成 bwrap 包装命令。

        前置条件：argv 已通过白名单与路径检查。
        后置条件：sandbox=bwrap 时返回 bwrap argv；否则原样返回。
        禁止事项：不要在沙箱里给 workspace 根目录写权限；只有 write_root 可写。
        异常：无。
        """
        if self._sandbox != "bwrap" or self._bwrap is None:
            return list(argv)
        command = [
            self._bwrap,
            "--die-with-parent",
            "--unshare-all",
            "--clearenv",
        ]
        for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"):
            if Path(path).exists():
                command.extend(["--ro-bind", path, path])
        # 先挂 tmpfs 再挂 workspace：workspace 可能位于 /tmp 下，顺序反了会被 tmpfs 遮住。
        command.extend(["--tmpfs", "/tmp"])
        root = str(self._root)
        command.extend(["--ro-bind", root, root])
        if self._write_root is not None:
            write_root = str(self._write_root)
            if self._write_root.exists():
                command.extend(["--bind", write_root, write_root])
        command.extend(
            [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--chdir",
                root,
            ]
        )
        for key, value in env.items():
            command.extend(["--setenv", key, value])
        command.extend(["--", *argv])
        return command

    def _allowed(self, argv: Sequence[str], allowlist: Sequence[Sequence[str]]) -> bool:
        """argv 是否匹配所选 profile 的任一配置前缀。"""
        return any(tuple(argv[: len(prefix)]) == tuple(prefix) for prefix in allowlist)

    def _paths_allowed(self, argv: Sequence[str]) -> bool:
        """拒绝绝对路径、`~` 与逃出 workspace 的相对路径参数。"""
        for raw in argv[1:]:
            candidate = raw.split("=", 1)[1] if "=" in raw else raw
            if candidate.startswith("-"):
                continue
            if candidate.startswith(("~", "/")):
                return False
            if ".." in Path(candidate).parts:
                return False
            if "/" in candidate or "\\" in candidate:
                resolved = (self._root / candidate).resolve()
                if not resolved.is_relative_to(self._root):
                    return False
        return True


def _decode_cap(data: bytes | None, limit: int) -> tuple[str, bool]:
    """把 bytes 输出截断并解码。

    前置条件：limit > 0；data 可为 None。
    后置条件：返回 (text, truncated)；text 最多 limit 字节解码后的文本。
    禁止事项：不要对 None 做 len()。
    异常：无。
    """
    if not data:
        return "", False
    truncated = len(data) > limit
    return data[:limit].decode("utf-8", errors="replace"), truncated


def _describe(path: Path) -> dict[str, Any]:
    """把目录项压成可序列化描述。

    前置条件：path 来自 iterdir()。
    后置条件：返回 {name, kind, size}；size 对目录为 None。
    禁止事项：不要在此抛异常（权限问题不该中断整个列表）。
    异常：无。
    """
    try:
        if path.is_symlink():
            kind = "symlink"
        elif path.is_dir():
            kind = "dir"
        else:
            kind = "file"
        size = None if kind == "dir" else path.stat().st_size
    except OSError:
        return {"name": path.name, "kind": "unknown", "size": None}
    return {"name": path.name, "kind": kind, "size": size}


def _fail(
    call: ToolCall,
    code: ErrorCode,
    message: str,
    started: float,
    *,
    retryable: bool = False,
    hint: str = "",
) -> ToolResult:
    """构造失败回执。

    前置条件：started 为 time.perf_counter() 的起点。
    后置条件：返回 ok=False 且带 error 的 ToolResult，call_id 与入参一致。
    禁止事项：不要吞掉 call_id（回执必须能对上 TOOL_CALL）。
    异常：无。
    """
    return ToolResult(
        call_id=call.call_id,
        ok=False,
        error=ErrorInfo(code=code, message=message, retryable=retryable, hint=hint),
        elapsed_s=time.perf_counter() - started,
    )
