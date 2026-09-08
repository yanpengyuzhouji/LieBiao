from __future__ import annotations

import unittest

from backend.manual_verification import _cookie_header


class ManualVerificationTests(unittest.TestCase):
    def test_cookie_header_only_keeps_target_platform(self) -> None:
        cookies = [
            {"name": "session", "value": "ok", "domain": ".cdt-ec.com"},
            {"name": "token", "value": "yes", "domain": "tang.cdt-ec.com"},
            {"name": "foreign", "value": "no", "domain": ".example.com"},
        ]
        self.assertEqual(_cookie_header(cookies, "tang.cdt-ec.com"), "session=ok; token=yes")


if __name__ == "__main__":
    unittest.main()
