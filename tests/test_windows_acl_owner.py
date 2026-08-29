from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from deepseek_mcp import windows_acl


@unittest.skipUnless(os.name == "nt", "Windows ACL owner semantics")
class WindowsAclOwnerTests(unittest.TestCase):
    @staticmethod
    def _sid_equal(left: int, right: int) -> bool:
        return int(left) == int(right)

    def test_current_user_owner_is_accepted(self) -> None:
        with patch.object(windows_acl, "_EQUAL_SID", side_effect=self._sid_equal):
            self.assertTrue(
                windows_acl._owner_sid_is_allowed(
                    owner=101,
                    current_user=101,
                    token_owner=202,
                    allowed=[101, 303],
                )
            )

    def test_trusted_token_default_owner_is_accepted(self) -> None:
        with patch.object(windows_acl, "_EQUAL_SID", side_effect=self._sid_equal):
            self.assertTrue(
                windows_acl._owner_sid_is_allowed(
                    owner=202,
                    current_user=101,
                    token_owner=202,
                    allowed=[101, 202, 303],
                )
            )

    def test_untrusted_token_default_owner_is_rejected(self) -> None:
        with patch.object(windows_acl, "_EQUAL_SID", side_effect=self._sid_equal):
            self.assertFalse(
                windows_acl._owner_sid_is_allowed(
                    owner=202,
                    current_user=101,
                    token_owner=202,
                    allowed=[101, 303],
                )
            )

    def test_unrelated_owner_is_rejected(self) -> None:
        with patch.object(windows_acl, "_EQUAL_SID", side_effect=self._sid_equal):
            self.assertFalse(
                windows_acl._owner_sid_is_allowed(
                    owner=404,
                    current_user=101,
                    token_owner=202,
                    allowed=[101, 202, 303],
                )
            )

    def test_current_owner_sid_queries_token_owner_class(self) -> None:
        sentinel = (object(), 202)
        with patch.object(
            windows_acl, "_current_token_sid", return_value=sentinel
        ) as query:
            self.assertEqual(windows_acl._current_owner_sid(), sentinel)
        query.assert_called_once_with(windows_acl._TOKEN_OWNER)


if __name__ == "__main__":
    unittest.main()
