from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "src" / "deepseek_mcp", ROOT / "adapters" / "codex")
CONTROL_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.IfExp,
    ast.Match,
)


class _FunctionMetrics(ast.NodeVisitor):
    def __init__(self) -> None:
        self.complexity = 1
        self.depth = 0
        self.max_depth = 0

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, CONTROL_NODES):
            self.complexity += 1
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
            super().generic_visit(node)
            self.depth -= 1
            return
        if isinstance(node, ast.BoolOp):
            self.complexity += max(1, len(node.values) - 1)
        elif isinstance(node, (ast.comprehension, ast.ExceptHandler, ast.Assert)):
            self.complexity += 1
        super().generic_visit(node)


class SourceLimitTests(unittest.TestCase):
    def test_production_python_sources_stay_within_limits(self) -> None:
        violations: list[str] = []
        paths = sorted(path for root in SOURCE_ROOTS for path in root.glob("*.py"))
        for path in paths:
            source = path.read_text(encoding="utf-8")
            relative = path.relative_to(ROOT)
            if len(source.splitlines()) > 500:
                violations.append(f"{relative}: more than 500 lines")
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                span = (node.end_lineno or node.lineno) - node.lineno + 1
                metrics = _FunctionMetrics()
                metrics.visit(node)
                if span > 50:
                    violations.append(f"{relative}:{node.lineno} {node.name}: {span} lines")
                if metrics.max_depth > 4:
                    violations.append(
                        f"{relative}:{node.lineno} {node.name}: nesting {metrics.max_depth}"
                    )
                if metrics.complexity > 15:
                    violations.append(
                        f"{relative}:{node.lineno} {node.name}: complexity {metrics.complexity}"
                    )
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
