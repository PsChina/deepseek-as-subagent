from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.agent_loop import AgentLoopCancelled
from deepseek_mcp.config import Config
from deepseek_mcp.job_manager import (
    MAX_CONTEXT_BYTES,
    MAX_COMBINED_TASK_BYTES,
    MAX_QUEUED_MESSAGES,
    MAX_QUEUED_MESSAGE_BYTES,
    MAX_RETAINED_JOBS,
    MAX_STEERING_MESSAGE_BYTES,
    MAX_TASK_BYTES,
    DeepSeekJobManager,
    JobBusy,
    JobError,
    JobRecord,
)


def _result(message: str = "done") -> dict:
    return {
        "final_message": message,
        "turns_used": 1,
        "tokens": {"prompt": 1, "completion": 1, "total": 2},
        "tool_calls": 0,
        "duration_seconds": 0.01,
    }


class JobManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        root = Path(self._temporary_directory.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.lock_directory = root / "locks"

    def _manager(self) -> DeepSeekJobManager:
        return DeepSeekJobManager(lock_directory=self.lock_directory)

    def _config(self, workspace: Path | None = None) -> Config:
        return Config(api_key="sk-test", workspace=workspace or self.workspace)

    def _assert_terminal(self, manager: DeepSeekJobManager, job_id: str) -> dict:
        self.assertTrue(manager.wait_for_terminal(job_id, 2.0))
        return manager.status(job_id)

    def _seed_terminal_jobs(self, manager: DeepSeekJobManager) -> list[str]:
        job_ids = []
        for index in range(MAX_RETAINED_JOBS):
            job_id = f"retained-{index}"
            manager._jobs[job_id] = JobRecord(
                job_id=job_id,
                task="",
                context="",
                task_length=index,
                status="completed",
                finished_at=float(index),
                result=_result(job_id),
            )
            job_ids.append(job_id)
        return job_ids

    def _assert_results_retained(
        self,
        manager: DeepSeekJobManager,
        job_ids: list[str],
    ) -> None:
        for job_id in job_ids:
            payload = manager.result(job_id)
            self.assertTrue(payload["ready"], job_id)
            self.assertEqual(payload["result"]["final_message"], job_id)

    def test_background_job_accepts_steering_and_completes(self) -> None:
        manager = self._manager()
        started = threading.Event()
        allow_poll = threading.Event()
        got_message = threading.Event()
        captured: list[str] = []

        def fake_run_agent(task, config, **kwargs):
            started.set()
            if not allow_poll.wait(2.0):
                raise AssertionError("test did not release steering poll")
            captured.extend(kwargs["control_poll"]())
            got_message.set()
            kwargs["control_finalize"]()
            return _result()

        with patch("deepseek_mcp.job_manager.run_agent", side_effect=fake_run_agent):
            job = manager.start("test task", "", self._config())
            self.assertTrue(started.wait(1.0))
            queued = manager.send_message(job["job_id"], "change direction")
            self.assertTrue(queued["message_queued"])
            allow_poll.set()
            self.assertTrue(got_message.wait(1.0))
            self._assert_terminal(manager, job["job_id"])

        self.assertEqual(captured, ["change direction"])
        result = manager.result(job["job_id"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["result"]["final_message"], "done")

        usage = manager.claim_usage_record(job["job_id"])
        self.assertIsNotNone(usage)
        assert usage is not None
        task_length, usage_result = usage
        self.assertEqual(task_length, len("test task"))
        self.assertEqual(usage_result["tokens"]["total"], 2)
        self.assertIsNone(manager.claim_usage_record(job["job_id"]))
        manager.finish_usage_record(job["job_id"], False)
        self.assertIsNotNone(manager.claim_usage_record(job["job_id"]))
        manager.finish_usage_record(job["job_id"], True)
        self.assertIsNone(manager.claim_usage_record(job["job_id"]))

    def test_cancel_is_cooperative_and_reaches_cancelled(self) -> None:
        manager = self._manager()
        started = threading.Event()

        def fake_run_agent(task, config, **kwargs):
            started.set()
            if kwargs["cancel_signal"].wait(2.0):
                raise AgentLoopCancelled("cancelled in test")
            return _result("unexpected")

        with patch("deepseek_mcp.job_manager.run_agent", side_effect=fake_run_agent):
            job = manager.start("long task", "", self._config())
            self.assertTrue(started.wait(1.0))
            manager.send_message(job["job_id"], "private queued direction")
            response = manager.cancel(job["job_id"])
            self.assertTrue(response["cancel_accepted"])
            self.assertTrue(response["cancel_requested"])
            status = self._assert_terminal(manager, job["job_id"])

        self.assertEqual(status["status"], "cancelled")
        self.assertFalse(status["accepting_messages"])
        self.assertEqual(status["queued_messages"], 0)

    def test_unexpected_failure_does_not_expose_raw_exception_text(self) -> None:
        manager = self._manager()
        marker = "private-provider-body"
        with (
            patch(
                "deepseek_mcp.job_manager.run_agent",
                side_effect=RuntimeError(marker),
            ),
            patch("deepseek_mcp.job_manager.logger.error") as logged,
        ):
            job = manager.start("failure", "", self._config())
            status = self._assert_terminal(manager, job["job_id"])

        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "unexpected internal failure")
        self.assertNotIn(marker, str(logged.call_args_list))

    def test_cancel_accepted_before_final_commit_wins_atomically(self) -> None:
        manager = self._manager()
        ready_to_return = threading.Event()
        release_return = threading.Event()

        def fake_run_agent(task, config, **kwargs):
            ready_to_return.set()
            if not release_return.wait(2.0):
                raise AssertionError("test did not release final response")
            return _result()

        with patch("deepseek_mcp.job_manager.run_agent", side_effect=fake_run_agent):
            job = manager.start("final race", "", self._config())
            self.assertTrue(ready_to_return.wait(1.0))
            cancelled = manager.cancel(job["job_id"])
            self.assertTrue(cancelled["cancel_accepted"])
            release_return.set()
            status = self._assert_terminal(manager, job["job_id"])

        self.assertEqual(status["status"], "cancelled")
        self.assertTrue(status["cancel_requested"])
        self.assertIsNone(manager.result(job["job_id"])["result"])

    def test_late_cancel_preserves_completed_mutation_transactions(self) -> None:
        manager = self._manager()
        ready_to_return = threading.Event()
        release_return = threading.Event()
        result = _result()
        result["mutations"] = [{
            "transaction_id": "a" * 32,
            "tool": "NotebookEdit",
            "status": "committed",
        }]

        def fake_run_agent(task, config, **kwargs):
            ready_to_return.set()
            if not release_return.wait(2.0):
                raise AssertionError("test did not release mutation result")
            return result

        with patch("deepseek_mcp.job_manager.run_agent", side_effect=fake_run_agent):
            job = manager.start("mutation race", "", self._config())
            self.assertTrue(ready_to_return.wait(1.0))
            manager.cancel(job["job_id"])
            release_return.set()
            status = self._assert_terminal(manager, job["job_id"])

        self.assertEqual(status["status"], "cancelled")
        self.assertIn("DO NOT RETRY", status["error"])
        self.assertIn("a" * 32, status["error"])

    def test_cancel_after_terminal_is_not_accepted(self) -> None:
        manager = self._manager()

        with patch("deepseek_mcp.job_manager.run_agent", return_value=_result()):
            job = manager.start("quick", "", self._config())
            self._assert_terminal(manager, job["job_id"])

        response = manager.cancel(job["job_id"])
        self.assertFalse(response["cancel_accepted"])
        self.assertFalse(response["cancel_requested"])
        self.assertEqual(response["status"], "completed")

    def test_only_one_background_job_can_run(self) -> None:
        manager = self._manager()
        started = threading.Event()
        release = threading.Event()

        def fake_run_agent(task, config, **kwargs):
            started.set()
            if not release.wait(2.0):
                raise AssertionError("test did not release job")
            return _result()

        with patch("deepseek_mcp.job_manager.run_agent", side_effect=fake_run_agent):
            first = manager.start("first", "", self._config())
            self.assertTrue(started.wait(1.0))
            with self.assertRaises(JobBusy):
                manager.start("second", "", self._config())
            with self.assertRaises(JobBusy):
                manager.run_sync("sync", self._config())
            release.set()
            self._assert_terminal(manager, first["job_id"])

    def test_second_manager_cannot_run_same_workspace(self) -> None:
        first_manager = self._manager()
        second_manager = self._manager()
        started = threading.Event()
        release = threading.Event()

        def fake_run_agent(task, config, **kwargs):
            started.set()
            if not release.wait(2.0):
                raise AssertionError("test did not release job")
            return _result()

        with patch("deepseek_mcp.job_manager.run_agent", side_effect=fake_run_agent):
            first = first_manager.start("first", "", self._config())
            self.assertTrue(started.wait(1.0))
            with self.assertRaises(JobBusy):
                second_manager.run_sync("same workspace", self._config())
            release.set()
            self._assert_terminal(first_manager, first["job_id"])

    def test_sync_execution_blocks_background_start(self) -> None:
        manager = self._manager()
        started = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def fake_run_agent(task, config, **kwargs):
            started.set()
            if not release.wait(2.0):
                raise AssertionError("test did not release sync run")
            return _result()

        def run_sync() -> None:
            try:
                manager.run_sync("sync", self._config())
            except BaseException as error:
                errors.append(error)

        with patch("deepseek_mcp.job_manager.run_agent", side_effect=fake_run_agent):
            thread = threading.Thread(target=run_sync)
            thread.start()
            self.assertTrue(started.wait(1.0))
            with self.assertRaises(JobBusy):
                manager.start("background", "", self._config())
            release.set()
            thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_busy_start_does_not_prune_terminal_results(self) -> None:
        manager = self._manager()
        retained = self._seed_terminal_jobs(manager)
        active = JobRecord(
            job_id="active",
            task="active",
            context="",
            task_length=6,
            status="running",
        )
        manager._jobs[active.job_id] = active
        manager._active_job_id = active.job_id

        with self.assertRaises(JobBusy):
            manager.start("rejected", "", self._config())

        self._assert_results_retained(manager, retained)

    def test_busy_workspace_lease_does_not_prune_terminal_results(self) -> None:
        manager = self._manager()
        retained = self._seed_terminal_jobs(manager)

        with patch.object(
            manager,
            "_acquire_workspace_lease_locked",
            side_effect=JobBusy("workspace busy"),
        ):
            with self.assertRaises(JobBusy):
                manager.start("rejected", "", self._config())

        self._assert_results_retained(manager, retained)

    def test_result_and_usage_claim_are_atomic_against_pruning_start(self) -> None:
        manager = self._manager()
        retained = self._seed_terminal_jobs(manager)
        oldest = retained[0]
        payload_started = threading.Event()
        release_payload = threading.Event()
        collected = []
        start_results = []
        original_payload = manager._result_payload_locked

        def delayed_payload(job: JobRecord) -> dict:
            payload = original_payload(job)
            payload_started.set()
            if not release_payload.wait(2):
                raise AssertionError("test did not release result claim")
            return payload

        def collect() -> None:
            collected.append(manager.result_with_usage_claim(oldest))

        def start_new() -> None:
            start_results.append(manager.start("new", "", self._config()))

        with (
            patch.object(manager, "_result_payload_locked", side_effect=delayed_payload),
            patch("deepseek_mcp.job_manager.run_agent", return_value=_result("new")),
        ):
            collector = threading.Thread(target=collect)
            starter = threading.Thread(target=start_new)
            collector.start()
            self.assertTrue(payload_started.wait(1))
            starter.start()
            release_payload.set()
            collector.join(2)
            starter.join(2)
            self._assert_terminal(manager, start_results[0]["job_id"])

        self.assertFalse(collector.is_alive())
        self.assertFalse(starter.is_alive())
        payload, usage = collected[0]
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["result"]["final_message"], oldest)
        self.assertIsNotNone(usage)
        self.assertEqual(len(start_results), 1)

    def test_thread_start_failure_rolls_back_job_and_workspace_lease(self) -> None:
        manager = self._manager()
        retained = self._seed_terminal_jobs(manager)

        with patch.object(
            threading.Thread,
            "start",
            side_effect=RuntimeError("thread unavailable"),
        ):
            with self.assertRaisesRegex(JobError, "failed to start"):
                manager.start("first", "", self._config())

        self._assert_results_retained(manager, retained)

        with patch("deepseek_mcp.job_manager.run_agent", return_value=_result()):
            second = manager.start("second", "", self._config())
            status = self._assert_terminal(manager, second["job_id"])

        self.assertEqual(status["status"], "completed")

    def test_finalize_closes_mailbox_atomically(self) -> None:
        job = JobRecord(job_id="abc", task="x", context="", task_length=1)
        self.assertEqual(job.drain_messages(finalize_if_empty=True), [])
        with self.assertRaises(JobError):
            job.queue_message("too late")

    def test_task_context_and_sync_inputs_are_bounded(self) -> None:
        manager = self._manager()
        with self.assertRaisesRegex(JobError, "task exceeds"):
            manager.start("x" * (MAX_TASK_BYTES + 1), "", self._config())
        with self.assertRaisesRegex(JobError, "context exceeds"):
            manager.start("task", "x" * (MAX_CONTEXT_BYTES + 1), self._config())
        with self.assertRaisesRegex(JobError, "task exceeds"):
            manager.run_sync("x" * (MAX_COMBINED_TASK_BYTES + 1), self._config())

    def test_steering_queue_count_and_byte_limits_are_atomic(self) -> None:
        job = JobRecord(job_id="bounded", task="x", context="", task_length=1)
        for _ in range(MAX_QUEUED_MESSAGES):
            job.queue_message("x")
        with self.assertRaisesRegex(JobError, "too many"):
            job.queue_message("one too many")
        self.assertEqual(job.snapshot()["queued_messages"], MAX_QUEUED_MESSAGES)

        byte_limited = JobRecord(
            job_id="byte-bounded", task="x", context="", task_length=1
        )
        chunk = "x" * MAX_STEERING_MESSAGE_BYTES
        for _ in range(MAX_QUEUED_MESSAGE_BYTES // MAX_STEERING_MESSAGE_BYTES):
            byte_limited.queue_message(chunk)
        with self.assertRaisesRegex(JobError, "byte limit"):
            byte_limited.queue_message("x")
        self.assertEqual(
            byte_limited.snapshot()["queued_messages"],
            MAX_QUEUED_MESSAGE_BYTES // MAX_STEERING_MESSAGE_BYTES,
        )

    def test_single_steering_message_limit_and_close_clears_content(self) -> None:
        job = JobRecord(job_id="single", task="x", context="", task_length=1)
        with self.assertRaisesRegex(JobError, "message exceeds"):
            job.queue_message("x" * (MAX_STEERING_MESSAGE_BYTES + 1))
        job.queue_message("private")
        job.close_messages()
        self.assertEqual(job.snapshot()["queued_messages"], 0)
        self.assertEqual(job._queued_message_bytes, 0)


if __name__ == "__main__":
    unittest.main()
