import unittest
from unittest.mock import Mock, patch

from backend.update_checker import UpdateCheckError, check_update, version_key


class UpdateCheckerTests(unittest.TestCase):
    def test_semantic_version_comparison(self):
        self.assertGreater(version_key("v1.10.0"), version_key("1.9.9"))
        self.assertEqual(version_key("1.1"), version_key("1.1.0"))

    @patch("backend.update_checker.httpx.get")
    def test_github_release_is_normalized(self, get):
        response = Mock(content=b"{}")
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "tag_name": "v1.2.0",
            "html_url": "https://github.com/example/project/releases/tag/v1.2.0",
            "body": "changes",
        }
        get.return_value = response
        result = check_update("1.1.0", "https://api.github.com/repos/example/project/releases/latest")
        self.assertTrue(result["available"])
        self.assertEqual(result["latest_version"], "1.2.0")

    def test_non_https_manifest_is_rejected(self):
        with self.assertRaises(UpdateCheckError):
            check_update("1.1.0", "http://example.com/latest.json")


if __name__ == "__main__":
    unittest.main()
