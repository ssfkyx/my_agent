"""registry — 工具注册表：主 agent 的工具目录来源。

Module: agent.tools.registry
Responsibility: 注册/查找 Tool，并向 planner 暴露只含 ToolSpec 的目录。
Depends on: stdlib (typing) + agent.core.protocol + agent.ports
Depended by: core.harness, app
ADRs: ADR-001（ToolSpec 形状）, ADR-005（未知工具以 NOT_FOUND 结果表达）, ADR-006（TB-0 只注册 echo）
"""

from collections.abc import Iterable

from agent.core.protocol import ToolSpec
from agent.ports import Tool


class ToolRegistryError(RuntimeError):
    """注册表自身被误用（重复注册、非法名字）。

    前置条件：无。
    后置条件：异常信息指出冲突的工具名。
    禁止事项：不要用它表达"工具执行失败"。
    异常来源：ToolRegistry.register。
    """


class ToolNotFoundError(KeyError):
    """请求的工具不在注册表中。

    前置条件：无。
    后置条件：异常信息含被查找的名字。
    禁止事项：不要把 KeyError 原样抛给用户（harness 会转成 NOT_FOUND 结果）。
    异常来源：ToolRegistry.resolve。
    """


class ToolRegistry:
    """按名字索引的工具集合。

    前置条件：注册的 Tool.spec.name 唯一。
    后置条件：catalog 顺序稳定（按注册顺序），便于提示词缓存命中。
    禁止事项：禁止运行期动态删除工具（会让历史计划无法重放）。
    """

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """注册一个工具。

        前置条件：tool.spec 已通过校验；名字未被占用。
        后置条件：注册后可被 resolve 与 catalog 看到。
        禁止事项：不要覆盖同名工具（会造成"同一计划两次执行结果不同"）。
        异常：ToolRegistryError（重名）。
        """
        name = tool.spec.name
        if name in self._tools:
            raise ToolRegistryError(f"tool already registered: {name}")
        self._tools[name] = tool

    def resolve(self, name: str) -> Tool:
        """按名字取工具。

        前置条件：name 来自计划中的 step.target。
        后置条件：返回对应 Tool 实例。
        禁止事项：不要返回 None 或替身对象。
        异常：ToolNotFoundError（未注册）。
        """
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def catalog(self) -> tuple[ToolSpec, ...]:
        """返回工具目录（只含描述，不含实现）。

        前置条件：无。
        后置条件：返回元组，顺序等于注册顺序。
        禁止事项：不要在此过滤 side_effect 工具（过滤属于 guardrail 的职责）。
        异常：无。
        """
        return tuple(tool.spec for tool in self._tools.values())
