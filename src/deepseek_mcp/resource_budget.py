"""Hard per-delegation resource budgets for untrusted model output."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

MAX_TOOL_CALLS_PER_TURN = 32
MAX_TOOL_CALLS_PER_RUN = 128
MAX_MUTATION_BYTES_PER_RUN = 64 * 1024 * 1024


class ResourceBudgetExceeded(RuntimeError):
    """The current delegation exhausted a hard resource budget."""


@dataclass
class MutationBudget:
    limit: int = MAX_MUTATION_BYTES_PER_RUN
    used: int = 0

    @contextmanager
    def reserve(self, byte_count: int) -> Iterator[None]:
        if byte_count < 0 or byte_count > self.limit - self.used:
            raise ResourceBudgetExceeded(
                f"mutation output budget exceeded ({self.limit} bytes per run)"
            )
        self.used += byte_count
        # Reservations are never refunded: an operation can commit with
        # os.replace() and then fail during directory fsync, so success cannot
        # be inferred from the exception boundary.
        yield


def apply_mutation(
    budget: MutationBudget | None, byte_count: int, operation: Callable[[], None]
) -> None:
    if budget is None:
        operation()
        return
    with budget.reserve(byte_count):
        operation()
