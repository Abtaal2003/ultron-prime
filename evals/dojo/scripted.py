"""A scripted *gullible* model for deterministic AgentDojo runs.

It does the user's task by replaying the task's ground truth, and the moment any tool output
shows it the injection goal it obeys completely: the injection task's ground truth runs next. All
that stands between such a model and a successful attack is the pipeline around it, so the
resulting attack success rate measures the structural defences alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.base_tasks import BaseInjectionTask, BaseUserTask
from agentdojo.functions_runtime import EmptyEnv, Env, FunctionCall, FunctionsRuntime
from agentdojo.types import (
    ChatAssistantMessage,
    ChatMessage,
    get_text_content_as_str,
    text_content_block_from_string,
)

Query = tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict[str, Any]]


def _squash(text: str) -> str:
    """Lower-case and keep only alphanumerics, so YAML line folding and quoting cannot hide text."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


class GullibleLLM(BasePipelineElement):
    """Replays the user task, then obeys any injection it reads."""

    # AgentDojo's attacks address the model by name; any registered model id works.
    name = "gullible-claude-3-opus-20240229"

    def __init__(
        self,
        user_task: BaseUserTask[Any],
        injection_task: BaseInjectionTask[Any] | None = None,
    ) -> None:
        self.user_task = user_task
        self.injection_task = injection_task
        self.queue: list[FunctionCall] | None = None
        self.hijacked = False

    def _saw_injection(self, messages: Sequence[ChatMessage]) -> bool:
        if self.injection_task is None:
            return False
        goal = _squash(self.injection_task.GOAL)
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                break
            if msg["role"] == "tool" and goal in _squash(get_text_content_as_str(msg["content"])):
                return True
        return False

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict[str, Any] = {},
    ) -> Query:
        """Emit the next scripted tool call, or the final answer when the script is done."""
        if self.queue is None:
            self.queue = list(self.user_task.ground_truth(env))
        if not self.hijacked and self.injection_task is not None and self._saw_injection(messages):
            self.hijacked = True
            self.queue = [*self.injection_task.ground_truth(env), *self.queue]
            extra_args["ultron_hijacked"] = True
        if self.queue:
            call = self.queue.pop(0)
            reply = ChatAssistantMessage(
                role="assistant",
                content=[text_content_block_from_string("")],
                tool_calls=[call],
            )
        else:
            reply = ChatAssistantMessage(
                role="assistant",
                content=[text_content_block_from_string(self.user_task.GROUND_TRUTH_OUTPUT)],
                tool_calls=None,
            )
        return query, runtime, env, [*messages, reply], extra_args
