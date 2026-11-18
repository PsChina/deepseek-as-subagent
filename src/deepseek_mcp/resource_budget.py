"""Hard per-delegation resource budgets for untrusted model output."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator
from threading import Lock

MAX_TOOL_CALLS_PER_TURN = 32
MAX_TOOL_CALLS_PER_RUN = 128
MAX_MUTATION_BYTES_PER_RUN = 64 * 1024 * 1024
MUTATION_TOOL_NAMES = frozenset({"Write", "Edit", "NotebookEdit"})


class ResourceBudgetExceeded(RuntimeError):
    """The current delegation exhausted a hard resource budget."""


@dataclass
class MutationBudget:
    limit: int = MAX_MUTATION_BYTES_PER_RUN
    used: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def validate(self, maximum: int = MAX_MUTATION_BYTES_PER_RUN) -> None:
        """Check hard limits while holding the reservation lock."""
        with self._lock:
            if (
                isinstance(self.limit, bool)
                or not isinstance(self.limit, int)
                or isinstance(self.used, bool)
                or not isinstance(self.used, int)
                or self.limit < 0
                or self.used < 0
                or self.used > self.limit
            ):
                raise ValueError("mutation budget is invalid")
            if self.limit > maximum:
                raise ResourceBudgetExceeded(
                    f"mutation budget exceeds the effective maximum of {maximum} bytes"
                )

    def advance_to(self, used: int) -> None:
        """Apply a child-process reservation without allowing the count to regress."""
        with self._lock:
            if (
                isinstance(used, bool)
                or not isinstance(used, int)
                or not self.used <= used <= self.limit
            ):
                raise ValueError("tool process returned an invalid budget")
            self.used = used

    @contextmanager
    def reserve(self, byte_count: int) -> Iterator[None]:
        if isinstance(byte_count, bool) or not isinstance(byte_count, int):
            raise ValueError("mutation reservation size is invalid")
        with self._lock:
            if byte_count < 0 or byte_count > self.limit - self.used:
                raise ResourceBudgetExceeded(
                    f"mutation output budget exceeded ({self.limit} bytes per run)"
                )
            self.used += byte_count
        # Never refund a reservation: a write may commit before durability fails.
        yield


def ensure_mutation_budget(
    budget: MutationBudget | None, maximum: int
) -> MutationBudget:
    """Create or validate a mutation budget against the active hard ceiling."""
    if budget is None:
        budget = MutationBudget(limit=maximum)
    if not isinstance(budget, MutationBudget):
        raise ValueError("mutation budget is invalid")
    budget.validate(maximum)
    return budget


def ensure_mutation_budget_for_tool(budget: MutationBudget | None, config) -> MutationBudget:
    """Apply the task ceiling when a tool dispatcher receives a mutation call."""
    policy = getattr(config, "effective_policy", None)
    maximum = getattr(policy, "max_mutation_bytes", MAX_MUTATION_BYTES_PER_RUN)
    return ensure_mutation_budget(budget, maximum)


def apply_mutation(
    budget: MutationBudget | None, byte_count: int, operation: Callable[[], None]
) -> None:
    if budget is None:
        operation()
        return
    with budget.reserve(byte_count):
        operation()
