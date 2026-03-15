from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from opencompany.models import AgentNode


AskAgentFn = Callable[[AgentNode], Awaitable[list[dict[str, Any]]]]
ExecuteActionsFn = Callable[[AgentNode, list[dict[str, Any]]], Awaitable["ActionBatchResult"]]
RequestForcedFinishFn = Callable[[AgentNode], Awaitable[dict[str, Any] | None]]
InterruptedFn = Callable[[], bool]


@dataclass(slots=True)
class ActionBatchResult:
    finish_payload: dict[str, Any] | None = None


@dataclass(slots=True)
class AgentLoopResult:
    finish_payload: dict[str, Any] | None
    interrupted: bool = False
    step_limit_reached: bool = False


class AgentLoopRunner:
    """Runs one reusable think-act-feedback loop for any agent role."""

    def __init__(self, *, max_steps: int) -> None:
        self.max_steps = max(1, int(max_steps))

    async def run(
        self,
        *,
        agent: AgentNode,
        ask_agent: AskAgentFn,
        execute_actions: ExecuteActionsFn,
        request_forced_finish: RequestForcedFinishFn,
        interrupted: InterruptedFn,
    ) -> AgentLoopResult:
        for _ in range(self.max_steps):
            if interrupted():
                return AgentLoopResult(finish_payload=None, interrupted=True)
            actions = await ask_agent(agent)
            if interrupted():
                return AgentLoopResult(finish_payload=None, interrupted=True)
            result = await execute_actions(agent, actions)
            if result.finish_payload is not None:
                return AgentLoopResult(finish_payload=result.finish_payload)
            # Let background worker/tool tasks advance between consecutive loop turns.
            await asyncio.sleep(0)
        forced_finish = await request_forced_finish(agent)
        return AgentLoopResult(
            finish_payload=forced_finish,
            step_limit_reached=True,
        )
