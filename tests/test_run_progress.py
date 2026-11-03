"""Progress recorded when a delegated run stops before finishing."""
from __future__ import annotations

import time
import unittest

from deepseek_mcp.agent_loop import AgentLoopError
from deepseek_mcp.run_progress import attach_progress, progress_of


class _Mutations:
    def __init__(self, records: list[str] | None = None) -> None:
        self.records = list(records or ())

    def payload(self) -> list[dict[str, str]]:
        if not self.records:
            return []
        return [
            {"transaction_id": "a" * 32, "tool": "Write", "status": "committed"}
        ]


class _State:
    def __init__(self) -> None:
        self.messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "planning"},
            {"role": "tool", "content": "output"},
            {"role": "assistant", "content": "done part"},
        ]
        self.tool_calls = 7
        self.prompt_tokens = 500
        self.completion_tokens = 40
        self.started = time.time() - 12
        self.mutations = _Mutations()


class RunProgressTests(unittest.TestCase):
    def test_progress_reuses_the_keys_a_completed_result_reports(self) -> None:
        progress = progress_of(_State())
        self.assertEqual(progress["turns_used"], 2)
        self.assertEqual(progress["tool_calls"], 7)
        self.assertEqual(
            progress["tokens"], {"prompt": 500, "completion": 40, "total": 540}
        )
        self.assertGreater(progress["duration_seconds"], 11.0)

    def test_a_bare_state_still_produces_zeroed_progress(self) -> None:
        progress = progress_of(object())
        self.assertEqual(progress["turns_used"], 0)
        self.assertEqual(progress["mutations"], [])

    def test_attaching_leaves_the_failure_type_and_message_intact(self) -> None:
        error = AgentLoopError("DeepSeek client error on turn 4: category=client")
        state = _State()
        state.mutations = _Mutations(["committed"])

        attach_progress(error, state)

        self.assertEqual(
            str(error), "DeepSeek client error on turn 4: category=client"
        )
        self.assertEqual(error.progress["mutations"][0]["tool"], "Write")

    def test_progress_already_on_the_failure_is_not_replaced(self) -> None:
        error = AgentLoopError("stopped")
        error.progress = {"turns_used": 1}
        attach_progress(error, _State())
        self.assertEqual(error.progress, {"turns_used": 1})

    def test_a_base_exception_is_left_alone(self) -> None:
        attach_progress(KeyboardInterrupt(), _State())


if __name__ == "__main__":
    unittest.main()
