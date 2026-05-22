from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext


class _BaseHNUUtilityTool(FunctionTool[AstrAgentContext]):
    plugin: Any = None


@pydantic_dataclass
class HNUUtilityBalanceQueryTool(_BaseHNUUtilityTool):
    name: str = "query_hnu_utility_balance"
    description: str = "查询海南大学水电费余额，返回热水、照明、空调和水表余额。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        plugin = self.plugin
        if not plugin:
            return "查询水电费余额失败，请在消息上下文中重试。"

        try:
            return await plugin.query_hnu_utility_balance()
        except Exception:  # noqa: BLE001
            logger.exception("HNUUtilityBalance LLM 工具查询失败。")
            return "查询水电费余额失败，请稍后重试。"


def build_llm_tools(plugin) -> list[FunctionTool[AstrAgentContext]]:
    tools: list[FunctionTool[AstrAgentContext]] = [HNUUtilityBalanceQueryTool()]
    for tool in tools:
        tool.plugin = plugin
    return tools
