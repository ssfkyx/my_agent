"""redact — 外发前的确定性脱敏规则（纯函数）。

Module: agent.core.redact
Responsibility: 在升级请求越过本机边界之前，按固定规则移除私钥、令牌、口令、
    邮箱、绝对路径与长十六进制串；只处理字符串，数字/布尔原样保留。
Depends on: stdlib (re, typing) only
Depended by: core.escalation
ADRs: ADR-022（脱敏规则）, ADR-021（升级必须先脱敏）
"""

import re
from typing import Any, Mapping

# 规则按“先整体、后局部”的顺序执行；每条都用类型标记替换，便于远端理解。
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|"
    r"AKIA[0-9A-Z]{12,}|xox[baprs]-[A-Za-z0-9\-]{8,})\b"
)
_ASSIGNMENT = re.compile(
    r"(?i)(['\"]?)(\b(?:api[_-]?key|access[_-]?token|token|password|passwd|secret)\b)"
    r"(['\"]?)"
    r"(\s*[:=]\s*)"
    r"(['\"]?)"
    r"([^\s'\",;]+)"
    r"\5"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# 绝对路径：前面不能是单词字符、点、横线、冒号或斜杠，避免误伤 URL。
_ABS_PATH = re.compile(r"(?<![\w.\-:/])/(?:[^\s/\"'`]+/)*[^\s/\"'`]+")
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")

# 家目录/root 下的路径只保留文件名，避免把用户名或私有目录结构带出去。
_HOME_ROOTS = frozenset({"home", "users", "root"})


class Redactor:
    """一组顺序执行、可审计的脱敏规则（ADR-022）。

    前置条件：无。
    后置条件：同一输入恒产生同一输出；不读取环境或文件。
    禁止事项：不要用模型代替本类（模型会漏，规则不会）；不要提供“关闭脱敏”开关。
    """

    def redact_text(self, text: str) -> str:
        """对单个字符串应用全部规则。

        前置条件：text 为 str。
        后置条件：返回脱敏后的新字符串；六类敏感样例均被类型标记替换。
        禁止事项：不要删除整段文本（空串会让远端误以为用户没说话）。
        异常：无。
        """
        redacted = _PRIVATE_KEY.sub("<private-key>", text)
        redacted = _TOKEN.sub("<secret>", redacted)
        redacted = _ASSIGNMENT.sub(_replace_assignment, redacted)
        redacted = _EMAIL.sub("<email>", redacted)
        redacted = _ABS_PATH.sub(_replace_path, redacted)
        redacted = _LONG_HEX.sub("<hex>", redacted)
        return redacted

    def redact_payload(self, payload: Any) -> Any:
        """递归脱敏 dict/list 中的字符串值。

        前置条件：payload 为 JSON 可序列化结构。
        后置条件：返回同构的新对象；dict 键不变，数字与布尔原样保留。
        禁止事项：不要修改入参（调用方可能还要用原文做本地审计）。
        异常：无。
        """
        if isinstance(payload, str):
            return self.redact_text(payload)
        if isinstance(payload, Mapping):
            return {key: self.redact_payload(value) for key, value in payload.items()}
        if isinstance(payload, list):
            return [self.redact_payload(value) for value in payload]
        if isinstance(payload, tuple):
            return tuple(self.redact_payload(value) for value in payload)
        return payload

    def redact(self, value: Any) -> Any:
        """redact_payload 的别名，便于按“值”而不是“payload”称呼。

        前置条件：同 redact_payload。
        后置条件：同 redact_payload。
        禁止事项：不要用它处理需要保留原始引用的对象。
        异常：无。
        """
        return self.redact_payload(value)

    def classify(self, text: str) -> dict[str, int]:
        """统计文本中各类敏感模式的出现次数（用于审计与测试）。

        前置条件：text 为 str。
        后置条件：返回固定键 private_key/secret/email/path/hex 的计数。
        禁止事项：不要把计数结果当作“已脱敏”的证明（它只看原文）。
        异常：无。
        """
        tokens = len(_TOKEN.findall(text))
        assignments = len(_ASSIGNMENT.findall(text))
        return {
            "private_key": len(_PRIVATE_KEY.findall(text)),
            "secret": tokens + assignments,
            "email": len(_EMAIL.findall(text)),
            "path": len(_ABS_PATH.findall(text)),
            "hex": len(_LONG_HEX.findall(text)),
        }


def _replace_assignment(match: re.Match[str]) -> str:
    """保留敏感字段名与分隔符，只遮蔽值。

    前置条件：match.group(2) 为键名，group(4) 为分隔符，group(5) 为值引号。
    后置条件：返回 `key = <secret>` 或 `"key": "<secret>"` 形状。
    禁止事项：不要连键名一起删除（远端需要知道这是口令类失败）。
    异常：无。
    """
    quote = match.group(5)
    return f"{match.group(1)}{match.group(2)}{match.group(3)}{match.group(4)}{quote}<secret>{quote}"


def _replace_path(match: re.Match[str]) -> str:
    """把绝对路径前段替换为 <path>，保留可理解的文件/末级目录。

    前置条件：match.group(0) 以 / 开头。
    后置条件：返回 `<path>/...`；家目录下的路径不保留用户名。
    禁止事项：不要返回包含 `/home/<user>` 前缀的文本。
    异常：无。
    """
    parts = [part for part in match.group(0).split("/") if part]
    if not parts:
        return "<path>"
    if parts[0].casefold() in _HOME_ROOTS:
        # /home/<user> 本身不保留用户名；更深路径只保留文件名。
        if len(parts) <= 2:
            return "<path>"
        return f"<path>/{parts[-1]}"
    if len(parts) >= 3:
        return f"<path>/{parts[-2]}/{parts[-1]}"
    return f"<path>/{parts[-1]}"
