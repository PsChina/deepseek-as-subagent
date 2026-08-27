from __future__ import annotations

import http.server
import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from deepseek_mcp import provider_process
from deepseek_mcp import provider_child
from deepseek_mcp import process_hardening
from deepseek_mcp.config import Config
from deepseek_mcp.provider_process import (
    ProviderRequestCancelled,
    ProviderRequestDeadline,
    request_in_subprocess,
)

ROOT = Path(__file__).resolve().parents[1]


class _DripHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    started = threading.Event()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "1000000")
        self.end_headers()
        self.close_connection = True
        type(self).started.set()
        try:
            for _ in range(100):
                self.wfile.write(b" \n")
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, _format: str, *_args) -> None:
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, _request, _client_address) -> None:
        pass


class ProviderProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _QuietServer(("127.0.0.1", 0), _DripHandler)
        cls.server.daemon_threads = True
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(2)

    def setUp(self) -> None:
        _DripHandler.started.clear()
        endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.config = Config(
            "placeholder", ROOT, base_url=endpoint, allowed_tools=["Read"]
        )
        self.messages = [{"role": "user", "content": "wait"}]

    def test_parent_provider_boundary_has_no_openai_import(self) -> None:
        source = (ROOT / "src" / "deepseek_mcp" / "provider_process.py").read_text()

        self.assertNotIn("from openai", source)
        self.assertNotIn("import openai", source)

    @unittest.skipUnless(os.name == "posix", "core dump limits require POSIX")
    def test_provider_child_disables_core_dumps_before_reading_request(self) -> None:
        with (
            patch.object(
                provider_child.sys,
                "argv",
                ["provider_child", "10", "fd:99"],
            ),
            patch("resource.setrlimit", side_effect=OSError("denied")),
            patch.object(provider_child, "_read_payload") as read_payload,
            patch.object(provider_child, "_write_payload") as write_payload,
        ):
            provider_child.main()

        read_payload.assert_not_called()
        write_payload.assert_called_once_with(
            {"kind": "error", "summary": "category=client", "retryable": False}
        )

    @unittest.skipUnless(os.name == "posix", "core dump limits require POSIX")
    def test_provider_child_sets_zero_core_limit(self) -> None:
        import resource

        with patch("resource.setrlimit") as set_limit:
            process_hardening.disable_core_dumps()

        set_limit.assert_called_once_with(resource.RLIMIT_CORE, (0, 0))

    def test_windows_process_disables_wer_heap_collection(self) -> None:
        with (
            patch.object(process_hardening.os, "name", "nt"),
            patch.object(
                process_hardening, "_disable_windows_heap_reporting"
            ) as disable,
        ):
            process_hardening.disable_core_dumps()

        disable.assert_called_once_with()

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "address-space limit is Linux-only"
    )
    def test_provider_child_has_a_hard_address_space_limit(self) -> None:
        import resource

        with (
            patch.object(process_hardening, "disable_core_dumps"),
            patch("resource.getrlimit", return_value=(resource.RLIM_INFINITY,) * 2),
            patch("resource.setrlimit") as set_limit,
        ):
            process_hardening.harden_provider_process()

        limit = process_hardening.PROVIDER_MEMORY_LIMIT_BYTES
        set_limit.assert_called_once_with(resource.RLIMIT_AS, (limit, limit))

    @unittest.skipUnless(sys.platform == "darwin", "Darwin policy check")
    def test_provider_child_does_not_apply_incompatible_darwin_address_limit(
        self,
    ) -> None:
        with (
            patch.object(process_hardening, "disable_core_dumps") as disable,
            patch("resource.setrlimit") as set_limit,
        ):
            process_hardening.harden_provider_process()

        disable.assert_called_once_with()
        set_limit.assert_not_called()

    def test_parent_validates_provider_response_without_sdk_types(self) -> None:
        payload = {
            "kind": "ok",
            "response": {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        }

        response, summary, retryable = provider_process._decode_response(
            json.dumps(payload).encode()
        )

        self.assertIsNotNone(response)
        self.assertEqual(response.choices[0].message.content, "ok")
        self.assertEqual(response.choices[0].finish_reason, "stop")
        self.assertEqual((summary, retryable), ("", False))

    def test_parent_rejects_truncated_provider_response(self) -> None:
        payload = {
            "kind": "ok",
            "response": {
                "choices": [{
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "partial"},
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        }

        self.assertEqual(
            provider_process._decode_response(json.dumps(payload).encode()),
            (None, "category=client", False),
        )

    def test_parent_retries_insufficient_inference_resources(self) -> None:
        payload = {
            "kind": "ok",
            "response": {
                "choices": [{
                    "finish_reason": "insufficient_system_resource",
                    "message": {"role": "assistant", "content": "partial"},
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        }

        self.assertEqual(
            provider_process._decode_response(json.dumps(payload).encode()),
            (None, "category=api", True),
        )

    def test_parent_rejects_finish_reason_inconsistent_with_tool_calls(self) -> None:
        payload = {
            "kind": "ok",
            "response": {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call-1",
                            "function": {"name": "Read", "arguments": "{}"},
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        }

        self.assertEqual(
            provider_process._decode_response(json.dumps(payload).encode()),
            (None, "category=client", False),
        )

    def test_parent_rejects_lone_surrogates_in_provider_text_fields(self) -> None:
        base = {
            "kind": "ok",
            "response": {
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call-1",
                            "function": {"name": "Read", "arguments": "{}"},
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        }
        cases = (
            ("content", lambda value: value.update(content="\ud800")),
            ("reasoning", lambda value: value.update(reasoning_content="\ud800")),
            ("id", lambda value: value["tool_calls"][0].update(id="\ud800")),
            ("name", lambda value: value["tool_calls"][0]["function"].update(name="\ud800")),
            ("arguments", lambda value: value["tool_calls"][0]["function"].update(arguments="\ud800")),
        )
        for label, mutate in cases:
            with self.subTest(field=label):
                payload = copy.deepcopy(base)
                mutate(payload["response"]["choices"][0]["message"])
                self.assertEqual(
                    provider_process._decode_response(
                        json.dumps(payload).encode()
                    ),
                    (None, "category=client", False),
                )

    def test_loopback_http_provider_ignores_inherited_proxy_environment(self) -> None:
        settings = {
            "credential": "placeholder",
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "deepseek-chat",
        }
        response = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        }
        raw_response = MagicMock()
        entered = raw_response.__enter__.return_value
        entered.headers = {"content-encoding": "identity"}
        entered.iter_bytes.return_value = [json.dumps(response).encode()]

        with (
            patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.invalid:3128"}),
            patch.object(provider_child.httpx, "Client") as http_client,
            patch.object(provider_child, "OpenAI") as openai,
        ):
            openai.return_value.chat.completions.with_streaming_response.create.return_value = (
                raw_response
            )
            result = provider_child.execute_request(settings, [], [], 10)

        self.assertEqual(result["kind"], "ok")
        self.assertFalse(http_client.call_args.kwargs["trust_env"])
        self.assertIs(
            openai.call_args.kwargs["http_client"], http_client.return_value
        )
        self.assertEqual(
            openai.call_args.kwargs["default_headers"]["Accept-Encoding"],
            "identity",
        )

    def test_remote_https_provider_keeps_explicit_proxy_support(self) -> None:
        self.assertTrue(
            provider_child._trust_proxy_environment("https://api.deepseek.com")
        )

    def test_parent_rejects_malformed_provider_response(self) -> None:
        raw = json.dumps({"kind": "ok", "response": {"choices": []}}).encode()

        self.assertEqual(
            provider_process._decode_response(raw),
            (None, "category=client", False),
        )

        self.assertEqual(
            provider_process._decode_response(b"[]"),
            (None, "category=client", False),
        )

    def test_parent_rejects_provider_response_without_usage(self) -> None:
        raw = json.dumps({
            "kind": "ok",
            "response": {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]
            },
        }).encode()

        self.assertEqual(
            provider_process._decode_response(raw),
            (None, "category=client", False),
        )

    def test_parent_rejects_zero_provider_usage(self) -> None:
        raw = json.dumps({
            "kind": "ok",
            "response": {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
        }).encode()

        self.assertEqual(
            provider_process._decode_response(raw),
            (None, "category=client", False),
        )

    def test_drip_response_cannot_extend_the_absolute_deadline(self) -> None:
        started = time.monotonic()
        with self.assertRaisesRegex(ProviderRequestDeadline, "time budget"):
            request_in_subprocess(
                self.config,
                self.messages,
                [],
                None,
                started + 0.6,
            )
        self.assertLess(time.monotonic() - started, 3.0)

    def test_workspace_shadow_package_is_never_imported_by_provider_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            package = workspace / "deepseek_mcp"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            marker = workspace / "host-code-executed"
            (package / "provider_child.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n"
                "print('{\"kind\":\"error\",\"summary\":\"category=client\","
                "\"retryable\":false}')\n",
                encoding="utf-8",
            )
            previous = Path.cwd()
            try:
                os.chdir(workspace)
                with patch.dict(os.environ, {"PYTHONPATH": str(workspace)}):
                    process = provider_process._start_provider(4.0)
                    output, _ = process.communicate(b"{}", timeout=8)
            finally:
                os.chdir(previous)
            self.assertFalse(marker.exists())
            self.assertEqual(json.loads(output)["summary"], "category=client")

    def test_cancellation_terminates_and_reaps_the_provider_child(self) -> None:
        cancelled = threading.Event()
        errors: list[BaseException] = []
        results = []
        child_pids: list[int] = []
        original_start = provider_process._start_provider

        def recording_start(timeout: float):
            process = original_start(timeout)
            child_pids.append(process.pid)
            return process

        def invoke() -> None:
            try:
                results.append(
                    request_in_subprocess(
                        self.config,
                        self.messages,
                        [],
                        cancelled,
                        time.monotonic() + 10,
                    )
                )
            except BaseException as error:
                errors.append(error)

        with patch(
            "deepseek_mcp.provider_process._start_provider",
            side_effect=recording_start,
        ):
            worker = threading.Thread(target=invoke)
            worker.start()
            reached_server = _DripHandler.started.wait(3)
            cancelled.set()
            worker.join(4)

        self.assertTrue(reached_server, (results, errors))
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ProviderRequestCancelled)
        self.assertEqual(len(child_pids), 1)
        self.assertFalse(_process_exists(child_pids[0]))

    def test_thread_start_failure_closes_provider_pipes(self) -> None:
        processes: list[subprocess.Popen[bytes]] = []

        def start_sleeper(_timeout: float) -> subprocess.Popen[bytes]:
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            processes.append(process)
            return process

        with (
            patch.object(
                provider_process, "_start_provider", side_effect=start_sleeper
            ),
            patch.object(
                provider_process.threading.Thread,
                "start",
                side_effect=RuntimeError("thread unavailable"),
            ),
        ):
            result = request_in_subprocess(
                self.config,
                self.messages,
                [],
                None,
                time.monotonic() + 5,
            )

        self.assertEqual(result, (None, "category=client", False))
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdin.closed)
        self.assertTrue(processes[0].stdout.closed)

    @unittest.skipUnless(os.name == "posix", "orphan process check requires POSIX")
    def test_orphaned_provider_exits_on_its_child_watchdog(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        parent = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests" / "provider_parent_helper.py"),
                str(ROOT),
                self.config.base_url,
                "10.0",
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline().strip())
        try:
            self.assertTrue(_DripHandler.started.wait(6))
            parent.kill()
            parent.wait(timeout=3)
            stdio_started = time.monotonic()
            parent.communicate(timeout=2.5)
            self.assertLess(time.monotonic() - stdio_started, 2.5)
            deadline = time.monotonic() + 8
            while _process_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_process_exists(child_pid))
        finally:
            if parent.poll() is None:
                parent.kill()
            if _process_exists(child_pid):
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            parent.communicate(timeout=3)


def _process_exists(process_id: int) -> bool:
    completed = subprocess.run(
        ["ps", "-p", str(process_id), "-o", "stat="],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


if __name__ == "__main__":
    unittest.main()
