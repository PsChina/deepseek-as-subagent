"""Shared contract for preparing and verifying the fixed live benchmark tasks."""

TASK_PATH = "src/task.py"
TASK_TOOLS = ("Read", "Write", "Edit")
TASK_CALL_LIMIT = 8


def task_contract() -> dict:
    return {
        "intent": "implement",
        "permissions": {
            "tools": list(TASK_TOOLS),
            "read_paths": [TASK_PATH], "write_paths": [TASK_PATH],
        },
        "constraints": {
            "success_checks": ["trusted host-side hidden acceptance tests"],
            "expected_outputs": [TASK_PATH],
        },
        "limits": {"max_tool_calls": TASK_CALL_LIMIT, "max_mutation_bytes": 4096},
    }
