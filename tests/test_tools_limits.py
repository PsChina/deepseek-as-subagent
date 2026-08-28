from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import deepseek_mcp.tools as tools_module
import deepseek_mcp.workspace_walk as walk_module
from deepseek_mcp.config import Config
from deepseek_mcp.tools import (
    MAX_GLOB_RESULTS,
    MAX_TEXT_FILE_BYTES,
    _execute_edit,
    _execute_glob,
    _execute_grep,
    _execute_notebook_edit,
    _execute_read,
    _execute_write,
    build_tool_schemas,
    execute_tool,
)


_CONTROL_NODES = (
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


class _ComplexityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.complexity = 1
        self.depth = 0
        self.max_depth = 0

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, _CONTROL_NODES):
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


def _make_oversized(path: Path) -> None:
    with path.open("wb") as handle:
        handle.write(b"x" * (MAX_TEXT_FILE_BYTES + 1))


class ToolLimitTests(unittest.TestCase):
    def test_grep_handles_adversarial_regex_in_linear_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "input.txt").write_text("a" * 30 + "!", encoding="utf-8")
            started = time.monotonic()

            result = _execute_grep({"pattern": "(a+)+$"}, workspace)

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("No matches found", result)

    def test_invalid_regex_does_not_log_the_untrusted_pattern(self) -> None:
        marker = "private-pattern-marker"
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir, contextlib.redirect_stderr(stderr):
            result = _execute_grep({"pattern": f"(?={marker})"}, Path(tmpdir))

        self.assertTrue(result.startswith("ERROR:"))
        self.assertNotIn(marker, result)
        self.assertNotIn(marker, stderr.getvalue())

    def test_source_complexity_stays_within_production_limits(self) -> None:
        source_path = Path(tools_module.__file__)
        source = source_path.read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 500)
        priority = {"_execute_edit", "_execute_glob", "_execute_grep"}
        for node in ast.parse(source).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            with self.subTest(function=node.name):
                visitor = _ComplexityVisitor()
                for statement in node.body:
                    visitor.visit(statement)
                self.assertLessEqual(node.end_lineno - node.lineno + 1, 50)
                self.assertLessEqual(visitor.max_depth, 4)
                self.assertLessEqual(visitor.complexity, 15)
                if node.name in priority:
                    self.assertLessEqual(visitor.complexity, 10)

    def test_schema_builder_remains_importable_from_tools(self) -> None:
        schemas = build_tool_schemas(["Grep", "unknown", "Read"])
        names = [schema["function"]["name"] for schema in schemas]
        self.assertEqual(names, ["Grep", "Read"])

    def test_notebook_schema_describes_insert_anchor_order(self) -> None:
        schema = build_tool_schemas(["NotebookEdit"])[0]["function"]
        properties = schema["parameters"]["properties"]

        self.assertIn("immediately after", schema["description"])
        self.assertIn("appends", schema["description"])
        self.assertIn("after it", properties["cell_id"]["description"])
        self.assertIn("after it", properties["cell_index"]["description"])

    def test_notebook_insert_uses_anchor_then_append_order(self) -> None:
        notebook = {
            "cells": [
                {"cell_type": "markdown", "id": "a", "source": ["a"], "metadata": {}},
                {"cell_type": "markdown", "id": "b", "source": ["b"], "metadata": {}},
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "order.ipynb"
            target.write_text(json.dumps(notebook), encoding="utf-8")
            _execute_notebook_edit(
                {
                    "path": "order.ipynb",
                    "edit_mode": "insert",
                    "cell_index": 0,
                    "cell_type": "markdown",
                    "new_source": "anchored",
                },
                workspace,
            )
            _execute_notebook_edit(
                {
                    "path": "order.ipynb",
                    "edit_mode": "insert",
                    "cell_type": "markdown",
                    "new_source": "appended",
                },
                workspace,
            )
            cells = json.loads(target.read_text(encoding="utf-8"))["cells"]

        self.assertEqual([cell["source"][0] for cell in cells], [
            "a", "anchored", "b", "appended",
        ])

    def test_read_edit_and_notebook_reject_oversized_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            for name, execute, args in (
                ("large.txt", _execute_read, {"path": "large.txt"}),
                (
                    "edit.txt",
                    _execute_edit,
                    {"path": "edit.txt", "old_string": "x", "new_string": "y"},
                ),
                (
                    "large.ipynb",
                    _execute_notebook_edit,
                    {"path": "large.ipynb", "edit_mode": "insert", "new_source": "x"},
                ),
            ):
                with self.subTest(tool=execute.__name__):
                    _make_oversized(workspace / name)
                    self.assertIn("exceeds", execute(args, workspace))

    def test_atomic_edit_preserves_original_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            path = workspace / "original.txt"
            path.write_text("before", encoding="utf-8")
            replace = (
                "deepseek_mcp.windows_atomic_commit.replace_paths"
                if os.name == "nt"
                else "deepseek_mcp.posix_atomic_commit._exchange"
            )
            with patch(replace, side_effect=OSError("failed")):
                result = _execute_edit(
                    {"path": "original.txt", "old_string": "before", "new_string": "after"},
                    workspace,
                )

            self.assertEqual(path.read_text(encoding="utf-8"), "before")
            self.assertEqual(list(workspace.glob(".deepseek-mcp-*.tmp")), [])
            self.assertIn("failed to write", result)

    @unittest.skipIf(os.name == "nt", "Windows does not preserve POSIX mode bits")
    def test_atomic_edit_preserves_existing_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            path = workspace / "mode.txt"
            path.write_text("before", encoding="utf-8")
            path.chmod(0o640)
            result = _execute_edit(
                {"path": "mode.txt", "old_string": "before", "new_string": "after"},
                workspace,
            )

            self.assertTrue(result.startswith("OK:"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    @unittest.skipIf(os.name == "nt", "directory fsync is POSIX-specific")
    def test_atomic_write_fsyncs_new_directories_and_final_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            with patch(
                "deepseek_mcp.file_io.os.fsync", wraps=os.fsync
            ) as fsync:
                result = _execute_write(
                    {"path": "nested/child/value.txt", "content": "durable"},
                    workspace,
                )

        self.assertTrue(result.startswith("OK:"))
        self.assertGreaterEqual(fsync.call_count, 4)

    def test_write_rejects_workspace_root_before_creating_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            result = _execute_write({"path": ".", "content": "x"}, workspace)

        self.assertIn("not a regular file", result)
        self.assertEqual(list(workspace.glob(".deepseek-mcp-*.tmp")), [])

    def test_write_rejects_existing_directory_before_creating_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "directory").mkdir()
            result = _execute_write(
                {"path": "directory", "content": "x"}, workspace
            )

        self.assertIn("not a regular file", result)
        self.assertEqual(list(workspace.glob(".deepseek-mcp-*.tmp")), [])

    def test_edit_rejects_invalid_utf8_without_changing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            path = workspace / "invalid.txt"
            original = b"before \xff tail"
            path.write_bytes(original)

            result = _execute_edit(
                {"path": "invalid.txt", "old_string": "before", "new_string": "after"},
                workspace,
            )

            self.assertIn("not valid UTF-8", result)
            self.assertEqual(path.read_bytes(), original)

    def test_notebook_edit_rejects_invalid_utf8_without_changing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            path = workspace / "invalid.ipynb"
            original = b'{"cells": [], "metadata": "\xff"}'
            path.write_bytes(original)

            result = _execute_notebook_edit(
                {"path": "invalid.ipynb", "edit_mode": "insert", "new_source": "x"},
                workspace,
            )

            self.assertIn("not valid UTF-8", result)
            self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_mutation_tools_reject_fifos_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            cases = (
                (
                    "edit.txt",
                    _execute_edit,
                    {"path": "edit.txt", "old_string": "a", "new_string": "b"},
                ),
                (
                    "data.ipynb",
                    _execute_notebook_edit,
                    {"path": "data.ipynb", "edit_mode": "insert", "new_source": "x"},
                ),
            )
            for name, execute, args in cases:
                with self.subTest(name=name):
                    os.mkfifo(workspace / name)
                    results: list[str] = []
                    worker = threading.Thread(
                        target=lambda: results.append(execute(args, workspace)), daemon=True
                    )
                    worker.start()
                    worker.join(1.0)
                    self.assertFalse(worker.is_alive(), f"{name} read blocked")
                    self.assertIn("not a regular file", results[0])

    @unittest.skipIf(os.name == "nt", "symlink loops are platform dependent")
    def test_path_symlink_loop_returns_errors_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            loop = workspace / "loop"
            loop.symlink_to("loop")
            cases = (
                (_execute_read, {"path": "loop"}),
                (_execute_write, {"path": "loop", "content": "changed"}),
                (
                    _execute_edit,
                    {"path": "loop", "old_string": "before", "new_string": "after"},
                ),
                (
                    _execute_notebook_edit,
                    {"path": "loop", "edit_mode": "insert", "new_source": "x"},
                ),
            )
            for execute, args in cases:
                with self.subTest(tool=execute.__name__):
                    result = execute(args, workspace)
                    self.assertTrue(result.startswith("ERROR:"), result)
                    self.assertEqual(os.readlink(loop), "loop")

    def test_unknown_home_base_returns_errors_for_recursive_tools(self) -> None:
        unknown = "~deepseek-mcp-user-that-must-not-exist"
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            glob_result = _execute_glob({"pattern": "*", "path": unknown}, workspace)
            grep_result = _execute_grep({"pattern": "x", "path": unknown}, workspace)

        self.assertTrue(glob_result.startswith("ERROR:"), glob_result)
        self.assertTrue(grep_result.startswith("ERROR:"), grep_result)

    def test_untrusted_argument_types_return_errors(self) -> None:
        cases = (
            ("Read", {"path": 1}),
            ("Write", {"path": [], "content": "x"}),
            (
                "Edit",
                {"path": "x", "old_string": "a", "new_string": "b", "replace_all": "yes"},
            ),
            ("Glob", {"pattern": 1}),
            ("Glob", {"pattern": "*", "path": []}),
            ("Grep", {"pattern": 1}),
            ("Grep", {"pattern": "x", "glob": []}),
            ("Grep", {"pattern": "x", "path": []}),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config(
                "credential",
                Path(tmpdir),
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
            )
            for name, args in cases:
                with self.subTest(name=name, args=args):
                    self.assertTrue(execute_tool(name, args, config).startswith("ERROR:"))
            self.assertTrue(execute_tool("Read", [], config).startswith("ERROR:"))  # type: ignore[arg-type]

    def test_read_and_grep_reject_coercible_integer_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "input.txt").write_text("one\ntwo\n", encoding="utf-8")
            for value in (True, 1.5, "1", -1):
                with self.subTest(tool="Read", value=value):
                    result = _execute_read(
                        {"path": "input.txt", "offset": value}, workspace
                    )
                    self.assertIn("offset must be", result)
            for value in (True, 1.5, "1", 0, 1001):
                with self.subTest(tool="Grep", value=value):
                    result = _execute_grep(
                        {"pattern": "one", "max_matches": value}, workspace
                    )
                    self.assertIn("max_matches must be", result)

    def test_notebook_cell_index_rejects_coercible_types_without_mutation(self) -> None:
        notebook = {
            "cells": [
                {"cell_type": "code", "id": "zero", "source": ["zero"]},
                {"cell_type": "code", "id": "one", "source": ["one"]},
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        original = json.dumps(notebook)
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "target.ipynb"
            target.write_text(original, encoding="utf-8")
            for value in (True, 0.9, "1"):
                with self.subTest(value=value):
                    result = _execute_notebook_edit(
                        {
                            "path": "target.ipynb",
                            "edit_mode": "delete",
                            "cell_index": value,
                        },
                        Path(tmpdir),
                    )
                    self.assertIn("cell_index must be an integer", result)
                    self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_glob_patterns_cannot_traverse_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            glob_result = _execute_glob({"pattern": "../*"}, workspace)
            grep_result = _execute_grep(
                {"pattern": "secret", "glob": "../../**/*"}, workspace
            )

        self.assertTrue(glob_result.startswith("ERROR:"))
        self.assertTrue(grep_result.startswith("ERROR:"))

    def test_glob_preserves_recursive_zero_segment_and_wildcard_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "root.py").write_text("root", encoding="utf-8")
            nested = workspace / "src" / "nested"
            nested.mkdir(parents=True)
            (workspace / "src" / "test1.c").write_text("c", encoding="utf-8")
            (nested / "deep.py").write_text("deep", encoding="utf-8")

            python_matches = _execute_glob({"pattern": "**/*.py"}, workspace)
            zero_segment = _execute_glob(
                {"pattern": "src/**/test?.[ch]"}, workspace
            )

        self.assertIn("root.py", python_matches)
        self.assertIn("src/nested/deep.py", python_matches)
        self.assertIn("src/test1.c", zero_segment)

    @unittest.skipIf(os.name == "nt", "symlink creation is not generally available")
    def test_recursive_tools_never_scan_or_read_external_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("private-marker", encoding="utf-8")
            (workspace / "outside-dir").symlink_to(outside, target_is_directory=True)
            (workspace / "outside-file.txt").symlink_to(outside / "secret.txt")
            outside_identity = (outside.stat().st_dev, outside.stat().st_ino)
            scanned_outside = False
            real_scandir = os.scandir

            def tracking_scandir(path):
                nonlocal scanned_outside
                info = os.fstat(path) if isinstance(path, int) else os.stat(path)
                scanned_outside |= (info.st_dev, info.st_ino) == outside_identity
                return real_scandir(path)

            for secure_dir_fds in (True, False):
                with self.subTest(secure_dir_fds=secure_dir_fds), patch.object(
                    walk_module, "_HAS_SECURE_DIR_FDS", secure_dir_fds
                ), patch(
                    "deepseek_mcp.workspace_walk.os.scandir",
                    side_effect=tracking_scandir,
                ):
                    glob_result = _execute_glob(
                        {"pattern": "**/absent-*"}, workspace
                    )
                    grep_result = _execute_grep(
                        {"pattern": "private-marker"}, workspace
                    )

        self.assertFalse(scanned_outside)
        self.assertNotIn("hidden", glob_result)
        self.assertTrue(grep_result.startswith("No matches found"))
        self.assertNotIn("secret.txt:", grep_result)

    def test_fallback_rejects_windows_reparse_points_without_isjunction(self) -> None:
        info = SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=0x400,
        )
        with (
            patch("deepseek_mcp.workspace_walk.os.lstat", return_value=info),
            patch("deepseek_mcp.workspace_walk.os.path.isjunction", create=True, return_value=False),
        ):
            self.assertIsNone(walk_module._real_path_info(Path("junction")))

    def test_walk_entries_directories_depth_and_deadline_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            nested = workspace / "one" / "two"
            nested.mkdir(parents=True)
            (workspace / "root.txt").write_text("root", encoding="utf-8")
            (nested / "deep.txt").write_text("deep", encoding="utf-8")
            cases = (
                ("MAX_WALK_ENTRIES", 1),
                ("MAX_WALK_DIRECTORIES", 1),
                ("MAX_WALK_DEPTH", 0),
            )
            for setting, value in cases:
                with self.subTest(setting=setting), patch.object(
                    walk_module, setting, value
                ):
                    result = _execute_glob({"pattern": "**/*.txt"}, workspace)
                    self.assertIn("configured traversal limit reached", result)
            with patch.object(walk_module, "MAX_WALK_SECONDS", 1.0), patch(
                "deepseek_mcp.workspace_walk.time.monotonic",
                side_effect=(0.0, 2.0),
            ):
                result = _execute_glob({"pattern": "**/*.txt"}, workspace)
            self.assertIn("configured traversal limit reached", result)
            with patch.object(walk_module, "MAX_WALK_ENTRIES", 0):
                grep_result = _execute_grep({"pattern": "root"}, workspace)
            self.assertIn("results incomplete", grep_result)

    def test_glob_pattern_length_and_segment_count_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            too_long = "a" * (walk_module.MAX_GLOB_PATTERN_CHARS + 1)
            too_deep = "/".join(
                ["a"] * (walk_module.MAX_GLOB_PATTERN_SEGMENTS + 1)
            )

            long_result = _execute_glob({"pattern": too_long}, workspace)
            deep_result = _execute_grep(
                {"pattern": "x", "glob": too_deep}, workspace
            )

        self.assertTrue(long_result.startswith("ERROR:"))
        self.assertTrue(deep_result.startswith("ERROR:"))
        self.assertIn("safety limit", long_result)
        self.assertIn("safety limit", deep_result)

    def test_notebook_helpers_preserve_replace_insert_delete_behavior(self) -> None:
        notebook = {
            "cells": [
                {
                    "cell_type": "code",
                    "id": "original",
                    "source": ["old\n"],
                    "metadata": {},
                    "outputs": [{"output_type": "stream"}],
                    "execution_count": 1,
                }
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            path = workspace / "test.ipynb"
            path.write_text(json.dumps(notebook), encoding="utf-8")
            replaced = _execute_notebook_edit(
                {"path": "test.ipynb", "edit_mode": "replace", "cell_id": "original", "new_source": "new\n"},
                workspace,
            )
            inserted = _execute_notebook_edit(
                {"path": "test.ipynb", "edit_mode": "insert", "cell_id": "original", "cell_type": "markdown", "new_source": "note"},
                workspace,
            )
            deleted = _execute_notebook_edit(
                {"path": "test.ipynb", "edit_mode": "delete", "cell_index": 1},
                workspace,
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertIn("replaced cell", replaced)
        self.assertIn("inserted markdown", inserted)
        self.assertIn("deleted cell", deleted)
        self.assertEqual(saved["cells"][0]["source"], ["new\n"])
        self.assertEqual(saved["cells"][0]["outputs"], [])
        self.assertIsNone(saved["cells"][0]["execution_count"])
        self.assertEqual(len(saved["cells"]), 1)

    def test_glob_stops_after_result_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            for index in range(MAX_GLOB_RESULTS + 1):
                (workspace / f"match-{index}.txt").write_text("ok", encoding="utf-8")
            result = _execute_glob({"pattern": "**/*"}, workspace)

        self.assertIn(f"limit {MAX_GLOB_RESULTS} reached", result)

    def test_grep_skips_oversized_files_and_reads_bounded_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            _make_oversized(workspace / "large.txt")
            (workspace / "small.txt").write_text("needle\n", encoding="utf-8")
            result = _execute_grep({"pattern": "needle"}, workspace)

        self.assertIn("small.txt:1: needle", result)
        self.assertNotIn("large.txt", result)
        self.assertIn("results incomplete", result)

    def test_grep_does_not_report_complete_negative_for_oversized_only_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            oversized = workspace / "large.txt"
            oversized.write_text("needle\n", encoding="utf-8")
            with oversized.open("ab") as handle:
                handle.truncate(MAX_TEXT_FILE_BYTES + 1)

            result = _execute_grep({"pattern": "needle"}, workspace)

        self.assertIn("results incomplete", result)
        self.assertNotIn("No matches found", result)


if __name__ == "__main__":
    unittest.main()
