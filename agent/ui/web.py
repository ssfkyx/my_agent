"""web — stdlib HTTP/SSE 单页 UI（ADR-027）。

Module: agent.ui.web
Responsibility: 作为第三个 UiPort 消费者：把 Envelope 推给浏览器，并把浏览器输入/同意决定
    交回 App.run_session；不 import core.harness，不直接调用模型或工具。
Depends on: stdlib (http.server, json, queue, threading) + agent.core.protocol + agent.ui.presenter
Depended by: app, __main__
ADRs: ADR-006（UI 只消费信封）, ADR-021（同意询问）, ADR-025（第二消费者 TUI）,
    ADR-027（Web UI）, ADR-036（Web 多模态附件上传）
"""

import email.parser
import email.policy
import hmac
import json
import queue
import re
import ssl
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from agent.core.protocol import Envelope

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".flac", ".ogg", ".m4a"})
_MAX_UPLOAD_BODY = 32 * 1024 * 1024


@dataclass
class _Session:
    """一个浏览器会话的隔离状态。"""

    sid: str
    max_events: int
    events: deque[dict[str, Any]] = field(init=False)
    subscribers: set[queue.Queue[dict[str, Any]]] = field(default_factory=set)
    input_queue: queue.Queue[str | None] = field(default_factory=queue.Queue)
    active: bool = False
    running: bool = False
    consent_pending: bool = False
    consent_result: bool = False
    consent_event: threading.Event = field(default_factory=threading.Event)
    consent_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.events = deque(maxlen=self.max_events)


class WebUI:
    """HTTP + SSE UI：多会话隔离，但由 App 串行执行 turn。

    前置条件：host 默认为回环地址；port 可为 0（测试时由系统分配）。
    后置条件：构造后 HTTP 服务已启动；next_job/read_user_input/ask_consent 阻塞等待浏览器。
    禁止事项：不要在这里调用 Harness；不要并发执行两个 turn（模型调用必须串行）。
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        consent_mode: str = "ask",
        max_events: int = 500,
        auth_token: str | None = None,
        tls_cert: Path | None = None,
        tls_key: Path | None = None,
        on_cancel: Callable[[], None] | None = None,
        upload_root: Path | None = None,
        workspace_root: Path | None = None,
        max_image_bytes: int = 10 * 1024 * 1024,
        max_audio_bytes: int = 25 * 1024 * 1024,
        media_enabled: bool = True,
        image_enabled: bool = True,
        audio_enabled: bool = True,
    ) -> None:
        if consent_mode not in {"ask", "allow", "deny"}:
            raise ValueError(f"unknown consent mode: {consent_mode}")
        if (tls_cert is None) != (tls_key is None):
            raise ValueError("tls_cert and tls_key must be provided together")
        if upload_root is not None:
            if workspace_root is None or not workspace_root.is_absolute():
                raise ValueError("workspace_root must be absolute when uploads are enabled")
            if not upload_root.is_absolute() or not upload_root.is_relative_to(workspace_root):
                raise ValueError("upload_root must be an absolute path inside workspace_root")
        if max_image_bytes <= 0 or max_audio_bytes <= 0:
            raise ValueError("upload byte limits must be positive")
        self._consent_mode = consent_mode
        self._auth_token = auth_token.strip() if auth_token else None
        self._on_cancel = on_cancel
        self._max_events = max_events
        self._upload_root = upload_root
        self._workspace_root = workspace_root
        self._max_image_bytes = max_image_bytes
        self._max_audio_bytes = max_audio_bytes
        self._media_enabled = media_enabled
        self._image_enabled = image_enabled
        self._audio_enabled = audio_enabled
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._jobs: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._closed = threading.Event()
        self._active_sid: str | None = None
        self._session("default")

        self._server = ThreadingHTTPServer((host, port), _WebHandler)
        if tls_cert is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(tls_cert), str(tls_key))
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._scheme = "https" if tls_cert is not None else "http"
        self._server.daemon_threads = True
        self._server.ui = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, name="agent-web", daemon=True)
        self._thread.start()
        self._host = host
        self._port = int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """浏览器访问地址；绑定 0.0.0.0 时显示回环地址。"""
        host = "127.0.0.1" if self._host in {"0.0.0.0", "::"} else self._host
        return f"{self._scheme}://{host}:{self._port}/"

    @property
    def auth_enabled(self) -> bool:
        return self._auth_token is not None

    @property
    def upload_enabled(self) -> bool:
        return (
            self._upload_root is not None
            and self._workspace_root is not None
            and self._media_enabled
            and (self._image_enabled or self._audio_enabled)
        )

    def _upload_dir(self, sid: str) -> Path:
        """本会话的上传目录：<write_root>/uploads/<sid>。"""
        assert self._upload_root is not None
        return self._upload_root / "uploads" / sid

    @staticmethod
    def _kind_for_suffix(suffix: str) -> str | None:
        suffix = suffix.casefold()
        if suffix in _IMAGE_SUFFIXES:
            return "image"
        if suffix in _AUDIO_SUFFIXES:
            return "audio"
        return None

    @staticmethod
    def _safe_filename(filename: str) -> str:
        base = Path(filename).name
        cleaned = _SAFE_FILENAME.sub("_", base).strip("._-") or "upload"
        suffix = Path(cleaned).suffix[:16]
        stem = Path(cleaned).stem[:80] or "upload"
        return f"{stem}{suffix}"

    def save_upload(self, sid: str, filename: str, data: bytes) -> tuple[int, dict[str, Any]]:
        """保存一个浏览器上传文件，返回相对 workspace 的路径。

        前置条件：sid 已通过 session_id 校验；data 为原始字节。
        后置条件：成功时文件位于 <write_root>/uploads/<sid>/，返回 image/audio 分类。
        禁止事项：不要信任浏览器文件名（必须 basename + 白名单字符）；不要写出 upload_root。
        异常：无（IO 错误转为 HTTP 状态）。
        """
        if not self.upload_enabled:
            return HTTPStatus.FORBIDDEN, {"ok": False, "error": "uploads are disabled"}
        suffix = Path(filename).suffix.casefold()
        kind = self._kind_for_suffix(suffix)
        if kind is None:
            return HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "error": f"unsupported file type: {suffix or '(no suffix)'}",
            }
        if kind == "image" and not self._image_enabled:
            return HTTPStatus.FORBIDDEN, {"ok": False, "error": "image uploads are disabled"}
        if kind == "audio" and not self._audio_enabled:
            return HTTPStatus.FORBIDDEN, {"ok": False, "error": "audio uploads are disabled"}
        limit = self._max_image_bytes if kind == "image" else self._max_audio_bytes
        if not data:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "uploaded file is empty"}
        if len(data) > limit:
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {
                "ok": False,
                "error": f"{kind} exceeds limit: {len(data)} > {limit} bytes",
            }

        assert self._upload_root is not None and self._workspace_root is not None
        directory = self._upload_dir(sid)
        directory.mkdir(parents=True, exist_ok=True)
        safe_name = self._safe_filename(filename)
        target = directory / f"{uuid.uuid4().hex[:10]}_{safe_name}"
        try:
            target.write_bytes(bytes(data))
        except OSError as exc:
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"cannot save upload: {exc}"}
        return HTTPStatus.OK, {
            "ok": True,
            "file": {
                "name": safe_name,
                "path": str(target.relative_to(self._workspace_root)),
                "kind": kind,
                "bytes": len(data),
            },
        }

    def _normalise_attachment(self, raw: str, sid: str) -> tuple[str, str] | None:
        """把附件路径限制在本会话 upload 目录内，返回 (relative_path, kind)。"""
        if not self.upload_enabled:
            return None
        assert self._upload_root is not None and self._workspace_root is not None
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self._workspace_root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        upload_dir = self._upload_dir(sid).resolve()
        if not resolved.is_relative_to(upload_dir) or not resolved.is_file():
            return None
        kind = self._kind_for_suffix(resolved.suffix)
        if kind is None:
            return None
        if kind == "image" and not self._image_enabled:
            return None
        if kind == "audio" and not self._audio_enabled:
            return None
        return str(resolved.relative_to(self._workspace_root)), kind

    def _message_with_attachments(
        self, text: str, attachments: Sequence[str] | None, sid: str
    ) -> str | None:
        """把附件路径与工具提示拼进用户消息；非法附件返回 None。"""
        if attachments is None:
            attachments = []
        if isinstance(attachments, (str, bytes)) or not isinstance(attachments, Sequence):
            return None
        valid: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw in attachments:
            if not isinstance(raw, str) or not raw.strip():
                return None
            normalised = self._normalise_attachment(raw, sid)
            if normalised is None:
                return None
            path, kind = normalised
            if path not in seen:
                seen.add(path)
                valid.append((path, kind))

        text = text.strip()
        if not text and not valid:
            return None
        if not valid:
            return text
        lines = ["[attachments]"]
        lines.extend(f"- {path} ({kind})" for path, kind in valid)
        lines.append("Use perception.attach_file on each attachment.")
        if any(kind == "image" for _, kind in valid):
            lines.append("Use perception.describe_image for images.")
        if any(kind == "audio" for _, kind in valid):
            lines.append("Use perception.transcribe for audio.")
        block = "\n".join(lines)
        return f"{text}\n\n{block}" if text else f"Please analyze the attached files.\n\n{block}"

    def receive_upload(
        self,
        sid: str,
        content_type: str,
        content_length: int,
        stream: Any,
    ) -> tuple[int, dict[str, Any]]:
        """解析 multipart/form-data 并保存其中的文件。"""
        if not self.upload_enabled:
            return HTTPStatus.FORBIDDEN, {"ok": False, "error": "uploads are disabled"}
        if not content_type.casefold().startswith("multipart/form-data"):
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "expected multipart/form-data"}
        if content_length <= 0:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "empty upload body"}
        if content_length > _MAX_UPLOAD_BODY:
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {
                "ok": False,
                "error": f"upload body exceeds {_MAX_UPLOAD_BODY} bytes",
            }
        body = stream.read(content_length)
        if len(body) != content_length:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "incomplete upload body"}
        raw = (
            b"Content-Type: "
            + content_type.encode("latin-1", errors="replace")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
        try:
            message = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
        except Exception:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "cannot parse multipart body"}
        if not message.is_multipart():
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "multipart body has no parts"}

        files: list[dict[str, Any]] = []
        errors: list[str] = []
        first_error = HTTPStatus.BAD_REQUEST
        for part in message.iter_parts():
            filename = part.get_filename()
            if not filename:
                continue
            payload = part.get_payload(decode=True)
            if not isinstance(payload, (bytes, bytearray)):
                errors.append(f"{filename}: invalid file payload")
                continue
            status, result = self.save_upload(sid, filename, bytes(payload))
            if status == HTTPStatus.OK:
                files.append(result["file"])
            else:
                first_error = status
                errors.append(result.get("error", f"{filename}: upload failed"))
        if not files:
            return first_error, {"ok": False, "error": errors[0] if errors else "no file part found"}
        return HTTPStatus.OK, {"ok": True, "files": files, "errors": errors}

    def authorized(self, headers: Mapping[str, str], raw_path: str) -> bool:
        """校验 Bearer token；未配置 token 时永远放行。"""
        if self._auth_token is None:
            return True
        header = headers.get("Authorization", "")
        if header.startswith("Bearer "):
            candidate = header[7:].strip()
        else:
            candidate = parse_qs(urlsplit(raw_path).query).get("token", [""])[0]
        return hmac.compare_digest(candidate, self._auth_token)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def session_id(self, headers: Mapping[str, str], raw_path: str) -> str:
        """从请求头/query 取会话 id；缺省/非法时落到 default。"""
        sid = (
            headers.get("X-Agent-Session")
            or parse_qs(urlsplit(raw_path).query).get("session", [""])[0]
            or "default"
        )
        return sid if _SESSION_ID.fullmatch(sid) else "default"

    def _session(self, sid: str) -> _Session:
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                session = _Session(sid, self._max_events)
                self._sessions[sid] = session
            return session

    def render(self, envelope: Envelope) -> None:
        """把一条 Envelope 推给当前 turn 所属会话的 SSE 客户端。"""
        with self._lock:
            sid = self._active_sid or "default"
        self._publish(sid, {"type": "envelope", "envelope": envelope.to_json()})

    def report(self, envelope: Envelope, ledger_path: Path) -> None:
        """推送最终报告与 ledger 路径，并结束当前会话的 busy 状态。"""
        with self._lock:
            sid = self._active_sid or "default"
        self._set_session_active(sid, False)
        self._publish(
            sid,
            {
                "type": "report",
                "envelope": envelope.to_json(),
                "ledger": str(ledger_path),
            },
        )

    def ask_consent(self, question: str) -> bool:
        """在当前会话弹窗询问；阻塞直到 POST /consent 或关闭。"""
        if self._consent_mode == "allow":
            return True
        if self._consent_mode == "deny":
            return False
        if self._closed.is_set():
            return False
        with self._lock:
            sid = self._active_sid or "default"
        session = self._session(sid)
        with session.consent_lock:
            session.consent_pending = True
            session.consent_result = False
            session.consent_event.clear()
        self._publish(sid, {"type": "consent", "question": question})
        while not session.consent_event.wait(0.5):
            if self._closed.is_set():
                with session.consent_lock:
                    session.consent_pending = False
                return False
        with session.consent_lock:
            session.consent_pending = False
            return session.consent_result

    def read_user_input(self) -> str | None:
        """兼容旧接口：读取 default 会话的一条输入。"""
        text = self._session("default").input_queue.get()
        if text is None:
            return None
        return text

    def next_job(self) -> tuple[str, str] | None:
        """阻塞等待下一条 (session_id, text)；关闭时返回 None。"""
        while not self._closed.is_set():
            try:
                return self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
        return None

    def begin_turn(self, sid: str) -> None:
        """标记会话开始执行 turn，并把 envelopes 路由到该会话。"""
        session = self._session(sid)
        with self._lock:
            self._active_sid = sid
            session.active = True
            session.running = True
        # 兼容 read_user_input 的 per-session 队列；真实 App 循环用 next_job。
        try:
            session.input_queue.get_nowait()
        except queue.Empty:
            pass
        self._publish(sid, {"type": "state", "busy": True})

    def end_turn(self, sid: str) -> None:
        """清除当前活动会话标记。"""
        session = self._session(sid)
        with self._lock:
            session.running = False
            if self._active_sid == sid:
                self._active_sid = None

    def close_session(self, sid: str) -> None:
        """关闭一个浏览器会话（/quit），不影响其他会话。"""
        session = self._session(sid)
        with self._lock:
            session.active = False
            session.running = False
        self._publish(sid, {"type": "session_closed"})

    def show_error(self, text: str) -> None:
        """显示 UI/运行错误，并解除当前会话的 busy。"""
        with self._lock:
            sid = self._active_sid or "default"
        self._set_session_active(sid, False)
        self._publish(sid, {"type": "error", "text": text})

    def close(self) -> None:
        """关闭 HTTP 服务并唤醒所有等待者。"""
        if self._closed.is_set():
            return
        self._closed.set()
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.input_queue.put(None)
            with session.consent_lock:
                session.consent_pending = False
                session.consent_event.set()
        self._jobs.put(None)
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()

    def submit_message(
        self,
        text: str,
        sid: str = "default",
        attachments: Sequence[str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """浏览器提交一条用户消息（会话内单 turn，全局串行执行）。

        前置条件：text 或 attachments 至少一个非空；附件必须位于本会话 upload 目录。
        后置条件：成功入队时该会话 busy=True；该会话已有 turn 时返回 409。
        禁止事项：不要在 busy 时覆盖/丢弃已有输入；不要并发执行两个 turn。
        """
        enriched = self._message_with_attachments(text, attachments, sid)
        if enriched is None:
            return HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "error": "message or attachments are invalid/empty",
            }
        session = self._session(sid)
        with self._lock:
            if session.active:
                return HTTPStatus.CONFLICT, {"ok": False, "error": "a turn is already running in this session"}
            session.active = True
        self._publish(sid, {"type": "state", "busy": True})
        session.input_queue.put(enriched)
        self._jobs.put((sid, enriched))
        return HTTPStatus.OK, {"ok": True}

    def cancel_turn(self, sid: str = "default") -> tuple[int, dict[str, Any]]:
        """请求取消当前会话正在执行的 turn。

        前置条件：该会话有 running turn，且构造时注入了 on_cancel。
        后置条件：回调 App.request_cancel；本轮会在下一个协作检查点以 ABORTED 结束。
        禁止事项：不要在这里直接杀进程/线程；不要伪造 busy=False（由 report 复位）。
        """
        session = self._session(sid)
        with self._lock:
            if not session.running:
                return HTTPStatus.CONFLICT, {"ok": False, "error": "no turn is running in this session"}
        if self._on_cancel is None:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "cancel is not wired"}
        try:
            self._on_cancel()
        except Exception as exc:  # 防御性：UI 不应因取消回调异常而崩溃。
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"cancel failed: {exc}"}
        self._publish(sid, {"type": "cancel_requested"})
        return HTTPStatus.OK, {"ok": True}

    def resolve_consent(self, decision: bool, sid: str = "default") -> tuple[int, dict[str, Any]]:
        """浏览器回答当前会话的同意问题。

        前置条件：该会话确实有 pending consent。
        后置条件：唤醒该会话的 ask_consent；重复回答返回 409。
        禁止事项：不要在没有 pending 时伪造同意。
        """
        session = self._session(sid)
        with session.consent_lock:
            if not session.consent_pending:
                return HTTPStatus.CONFLICT, {"ok": False, "error": "no consent request is pending"}
            session.consent_pending = False
            session.consent_result = decision
            session.consent_event.set()
        self._publish(sid, {"type": "consent_done", "decision": decision})
        return HTTPStatus.OK, {"ok": True, "decision": decision}

    def _set_session_active(self, sid: str, active: bool) -> None:
        session = self._session(sid)
        with self._lock:
            session.active = active
            if not active:
                session.running = False
        self._publish(sid, {"type": "state", "busy": active})

    def _publish(self, sid: str, event: dict[str, Any]) -> None:
        session = self._session(sid)
        with self._lock:
            session.events.append(event)
            subscribers = list(session.subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:  # 理论上不会发生（队列无界）；防御性保留。
                pass

    def _subscribe(self, sid: str) -> queue.Queue[dict[str, Any]]:
        session = self._session(sid)
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            for event in session.events:
                subscriber.put_nowait(event)
            session.subscribers.add(subscriber)
        return subscriber

    def _unsubscribe(self, sid: str, subscriber: queue.Queue[dict[str, Any]]) -> None:
        session = self._session(sid)
        with self._lock:
            session.subscribers.discard(subscriber)


class _WebHandler(BaseHTTPRequestHandler):
    """单页 UI 的 HTTP 路由。"""

    server_version = "AgentWeb/1.0"
    protocol_version = "HTTP/1.1"
    _MAX_BODY = 1_000_000

    @property
    def ui(self) -> WebUI:
        return self.server.ui  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        """静默访问日志，避免污染 TUI/CLI 输出。"""
        return

    def do_GET(self) -> None:  # noqa: N802（stdlib 命名）
        path = urlsplit(self.path).path
        if path == "/":
            self._send_bytes(HTTPStatus.OK, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if not self.ui.authorized(self.headers, self.path):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        if path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "ui": "web", "auth": self.ui.auth_enabled})
        elif path == "/events":
            self._stream_events(self.ui.session_id(self.headers, self.path))
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if not self.ui.authorized(self.headers, self.path):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        sid = self.ui.session_id(self.headers, self.path)
        if path == "/message":
            status, payload = self._handle_message(sid)
        elif path == "/consent":
            status, payload = self._handle_consent(sid)
        elif path == "/cancel":
            status, payload = self.ui.cancel_turn(sid)
        elif path == "/upload":
            status, payload = self._handle_upload(sid)
        else:
            status, payload = HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"}
        self._send_json(status, payload)

    def _handle_upload(self, sid: str) -> tuple[int, dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid Content-Length"}
        content_type = self.headers.get("Content-Type", "")
        return self.ui.receive_upload(sid, content_type, length, self.rfile)

    def _handle_message(self, sid: str) -> tuple[int, dict[str, Any]]:
        data = self._read_json()
        if data is None:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"}
        text = data.get("text")
        if not isinstance(text, str):
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "text must be a string"}
        attachments = data.get("attachments", [])
        if attachments is None:
            attachments = []
        if not isinstance(attachments, list) or any(not isinstance(item, str) for item in attachments):
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "attachments must be an array of strings"}
        return self.ui.submit_message(text, sid, attachments)

    def _handle_consent(self, sid: str) -> tuple[int, dict[str, Any]]:
        data = self._read_json()
        if data is None:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"}
        decision = data.get("decision")
        if not isinstance(decision, bool):
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "decision must be a boolean"}
        return self.ui.resolve_consent(decision, sid)

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > self._MAX_BODY:
            return None
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _stream_events(self, sid: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        subscriber = self.ui._subscribe(sid)
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while not self.ui.closed:
                try:
                    event = subscriber.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.ui._unsubscribe(sid, subscriber)

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", 'Bearer realm="agent"')
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Harness</title>
<style>
:root {
  --bg: #0f1419; --panel: #171e24; --line: #2a3640; --text: #d8e2ea;
  --muted: #7d8c99; --accent: #e5a54b; --cyan: #46c6d8; --ok: #5fd38a;
  --err: #f07178; --warn: #e5c07b;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
header { padding: 10px 14px; border-bottom: 1px solid var(--line);
  display: flex; gap: 16px; align-items: baseline; color: var(--accent); }
header strong { letter-spacing: .08em; }
header .meta { color: var(--muted); font-size: 12px; }
main { display: grid; grid-template-columns: minmax(260px, 42%) 1fr;
  grid-template-rows: minmax(110px, 26vh) minmax(52vh, 1fr) auto auto;
  gap: 8px; padding: 8px; height: calc(100vh - 62px); }
.panel { background: var(--panel); border: 1px solid var(--line); min-height: 0;
  display: flex; flex-direction: column; }
.panel h2 { margin: 0; padding: 6px 10px; font-size: 11px; letter-spacing: .1em;
  color: var(--muted); border-bottom: 1px solid var(--line); font-weight: 600; }
.panel .body { padding: 8px 10px; overflow: auto; white-space: pre-wrap; }
#plan { grid-column: 1; grid-row: 1; }
#supervision { grid-column: 2; grid-row: 1; }
#conversation { grid-column: 1 / 3; grid-row: 2; min-height: 52vh; }
#status { grid-column: 1 / 3; grid-row: 3; border: none; background: transparent;
  display: flex; gap: 14px; padding: 0 4px; color: var(--muted); overflow: hidden; }
#status .item { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#composer { display: flex; gap: 8px; padding: 6px 4px; grid-column: 1 / 3;
  grid-row: 4; flex-wrap: wrap; align-items: center; }
#composer.dragover { outline: 1px dashed var(--accent); outline-offset: 2px; }
#composer input { flex: 1; background: #0b1015; border: 1px solid var(--line);
  color: var(--text); padding: 8px 10px; font: inherit; outline: none; }
#composer input:focus { border-color: var(--accent); }
#composer #file { display: none; }
#composer #attach { background: var(--line); color: var(--text); }
#composer button, .modal button { background: var(--accent); color: #1b1305;
  border: 0; padding: 8px 14px; font: inherit; cursor: pointer; }
#composer button:disabled { opacity: .4; cursor: not-allowed; }
#attachments { flex-basis: 100%; display: none; gap: 6px; flex-wrap: wrap; }
#attachments.has { display: flex; }
.attachment { display: flex; align-items: center; gap: 6px; padding: 3px 8px;
  border: 1px solid var(--line); background: #0b1015; color: var(--text); }
.attachment .kind { color: var(--cyan); }
.attachment button { background: transparent; color: var(--err); border: 0;
  padding: 0 2px; cursor: pointer; font: inherit; }
.step { padding: 3px 0; color: var(--text); }
.step .id { color: var(--muted); margin-right: 6px; }
.step.pending { color: var(--muted); }
.step.running { color: var(--cyan); }
.step.ok { color: var(--ok); }
.step.fail { color: var(--err); }
.line { padding: 2px 0; }
.line .kind { color: var(--cyan); margin-right: 6px; }
.line .detail { color: var(--muted); }
.msg { padding: 4px 0; }
.msg.user { color: var(--accent); }
.msg.agent { color: var(--ok); }
.msg.error { color: var(--err); }
.msg pre { background: #0b1015; border: 1px solid var(--line); padding: 8px;
  overflow: auto; color: var(--text); white-space: pre; }
.msg code { background: #0b1015; border: 1px solid var(--line); padding: 0 3px; }
.msg pre code { border: 0; padding: 0; }
.msg h1, .msg h2, .msg h3 { color: var(--text); margin: 6px 0 2px; }
.msg ul { margin: 2px 0; padding-left: 20px; }
.msg a { color: var(--cyan); }
header .ghost { background: transparent; color: var(--muted);
  border: 1px solid var(--line); padding: 3px 8px; font: inherit; cursor: pointer; }
header #new-session { margin-left: auto; }
#token-modal input { width: 100%; background: #0b1015; border: 1px solid var(--line);
  color: var(--text); padding: 8px 10px; font: inherit; outline: none; margin-top: 8px; }
#token-modal input:focus { border-color: var(--accent); }
.modal { position: fixed; inset: 0; background: rgba(0,0,0,.65);
  display: none; align-items: center; justify-content: center; padding: 20px; }
.modal.open { display: flex; }
.modal .card { background: var(--panel); border: 1px solid var(--warn);
  max-width: 720px; width: 100%; padding: 18px; white-space: pre-wrap; }
.modal .card h3 { margin: 0 0 10px; color: var(--warn); font-size: 14px; }
.modal .actions { margin-top: 14px; display: flex; gap: 10px; justify-content: flex-end; }
.modal button.no, #composer button.no { background: var(--line); color: var(--text); }
@media (max-width: 760px) {
  main { grid-template-columns: 1fr; grid-template-rows: auto auto 52vh auto auto; height: auto; }
  #plan { grid-column: 1; grid-row: 1; } #supervision { grid-column: 1; grid-row: 2; }
  #conversation { grid-column: 1; grid-row: 3; min-height: 52vh; }
  #status { grid-column: 1; grid-row: 4; } #composer { grid-column: 1; grid-row: 5; }
}
</style>
</head>
<body>
<header>
  <strong>AGENT</strong>
  <span class="meta" id="turn">turn -</span>
  <span class="meta" id="role">role main</span>
  <span class="meta" id="state">idle</span>
  <button class="ghost" id="new-session" type="button">New</button>
  <button class="ghost" id="token-btn" type="button">Token</button>
</header>
<main>
  <section class="panel" id="plan"><h2>PLAN</h2><div class="body" id="plan-body">(waiting for plan)</div></section>
  <section class="panel" id="supervision"><h2>SUPERVISION</h2><div class="body" id="supervision-body"></div></section>
  <section class="panel" id="conversation"><h2>CONVERSATION / ANSWER</h2><div class="body" id="conversation-body">(no messages yet)</div></section>
  <section id="status">
    <span class="item" id="review">review: -</span>
    <span class="item" id="report">report: -</span>
    <span class="item" id="ledger">ledger: -</span>
    <span class="item">Enter send | /quit exit</span>
  </section>
  <form id="composer">
    <input id="file" type="file" accept="image/*,audio/*,.wav,.mp3,.flac,.ogg,.m4a" multiple hidden>
    <button id="attach" class="no" type="button" title="Attach image or audio">Attach</button>
    <input id="text" autocomplete="off" placeholder="Message the agent…" autofocus>
    <button id="send" type="submit">Send</button>
    <button id="cancel" class="no" type="button" disabled>Cancel</button>
    <div id="attachments"></div>
  </form>
</main>
<div class="modal" id="consent">
  <div class="card">
    <h3>Consent required</h3>
    <div id="consent-question"></div>
    <div class="actions">
      <button class="no" id="consent-no" type="button">Deny</button>
      <button id="consent-yes" type="button">Allow</button>
    </div>
  </div>
</div>
<div class="modal" id="token-modal">
  <div class="card">
    <h3>Access token</h3>
    <div>This Web UI requires a bearer token.</div>
    <input id="token-input" type="password" autocomplete="off" placeholder="AGENT_WEB_TOKEN">
    <div class="actions">
      <button id="token-save" type="button">Save</button>
    </div>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
const planBody = $("plan-body"), supBody = $("supervision-body"), convBody = $("conversation-body");
const steps = new Map();
const seen = new Set();
let busy = false;
let uploading = 0;
let attachments = [];
let token = sessionStorage.getItem("agent-token") || "";
let sessionId = sessionStorage.getItem("agent-session") || "";
if (!sessionId) {
  sessionId = (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2));
  sessionStorage.setItem("agent-session", sessionId);
}
let source = null;

function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (token) headers.set("Authorization", "Bearer " + token);
  if (sessionId) headers.set("X-Agent-Session", sessionId);
  return fetch(path, {...options, headers});
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function renderInline(text) {
  let html = escapeHtml(text);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (match, label, url) => {
    const safe = url.replace(/"/g, "%22");
    return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
  return html;
}

function renderMarkdown(text) {
  const lines = String(text).split("\n");
  let html = "", inCode = false, list = false;
  const code = [];
  const closeList = () => { if (list) { html += "</ul>"; list = false; } };
  for (const line of lines) {
    if (line.startsWith("```")) {
      if (inCode) { html += "<pre><code>" + escapeHtml(code.join("\n")) + "</code></pre>"; code.length = 0; inCode = false; }
      else { closeList(); inCode = true; }
      continue;
    }
    if (inCode) { code.push(line); continue; }
    if (/^\s*[-*]\s+/.test(line)) {
      if (!list) { html += "<ul>"; list = true; }
      html += "<li>" + renderInline(line.replace(/^\s*[-*]\s+/, "")) + "</li>";
      continue;
    }
    closeList();
    if (/^###\s+/.test(line)) html += "<h3>" + renderInline(line.replace(/^###\s+/, "")) + "</h3>";
    else if (/^##\s+/.test(line)) html += "<h2>" + renderInline(line.replace(/^##\s+/, "")) + "</h2>";
    else if (/^#\s+/.test(line)) html += "<h1>" + renderInline(line.replace(/^#\s+/, "")) + "</h1>";
    else html += renderInline(line) + "<br>";
  }
  closeList();
  if (inCode) html += "<pre><code>" + escapeHtml(code.join("\n")) + "</code></pre>";
  return html;
}

function setBusy(value) {
  busy = value;
  $("send").disabled = value;
  $("cancel").disabled = !value;
  $("state").textContent = value ? "working" : "idle";
}

function addLine(text, kind = "", detail = "") {
  if (!supBody.textContent || supBody.textContent === "") supBody.textContent = "";
  const row = document.createElement("div");
  row.className = "line";
  if (kind) {
    const k = document.createElement("span"); k.className = "kind"; k.textContent = kind;
    row.appendChild(k);
  }
  row.appendChild(document.createTextNode(text));
  if (detail) {
    const d = document.createElement("span"); d.className = "detail"; d.textContent = " " + detail;
    row.appendChild(d);
  }
  supBody.appendChild(row);
  while (supBody.childNodes.length > 240) supBody.removeChild(supBody.firstChild);
  supBody.scrollTop = supBody.scrollHeight;
}

function addMessage(speaker, text) {
  const row = document.createElement("div");
  row.className = "msg " + speaker;
  const prefix = speaker === "user" ? "user> " : speaker === "error" ? "error> " : "agent> ";
  if (speaker === "agent") {
    row.innerHTML = escapeHtml(prefix) + renderMarkdown(text);
  } else {
    row.textContent = prefix + text;
  }
  convBody.appendChild(row);
  convBody.scrollTop = convBody.scrollHeight;
}

function renderAttachments() {
  const box = $("attachments");
  box.textContent = "";
  box.classList.toggle("has", attachments.length > 0);
  attachments.forEach((item, index) => {
    const chip = document.createElement("span");
    chip.className = "attachment";
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = item.kind || "file";
    const name = document.createElement("span");
    name.textContent = item.name || item.path || "attachment";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "x";
    remove.title = "Remove attachment";
    remove.addEventListener("click", () => {
      attachments.splice(index, 1);
      renderAttachments();
    });
    chip.append(kind, name, remove);
    box.appendChild(chip);
  });
}

async function uploadFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  uploading += 1;
  $("send").disabled = true;
  try {
    for (const file of list) {
      const form = new FormData();
      form.append("file", file, file.name);
      const response = await apiFetch("/upload", {method: "POST", body: form});
      if (response.status === 401) { showToken(); return; }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        addLine(data.error || "upload failed", "ERROR");
        continue;
      }
      for (const item of (data.files || [])) attachments.push(item);
      for (const err of (data.errors || [])) addLine(err, "ERROR");
      renderAttachments();
    }
  } finally {
    uploading -= 1;
    if (uploading === 0 && !busy) $("send").disabled = false;
  }
}

function renderPlan(payload) {
  planBody.textContent = "";
  steps.clear();
  for (const step of (payload.steps || [])) {
    const row = document.createElement("div");
    row.className = "step pending";
    row.dataset.id = step.step_id;
    const id = document.createElement("span"); id.className = "id"; id.textContent = step.step_id;
    row.appendChild(id);
    row.appendChild(document.createTextNode(`${step.kind} ${step.target || ""} — ${step.why || ""}`));
    planBody.appendChild(row);
    steps.set(step.step_id, row);
  }
}

function stepStatus(stepId, status, text) {
  const row = steps.get(stepId);
  if (!row) return;
  row.className = "step " + status;
  if (text) row.appendChild(document.createTextNode("  " + text));
}

function handleEnvelope(env) {
  const p = env.payload || {}, m = env.meta || {};
  const key = `${env.turn_id}|${env.kind}|${env.message_id}|${m.seq ?? ""}`;
  if (seen.has(key)) return;
  seen.add(key);
  $("turn").textContent = "turn " + env.turn_id;
  if (m.role) $("role").textContent = "role " + m.role;
  switch (env.kind) {
    case "USER_INPUT":
      addMessage("user", p.text || "");
      addLine(p.text || "", "USER_INPUT");
      break;
    case "PLAN":
      renderPlan(p);
      addLine(p.goal || "plan ready", "PLAN", (p.steps || []).length + " step(s)");
      break;
    case "TOOL_CALL":
      stepStatus(m.step_id, "running", `tool:${p.tool}`);
      addLine(`${p.tool} ${JSON.stringify(p.args || {})}`, "TOOL_CALL");
      break;
    case "TOOL_RESULT":
      stepStatus(m.step_id, p.ok ? "ok" : "fail", p.ok ? "ok" : (p.error && p.error.code) || "failed");
      addLine(p.ok ? "ok" : "failed", "TOOL_RESULT", p.ok ? "" : ((p.error && p.error.message) || ""));
      break;
    case "REVIEW":
      $("review").textContent = `review: ${p.verdict || "-"} score=${Number(p.score || 0).toFixed(2)} issues=${(p.issues || []).length}`;
      addLine(`${p.verdict || "-"} score=${Number(p.score || 0).toFixed(2)}`, "REVIEW");
      break;
    case "REFLECTION":
      addLine(`${(p.lessons || []).length} lesson(s)`, "REFLECTION");
      break;
    case "CONSENT_REQUEST":
      addLine(p.decision ? "allowed" : "denied", "CONSENT", p.question || "");
      break;
    case "ROLE_SWITCH":
      $("role").textContent = "role " + (p.to || "-");
      addLine(`${p.from || "?"} -> ${p.to || "?"}`, "ROLE_SWITCH", p.alias || "");
      break;
    case "ANSWER":
      addMessage("agent", p.text || "");
      addLine("answer ready", "ANSWER");
      break;
    case "RUN_REPORT":
      $("report").textContent = `report: ${Number(p.duration_s || 0).toFixed(1)}s retries=${p.retries || 0} failures=${(p.failures || []).length}`;
      addLine("run complete", "RUN_REPORT");
      setBusy(false);
      break;
    case "ERROR":
      addMessage("error", `${p.code || "ERROR"}: ${p.message || ""}`);
      addLine(`${p.code || "ERROR"} ${p.message || ""}`, "ERROR");
      setBusy(false);
      break;
    default:
      addLine(env.kind, env.kind);
  }
}

function showConsent(question) {
  $("consent-question").textContent = question || "";
  $("consent").classList.add("open");
}

function hideConsent() { $("consent").classList.remove("open"); }

function showToken() {
  $("token-input").value = token;
  $("token-modal").classList.add("open");
  $("token-input").focus();
}

function hideToken() { $("token-modal").classList.remove("open"); }

function saveToken() {
  token = $("token-input").value.trim();
  if (token) sessionStorage.setItem("agent-token", token);
  else sessionStorage.removeItem("agent-token");
  hideToken();
  checkAuth();
}

$("new-session").addEventListener("click", () => {
  sessionStorage.removeItem("agent-session");
  location.reload();
});
$("token-btn").addEventListener("click", showToken);
$("token-save").addEventListener("click", saveToken);
$("token-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); saveToken(); }
});

async function answerConsent(decision) {
  hideConsent();
  const response = await apiFetch("/consent", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({decision})});
  if (response.status === 401) showToken();
}

$("consent-yes").addEventListener("click", () => answerConsent(true));
$("consent-no").addEventListener("click", () => answerConsent(false));

$("cancel").addEventListener("click", async () => {
  $("state").textContent = "cancelling…";
  const response = await apiFetch("/cancel", {method: "POST"});
  if (response.status === 401) { showToken(); return; }
  if (!response.ok) {
    $("state").textContent = "working";
    const data = await response.json().catch(() => ({}));
    addLine(data.error || "cancel failed", "ERROR");
  }
});

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("text");
  const text = input.value.trim();
  if ((!text && attachments.length === 0) || busy || uploading > 0) return;
  const outgoing = attachments.map((item) => item.path);
  const pendingText = text;
  setBusy(true);
  input.value = "";
  const response = await apiFetch("/message", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({text, attachments: outgoing})});
  if (!response.ok) {
    setBusy(false);
    input.value = pendingText;
    if (response.status === 401) { showToken(); return; }
    const data = await response.json().catch(() => ({}));
    addLine(data.error || "send failed", "ERROR");
    return;
  }
  attachments = [];
  renderAttachments();
});

$("attach").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", async (event) => {
  await uploadFiles(event.target.files);
  event.target.value = "";
});
const composer = $("composer");
["dragenter", "dragover"].forEach((name) => {
  composer.addEventListener(name, (event) => {
    event.preventDefault();
    composer.classList.add("dragover");
  });
});
["dragleave", "drop"].forEach((name) => {
  composer.addEventListener(name, (event) => {
    event.preventDefault();
    composer.classList.remove("dragover");
  });
});
composer.addEventListener("drop", async (event) => {
  await uploadFiles(event.dataTransfer.files);
});

function connectEvents() {
  if (source) source.close();
  const params = new URLSearchParams();
  params.set("session", sessionId);
  if (token) params.set("token", token);
  const url = "/events?" + params.toString();
  source = new EventSource(url);
  source.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.type === "envelope") handleEnvelope(msg.envelope);
    else if (msg.type === "report") {
      if (msg.ledger) $("ledger").textContent = "ledger: " + msg.ledger;
      setBusy(false);
    } else if (msg.type === "state") setBusy(Boolean(msg.busy));
    else if (msg.type === "consent") showConsent(msg.question);
    else if (msg.type === "consent_done") hideConsent();
    else if (msg.type === "session_closed") {
      addMessage("error", "session closed; use the New button to start another");
      setBusy(false);
      if (source) source.close();
    }
    else if (msg.type === "cancel_requested") {
      addLine("cancel requested", "CANCEL");
      $("state").textContent = "cancelling…";
    }
    else if (msg.type === "error") addMessage("error", msg.text || "");
  };
  source.onerror = () => addLine("SSE disconnected; retrying…", "SSE");
}

async function checkAuth() {
  if (source) { source.close(); source = null; }
  try {
    const response = await apiFetch("/health");
    if (response.status === 401) { showToken(); return; }
    hideToken();
    connectEvents();
  } catch {
    addLine("cannot reach server", "SSE");
  }
}

checkAuth();
</script>
</body>
</html>
"""
