"""In-memory background job manager for steerable DeepSeek runs.

Each manager has one local execution slot, and an OS lease extends exclusivity
across processes that target the same canonical workspace. Completed background
jobs are retained in memory for result retrieval and pruned to a bounded set.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_loop import (
    AgentLoopCancelled,
    AgentLoopError,
    CancellationSignal,
    run_agent,
)
from .config import Config
from .mutation_outcome import mutation_failure_message, records_from_result
from .provider_retry import MutationOutcomeError
from .transaction_recovery import TransactionRecoveryError, require_no_pending
from .execution_lock import (
    WorkspaceExecutionLease,
    WorkspaceLockBusy,
    WorkspaceLockError,
    acquire_workspace_lease,
)

TERMINAL_STATES = {"completed", "failed", "cancelled"}
MAX_RETAINED_JOBS = 20
MAX_TASK_BYTES = 256 * 1024
MAX_CONTEXT_BYTES = 256 * 1024
MAX_COMBINED_TASK_BYTES = MAX_TASK_BYTES + MAX_CONTEXT_BYTES + 64
MAX_STEERING_MESSAGE_BYTES = 64 * 1024
MAX_QUEUED_MESSAGES = 32
MAX_QUEUED_MESSAGE_BYTES = 256 * 1024
CANCELLED_ERROR = "DeepSeek job cancelled by parent agent"

logger = logging.getLogger(__name__)


class JobError(RuntimeError):
    """Base job-manager error."""


class JobNotFound(JobError):
    """Requested job id does not exist."""


class JobBusy(JobError):
    """Another DeepSeek execution owns the local slot or workspace lease."""


@dataclass
class JobRecord:
    job_id: str
    task: str
    context: str
    task_length: int
    capability: str = "coding"
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    usage_recorded: bool = False
    usage_recording: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    finished_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _control_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _control_messages: deque[str] = field(default_factory=deque, repr=False)
    _queued_message_bytes: int = field(default=0, repr=False)
    _accepting_messages: bool = field(default=True, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._control_lock:
            accepting_messages = self._accepting_messages
            queued_messages = len(self._control_messages)
        return {
            "job_id": self.job_id,
            "capability": self.capability,
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
        size = _bounded_text_bytes(
            "message", message, MAX_STEERING_MESSAGE_BYTES, allow_empty=False
        )
        with self._control_lock:
            if not self._accepting_messages:
                raise JobError(
                    f"job {self.job_id} is no longer accepting steering messages"
                )
            if len(self._control_messages) >= MAX_QUEUED_MESSAGES:
                raise JobError("job steering queue has too many messages")
            if self._queued_message_bytes + size > MAX_QUEUED_MESSAGE_BYTES:
                raise JobError("job steering queue exceeds its byte limit")
            self._control_messages.append(message)
            self._queued_message_bytes += size

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
                self._queued_message_bytes = 0
                return messages
            if finalize_if_empty:
                self._accepting_messages = False
            return []

    def close_messages(self) -> None:
        with self._control_lock:
            self._accepting_messages = False
            self._control_messages.clear()
            self._queued_message_bytes = 0


@dataclass(frozen=True)
class _JobOutcome:
    status: str
    result: dict[str, Any] | None
    error: str | None
    preserve_mutation_error: bool = False


def _run_background_agent(
    task: str, config: Config, job: JobRecord, lease: WorkspaceExecutionLease,
) -> _JobOutcome:
    try:
        result = run_agent(
            task,
            config,
            control_poll=lambda: job.drain_messages(),
            control_finalize=lambda: job.drain_messages(finalize_if_empty=True),
            cancel_signal=job.cancel_event,
            execution_lease_fd=lease.fileno(),
        )
    except MutationOutcomeError as error:
        status = "cancelled" if isinstance(error, AgentLoopCancelled) else "failed"
        return _JobOutcome(status, None, str(error), True)
    except AgentLoopCancelled as error:
        return _JobOutcome("cancelled", None, str(error))
    except AgentLoopError as error:
        return _JobOutcome("failed", None, str(error))
    except Exception:
        logger.error("DeepSeek background job failed category=internal")
        return _JobOutcome("failed", None, "unexpected internal failure")
    return _JobOutcome("completed", result, None)


def _apply_cancelled_outcome(job: JobRecord, outcome: _JobOutcome) -> None:
    records = records_from_result(outcome.result)
    job.cancel_event.set()
    job.status = "cancelled"
    job.result = None
    if records:
        job.error = mutation_failure_message(records, CANCELLED_ERROR)
    elif outcome.preserve_mutation_error:
        job.error = outcome.error
    else:
        job.error = outcome.error if outcome.status == "cancelled" else CANCELLED_ERROR


def _bounded_text_bytes(
    label: str, value: str, limit: int, *, allow_empty: bool = True
) -> int:
    if not isinstance(value, str):
        raise JobError(f"{label} must be a string")
    if not allow_empty and (not value or value.isspace()):
        raise JobError(f"{label} must not be empty")
    if len(value) > limit:
        raise JobError(f"{label} exceeds the {limit}-byte limit")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise JobError(f"{label} is not valid Unicode text") from None
    if size > limit:
        raise JobError(f"{label} exceeds the {limit}-byte limit")
    return size


def validate_delegation_input(task: str, context: str = "") -> None:
    _bounded_text_bytes("task", task, MAX_TASK_BYTES, allow_empty=False)
    _bounded_text_bytes("context", context, MAX_CONTEXT_BYTES)


class DeepSeekJobManager:
    """Thread-safe manager with one shared DeepSeek execution slot."""

    def __init__(self, lock_directory: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._active_job_id: str | None = None
        self._sync_active = False
        self._lock_directory = lock_directory

    def run_sync(
        self,
        task: str,
        config: Config,
        cancel_signal: CancellationSignal | None = None,
    ) -> dict[str, Any]:
        """Run synchronous delegation while sharing the execution slot/lease."""
        _bounded_text_bytes(
            "task", task, MAX_COMBINED_TASK_BYTES, allow_empty=False
        )
        with self._lock:
            self._ensure_slot_available_locked()
            lease = self._acquire_ready_workspace_lease_locked(config)
            self._sync_active = True
        try:
            return run_agent(
                task,
                config,
                cancel_signal=cancel_signal,
                execution_lease_fd=lease.fileno(),
            )
        finally:
            with self._lock:
                self._release_workspace_lease_locked(lease)
                self._sync_active = False

    def start(self, task: str, context: str, config: Config) -> dict[str, Any]:
        validate_delegation_input(task, context)

        with self._lock:
            self._ensure_slot_available_locked()
            lease = self._acquire_ready_workspace_lease_locked(config)

            job_id = uuid.uuid4().hex[:12]
            job = JobRecord(
                job_id=job_id,
                task=task,
                context=context,
                task_length=len(task),
                capability=config.delegation_capability,
            )
            self._jobs[job_id] = job
            self._active_job_id = job_id

            try:
                worker = threading.Thread(
                    target=self._run_job,
                    args=(job_id, config, lease),
                    name=f"deepseek-job-{job_id}",
                    daemon=True,
                )
                worker.start()
            except Exception as error:
                self._jobs.pop(job_id, None)
                if self._active_job_id == job_id:
                    self._active_job_id = None
                self._release_workspace_lease_locked(lease)
                raise JobError("failed to start DeepSeek worker thread") from error
            # Pruning is part of the successful-start commit. A rejected local
            # slot, busy workspace lease, or failed thread start must not make
            # an already completed result disappear.
            self._prune_locked()
            return job.snapshot()

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._get_locked(job_id).snapshot()

    def wait_for_terminal(self, job_id: str, timeout: float | None = None) -> bool:
        """Wait on the job's completion event without polling manager state."""
        with self._lock:
            finished_event = self._get_locked(job_id).finished_event
        return finished_event.wait(timeout)

    def send_message(self, job_id: str, message: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_locked(job_id)
            if job.status in TERMINAL_STATES:
                raise JobError(f"job {job_id} is already {job.status}")
            job.queue_message(message)
            snap = job.snapshot()
            snap["message_queued"] = True
            return snap

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_locked(job_id)
            if job.status in TERMINAL_STATES:
                payload = job.snapshot()
                payload["cancel_accepted"] = False
                return payload
            accepted = not job.cancel_event.is_set()
            if accepted:
                job.cancel_event.set()
                job.close_messages()
            payload = job.snapshot()
            payload["cancel_accepted"] = accepted
            return payload

    def result(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_locked(job_id)
            return self._result_payload_locked(job)

    def result_with_usage_claim(
        self, job_id: str
    ) -> tuple[dict[str, Any], tuple[int, dict[str, Any]] | None]:
        """Read the result and claim its usage record in one manager lock."""
        with self._lock:
            job = self._get_locked(job_id)
            payload = self._result_payload_locked(job)
            usage = self._claim_usage_locked(job)
            return payload, usage

    def claim_usage_record(self, job_id: str) -> tuple[int, dict[str, Any]] | None:
        """Claim one completed usage record until its persistence is acknowledged."""
        with self._lock:
            job = self._get_locked(job_id)
            return self._claim_usage_locked(job)

    def finish_usage_record(self, job_id: str, persisted: bool) -> None:
        """Acknowledge persistence, or release a failed claim for a later retry."""
        with self._lock:
            job = self._get_locked(job_id)
            if not job.usage_recording:
                return
            job.usage_recording = False
            if persisted:
                job.usage_recorded = True

    @staticmethod
    def _result_payload_locked(job: JobRecord) -> dict[str, Any]:
        payload = job.snapshot()
        payload["result"] = job.result if job.status == "completed" else None
        payload["ready"] = job.status in TERMINAL_STATES
        return payload

    @staticmethod
    def _claim_usage_locked(job: JobRecord) -> tuple[int, dict[str, Any]] | None:
        if (
            job.status != "completed"
            or not job.result
            or job.usage_recorded
            or job.usage_recording
        ):
            return None
        job.usage_recording = True
        return job.task_length, job.result

    def _run_job(
        self,
        job_id: str,
        config: Config,
        lease: WorkspaceExecutionLease,
    ) -> None:
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

        outcome = _run_background_agent(full_task, config, job, lease)
        self._finish_job(
            job_id,
            desired_status=outcome.status,
            result=outcome.result,
            error=outcome.error,
            lease=lease,
            preserve_mutation_error=outcome.preserve_mutation_error,
        )

    def _finish_job(
        self,
        job_id: str,
        *,
        desired_status: str,
        result: dict[str, Any] | None,
        error: str | None,
        lease: WorkspaceExecutionLease,
        preserve_mutation_error: bool = False,
    ) -> None:
        """Commit one terminal state atomically with accepted cancellation."""
        with self._lock:
            job = self._get_locked(job_id)
            self._release_workspace_lease_locked(lease)
            cancellation_won = job.cancel_event.is_set() or desired_status == "cancelled"
            if cancellation_won:
                _apply_cancelled_outcome(
                    job,
                    _JobOutcome(
                        desired_status, result, error, preserve_mutation_error
                    ),
                )
            else:
                job.status = desired_status
                job.result = result if desired_status == "completed" else None
                job.error = error
            job.finished_at = time.time()
            job.close_messages()
            if self._active_job_id == job_id:
                self._active_job_id = None
            job.finished_event.set()

    def _acquire_workspace_lease_locked(
        self,
        config: Config,
    ) -> WorkspaceExecutionLease:
        try:
            assert config.expected_workspace_identity is not None
            return acquire_workspace_lease(
                config.workspace,
                self._lock_directory,
                expected_identity=bytes.fromhex(config.expected_workspace_identity),
            )
        except WorkspaceLockBusy as error:
            raise JobBusy(str(error)) from error
        except WorkspaceLockError as error:
            raise JobError(str(error)) from error

    def _acquire_ready_workspace_lease_locked(
        self, config: Config,
    ) -> WorkspaceExecutionLease:
        lease = self._acquire_workspace_lease_locked(config)
        try:
            require_no_pending(config)
        except TransactionRecoveryError as error:
            self._release_workspace_lease_locked(lease)
            raise JobError(str(error)) from None
        return lease

    @staticmethod
    def _release_workspace_lease_locked(lease: WorkspaceExecutionLease) -> None:
        try:
            lease.release()
        except WorkspaceLockError:
            logger.exception("Failed to release workspace execution lease")

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
        terminal = [
            job for job in self._jobs.values()
            if job.status in TERMINAL_STATES and not job.usage_recording
        ]
        if len(terminal) < MAX_RETAINED_JOBS:
            return
        terminal.sort(key=lambda j: j.finished_at or j.created_at)
        remove_count = len(terminal) - MAX_RETAINED_JOBS + 1
        for job in terminal[:remove_count]:
            self._jobs.pop(job.job_id, None)
