"""In-memory background job manager for steerable DeepSeek runs.

V1 intentionally allows only one DeepSeek execution at a time across both the
synchronous delegate API and background jobs. Completed background jobs are
retained in memory for result retrieval and pruned to a small bounded set.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .agent_loop import AgentLoopCancelled, AgentLoopError, run_agent
from .config import Config

TERMINAL_STATES = {"completed", "failed", "cancelled"}
MAX_RETAINED_JOBS = 20


class JobError(RuntimeError):
    """Base job-manager error."""


class JobNotFound(JobError):
    """Requested job id does not exist."""


class JobBusy(JobError):
    """Another DeepSeek execution already owns the single V1 execution slot."""


@dataclass
class JobRecord:
    job_id: str
    task: str
    context: str
    task_summary: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    usage_recorded: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _control_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _control_messages: deque[str] = field(default_factory=deque, repr=False)
    _accepting_messages: bool = field(default=True, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._control_lock:
            accepting_messages = self._accepting_messages
            queued_messages = len(self._control_messages)
        return {
            "job_id": self.job_id,
            "status": self.status,
            "cancel_requested": self.cancel_event.is_set(),
            "accepting_messages": accepting_messages,
            "queued_messages": queued_messages,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }

    def queue_message(self, message: str) -> None:
        """Atomically enqueue steering unless the agent has begun finalizing."""
        with self._control_lock:
            if not self._accepting_messages:
                raise JobError(
                    f"job {self.job_id} is no longer accepting steering messages"
                )
            self._control_messages.append(message)

    def drain_messages(self, *, finalize_if_empty: bool = False) -> list[str]:
        """Drain queued steering.

        When finalize_if_empty=True, observing an empty mailbox atomically closes
        it. A sender can therefore never receive a successful queue acknowledgement
        after the agent has committed to returning its final answer.
        """
        with self._control_lock:
            if self._control_messages:
                messages = list(self._control_messages)
                self._control_messages.clear()
                return messages
            if finalize_if_empty:
                self._accepting_messages = False
            return []

    def close_messages(self) -> None:
        with self._control_lock:
            self._accepting_messages = False


class DeepSeekJobManager:
    """Thread-safe manager with one shared DeepSeek execution slot."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._active_job_id: str | None = None
        self._sync_active = False

    def run_sync(self, task: str, config: Config) -> dict[str, Any]:
        """Run the legacy synchronous delegate while sharing the V1 execution slot."""
        with self._lock:
            self._ensure_slot_available_locked()
            self._sync_active = True
        try:
            return run_agent(task, config)
        finally:
            with self._lock:
                self._sync_active = False

    def start(self, task: str, context: str, config: Config) -> dict[str, Any]:
        if not task.strip():
            raise JobError("task must not be empty")

        with self._lock:
            self._prune_locked()
            self._ensure_slot_available_locked()

            job_id = uuid.uuid4().hex[:12]
            job = JobRecord(
                job_id=job_id,
                task=task,
                context=context,
                task_summary=task[:60],
            )
            self._jobs[job_id] = job
            self._active_job_id = job_id

            worker = threading.Thread(
                target=self._run_job,
                args=(job_id, config),
                name=f"deepseek-job-{job_id}",
                daemon=True,
            )
            worker.start()
            return job.snapshot()

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._get_locked(job_id).snapshot()

    def send_message(self, job_id: str, message: str) -> dict[str, Any]:
        if not message.strip():
            raise JobError("message must not be empty")
        with self._lock:
            job = self._get_locked(job_id)
            if job.status in TERMINAL_STATES:
                raise JobError(f"job {job_id} is already {job.status}")
            job.queue_message(message.strip())
            snap = job.snapshot()
            snap["message_queued"] = True
            return snap

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_locked(job_id)
            if job.status in TERMINAL_STATES:
                return job.snapshot()
            job.cancel_event.set()
            job.close_messages()
            return job.snapshot()

    def result(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_locked(job_id)
            payload = job.snapshot()
            if job.status == "completed":
                payload["result"] = job.result
            elif job.status in {"failed", "cancelled"}:
                payload["result"] = None
            else:
                payload["result"] = None
                payload["ready"] = False
                return payload
            payload["ready"] = True
            return payload

    def claim_usage_record(self, job_id: str) -> tuple[str, dict[str, Any]] | None:
        """Claim one completed job usage record exactly once."""
        with self._lock:
            job = self._get_locked(job_id)
            if job.status != "completed" or not job.result or job.usage_recorded:
                return None
            job.usage_recorded = True
            return job.task_summary, job.result

    def _run_job(self, job_id: str, config: Config) -> None:
        with self._lock:
            job = self._get_locked(job_id)
            job.status = "running"
            job.started_at = time.time()
            full_task = job.task
            if job.context:
                full_task = f"{job.task}\n\n# Additional context\n{job.context}"
            # Do not retain full task/context/config secrets longer than needed in
            # the manager's persistent job record. Keep only a short usage summary.
            job.task = ""
            job.context = ""

        try:
            result = run_agent(
                full_task,
                config,
                control_poll=lambda: job.drain_messages(),
                control_finalize=lambda: job.drain_messages(finalize_if_empty=True),
                cancel_check=job.cancel_event.is_set,
            )
        except AgentLoopCancelled as e:
            with self._lock:
                job.status = "cancelled"
                job.error = str(e)
                job.finished_at = time.time()
        except AgentLoopError as e:
            with self._lock:
                job.status = "failed"
                job.error = str(e)
                job.finished_at = time.time()
        except Exception as e:
            with self._lock:
                job.status = "failed"
                job.error = f"unexpected failure: {e}"
                job.finished_at = time.time()
        else:
            with self._lock:
                job.status = "completed"
                job.result = result
                job.finished_at = time.time()
        finally:
            job.close_messages()
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def _ensure_slot_available_locked(self) -> None:
        if self._sync_active:
            raise JobBusy("a synchronous DeepSeek delegation is already running")
        if self._active_job_id is None:
            return
        active = self._jobs.get(self._active_job_id)
        if active and active.status not in TERMINAL_STATES:
            raise JobBusy(
                f"DeepSeek job {active.job_id} is already {active.status}; "
                "finish or cancel it before starting another DeepSeek execution"
            )
        self._active_job_id = None

    def _get_locked(self, job_id: str) -> JobRecord:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"unknown DeepSeek job: {job_id}")
        return job

    def _prune_locked(self) -> None:
        terminal = [j for j in self._jobs.values() if j.status in TERMINAL_STATES]
        if len(terminal) < MAX_RETAINED_JOBS:
            return
        terminal.sort(key=lambda j: j.finished_at or j.created_at)
        remove_count = len(terminal) - MAX_RETAINED_JOBS + 1
        for job in terminal[:remove_count]:
            self._jobs.pop(job.job_id, None)
