from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.agent_loop import AgentLoopCancelled
from deepseek_mcp.config import Config
from deepseek_mcp.job_manager import DeepSeekJobManager, JobBusy, JobError, JobRecord


def _config() -> Config:
    return Config(api_key="sk-test", workspace=Path.cwd())


def _result(message: str = "done") -> dict:
    return {
        "final_message": message,
        "turns_used": 1,
        "tokens": {"prompt": 1, "completion": 1, "total": 2},
        "tool_calls": 0,
        "duration_seconds": 0.01,
    }


class JobManagerTests(unittest.TestCase):
    def _wait_for_status(
        self,
        manager: DeepSeekJobManager,
        job_id: str,
        expected: set[str],
        timeout: float = 2.0,
    ) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = manager.status(job_id)
            if status["status"] in expected:
                return status
            time.sleep(0.01)
        self.fail(f"job {job_id} did not reach {expected}; last={manager.status(job_id)}")

    def test_background_job_accepts_steering_and_completes(self) -> None:
        manager = DeepSeekJobManager()
        started = threading.Event()
        got_message = threading.Event()
        captured: list[str] = []

        def fake_run_agent(task, config, **kwargs):
            started.set()
            deadline = time.time() + 2.0
            while time.time() < deadline:
                updates = kwargs["control_poll"]()
                if updates:
                    captured.extend(updates)
                    got_message.set()
                    break
                time.sleep(0.01)
            kwargs["control_finalize"]()
            return _result()

        with patch("deepseek_mcp.job_manager.run_agent", side_effect=fake_run_agent):
            job = manager.start("test task", "", _config())
            self.assertTrue(started.wait(1.0))
            queued = manager.send_message(job["job_id"], "change direction")
            self.assertTrue(queued["message_queued"])
            self.assertTrue(got_message.wait(1.0))
            self._wait_for_status(manager, job["job_id"], {"completed"})

        self.assertEqual(captured, ["change direction"])
        result = manager.result(job["job_id"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["result"]["final_message"], "done")

        usage = manager.claim_usage_record(job["job_id"])
        self.assertIsNotNone(usage)
        assert usage is not None
        summary, usage_result = usage
        self.assertEqual(summary, "test task")
        self.assertEqual(usage_result["tokens"]["total"], 2)
        self.assertIsNone(manager.claim_usage_record(job["job_id"]))

    def test_cancel_is_cooperative_and_reaches_cancelled(self) -> None:
        manager = DeepSeekJobManager()
        started = threading.Event()

        def fake_run_agent(task, config, **kwargs):
            started.set()
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if kwargs["cancel_check"]():
                    raise AgentLoopCancelled("cancelled in test")
                time.sleep(0.01)
            return _result("unexpected")

        with patch("deepseek_mcp.job_manager.run_agent", side_effect=fake_run_agent):
            job = manager.start("long task", "", _config())
            self.assertTrue(started.wait(1.0))
            response = manager.cancel(job["job_id"])
            self.assertTrue(response["cancel_requested"])
            status = self._wait_for_status(manager, job["job_id"], {"cancelled"})

        self.assertEqual(status["status"], "cancelled")
        self.assertFalse(status["accepting_messages"])

    def test_only_one_background_job_can_run(self) -> None:
        manager = DeepSeekJobManager()
        started = threading.Event()
        release = threading.Event()

        def fake_run_agent(task, config, **kwargs):
            started.set()
            release.wait(2.0)
            return _result()

        with patch("deepseek_mcp.job_manager.run_agent", side_effect=fake_run_agent):
            first = manager.start("first", "", _config())
            self.assertTrue(started.wait(1.0))
            with self.assertRaises(JobBusy):
                manager.start("second", "", _config())
            with self.assertRaises(JobBusy):
                manager.run_sync("sync", _config())
            release.set()
            self._wait_for_status(manager, first["job_id"], {"completed"})

    def test_sync_execution_blocks_background_start(self) -> None:
        manager = DeepSeekJobManager()
        started = threading.Event()
        release = threading.Event()

        def fake_run_agent(task, config, **kwargs):
            started.set()
            release.wait(2.0)
            return _result()

        with patch("deepseek_mcp.job_manager.run_agent", side_effect=fake_run_agent):
            thread = threading.Thread(target=lambda: manager.run_sync("sync", _config()))
            thread.start()
            self.assertTrue(started.wait(1.0))
            with self.assertRaises(JobBusy):
                manager.start("background", "", _config())
            release.set()
            thread.join(1.0)
            self.assertFalse(thread.is_alive())

    def test_finalize_closes_mailbox_atomically(self) -> None:
        job = JobRecord(job_id="abc", task="x", context="", task_summary="x")
        self.assertEqual(job.drain_messages(finalize_if_empty=True), [])
        with self.assertRaises(JobError):
            job.queue_message("too late")


if __name__ == "__main__":
    unittest.main()
