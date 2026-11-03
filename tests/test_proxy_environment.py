"""Proxy environment spellings the HTTP transport can actually parse."""
from __future__ import annotations

import os
import unittest
from importlib.util import find_spec
from unittest.mock import patch

from deepseek_mcp.proxy_environment import (
    normalize_no_proxy,
    sanitize_environment,
    sanitized_no_proxy_scope,
)

DOCKER_STYLE = "localhost,127.0.0.1,::1,[::1],api.deepseek.com"
# httpx is a locked runtime dependency; this test only needs it for the
# transport-level assertion, so a bare-checkout run can still execute the rest.
_httpx = find_spec("httpx")


class NoProxyNormalizationTests(unittest.TestCase):
    def test_bracketed_ipv6_loses_brackets_that_read_as_a_port(self) -> None:
        self.assertEqual(normalize_no_proxy("[::1]"), ("::1", ["[::1]"]))
        value, changed = normalize_no_proxy("[2001:db8::1]:8080")
        self.assertEqual(value, "2001:db8::1:8080")
        self.assertEqual(changed, ["[2001:db8::1]:8080"])

    def test_ordinary_hosts_are_left_alone(self) -> None:
        value, changed = normalize_no_proxy("localhost,127.0.0.1,.example.com, 10.0.0.0/8 ")
        self.assertEqual(changed, [])
        self.assertEqual(value, "localhost,127.0.0.1,.example.com,10.0.0.0/8")

    def test_blank_entries_are_dropped(self) -> None:
        self.assertEqual(normalize_no_proxy("a,,  ,b")[0], "a,b")


class SanitizedEnvironmentTests(unittest.TestCase):
    def test_input_is_not_mutated_and_both_spellings_are_fixed(self) -> None:
        original = {
            "NO_PROXY": DOCKER_STYLE,
            "no_proxy": "[::1]",
            "HTTP_PROXY": "http://127.0.0.1:8080",
        }
        clean = sanitize_environment(original)
        self.assertNotIn("[", f"{clean['NO_PROXY']}{clean['no_proxy']}")
        self.assertIn("[::1]", original["NO_PROXY"])
        self.assertEqual(clean["HTTP_PROXY"], "http://127.0.0.1:8080")

    def test_a_missing_value_stays_missing(self) -> None:
        self.assertEqual(sanitize_environment({"HTTP_PROXY": "http://x"}), {"HTTP_PROXY": "http://x"})

    @unittest.skipIf(_httpx is None, "httpx is not installed in this environment")
    def test_the_normalized_value_is_what_httpx_needs_to_construct_a_client(self) -> None:
        import httpx

        environment = {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "[::1]",
            "no_proxy": "[::1]",
        }
        self.assertEqual(sanitize_environment(environment)["no_proxy"], "::1")

        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(httpx.InvalidURL):
                httpx.Client()

        with patch.dict(os.environ, sanitize_environment(environment), clear=False):
            client = httpx.Client()
        client.close()


class NoProxyScopeTests(unittest.TestCase):
    def test_the_scope_restores_the_value_the_host_wrote(self) -> None:
        with patch.dict(os.environ, {"NO_PROXY": "[::1]"}, clear=False):
            with sanitized_no_proxy_scope():
                self.assertEqual(os.environ["NO_PROXY"], "::1")
            self.assertEqual(os.environ["NO_PROXY"], "[::1]")

    def test_absent_variables_stay_absent(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with sanitized_no_proxy_scope():
                self.assertNotIn("NO_PROXY", os.environ)
            self.assertNotIn("NO_PROXY", os.environ)

    def test_the_scope_is_restored_even_when_the_body_fails(self) -> None:
        with patch.dict(os.environ, {"no_proxy": "[::1]"}, clear=False):
            with self.assertRaises(RuntimeError), sanitized_no_proxy_scope():
                raise RuntimeError("transport exploded")
            self.assertEqual(os.environ["no_proxy"], "[::1]")


if __name__ == "__main__":
    unittest.main()
