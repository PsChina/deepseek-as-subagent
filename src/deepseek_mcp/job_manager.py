"""In-memory background job manager for steerable DeepSeek runs.

V1 intentionally allows only one active DeepSeek job at a time. Completed jobs
are retained in memory for result retrieval and pruned to a small bounded set.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
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
    """A DeepSeek job is already active."""


@dataclass
class JobRecord:
    job_id: str
    task: str
    context: str
    config: Config
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    control_queue: queue.Queue[str] = field(default_factory=queue.Queue, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "cancel_requested": self.cancel_event.is_set(),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class DeepSeekJobManager:
    """Thread-safe single-active-job manager."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._active_job_id: str | None = None

    def start(self, task: str, context: str, config: Config) -> dict[str, Any]:
        if not task.strip():
            raise JobError("task must not be empty")

        with self._lock:
            self._prune_locked()
            if self._active_job_id is not None:
                active = self._jobs.get(self._active_job_id)
                if active and active.status not in TERMINAL_STATES:
                    raise JobBusy(
                        f"DeepSeek job {active.job_id} is already {active.status}; "
                        "finish or cancel it before starting another job"
                    )
                self._active_job_id = None

            job_id = uuid.uuid4().hex[:12]
            job = JobRecord(job_id=job_id, task=task, context=context, config=config)
            self._jobs[job_id] = job
            self._active_job_id = job_id

            worker = threading.Thread(
                target=self._run_job,
                args=(job_id,),
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
            job.control_queue.put(message.strip())
            snap = job.snapshot()
            snap["message_queued"] = True
            return snap

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_locked(job_id)
            if job.status in TERMINAL_STATES:
                return job.snapshot()
            job.cancel_event.set()
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

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._get_locked(job_id)
            job.status = "running"
            job.started_at = time.time()

        full_task = job.task
        if job.context:
            full_task = f"{job.task}\n\n# Additional context\n{job.context}"

        def poll_control() -> list[str]:
            messages: list[str] = []
            while True:
                try:
                    messages.append(job.control_queue.get_nowait())
                except queue.Empty:
                    return messages

        try:
            result = run_agent(
                full_task,
                job.config,
                control_poll=poll_control,
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
            with self._lock:
                if self._active_job_id == job_id:
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
