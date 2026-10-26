"""Offline audit regression tests. Never contact the provider."""

import json
import tempfile
import unittest
from pathlib import Path

from protocol_audit import IMPLEMENTED_ROUTES, inspect_files, inspect_har


class ProtocolAuditTests(unittest.TestCase):
    def test_redacts_creds_payload_query_and_unknown_id(self):
        secret = "SECRET_SHOULD_NEVER_APPEAR"
        data = {"log": {"entries": [
            {"startedDateTime": "2026-10-24T12:00:00Z", "request": {
                "method": "POST",
                "url": f"https://chat.deepseek.com/api/v0/chat/completion?access_token={secret}",
                "headers": [{"name": "authorization", "value": secret}],
                "postData": {"text": secret},
                "cookies": [{"name": "session", "value": secret}],
            }, "response": {"status": 200, "content": {"text": secret}}},
            {"request": {"method": "GET", "url":
                f"https://chat.deepseek.com/api/v0/users/{secret}"}, "response": {"status": 403}},
        ]}}
        result = inspect_har(data)
        serialized = json.dumps(result)
        self.assertNotIn(secret, serialized)
        self.assertEqual(result["http_status_counts"], {"200": 1, "403": 1})
        self.assertEqual(result["known_client_routes_observed"], ["/api/v0/chat/completion"])
        self.assertIn("<other_api_route>", serialized)

    def test_reports_missing_paths_as_unobserved_not_required(self):
        result = inspect_har({"log": {"entries": []}})
        self.assertEqual(set(result["known_client_routes_not_observed"]), IMPLEMENTED_ROUTES)
        self.assertEqual(result["matching_deepseek_entries"], 0)

    def test_filters_third_party_urls_and_query_strings(self):
        data = {"log": {"entries": [{"request": {
            "method": "GET", "url": "https://example.com/api/secret?token=SECRET"
        }, "response": {"status": 200}}]}}
        self.assertEqual(inspect_har(data)["matching_deepseek_entries"], 0)

    def test_additional_site_settings_are_visible_but_not_assumed_required(self):
        data = {"log": {"entries": [{"request": {
            "method": "GET", "url": "https://chat.deepseek.com/api/v0/client/settings?ignored=SECRET"
        }, "response": {"status": 200}}]}}
        result = inspect_har(data)
        self.assertEqual(result["other_known_site_routes_observed"], ["/api/v0/client/settings"])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("/api/v0/client/settings", result["known_client_routes_not_observed"])

    def test_file_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "PRIVATE_FILENAME_SECRET.har"
            p.write_text("not valid json", encoding="utf-8")
            report = inspect_files([p])
            self.assertEqual(report["network_requests_made"], 0)
            self.assertEqual(report["captures"], [{"capture": 1, "error": "JSONDecodeError"}])
            self.assertNotIn("PRIVATE_FILENAME_SECRET", json.dumps(report))

    def test_bad_entries_do_not_crash(self):
        result = inspect_har({"log": {"entries": [None, 123, {"request": None}]}})
        self.assertEqual(result["total_har_entries"], 3)
        self.assertEqual(result["matching_deepseek_entries"], 0)


if __name__ == "__main__":
    unittest.main()
