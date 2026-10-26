"""Offline HTTP soak tests: never contact DeepSeek or execute model tools."""

from __future__ import annotations

import threading
import unittest

import requests

from openai_server import Gateway
from test_openai_bridge import FakeDeepSeek


class GatewaySoakTests(unittest.TestCase):
    def test_275_consecutive_responses_bound_history_and_keep_continuity(self):
        class OfflineProvider(FakeDeepSeek):
            response = "OFFLINE_SOAK_OK"

        gateway = Gateway(("127.0.0.1", 0), api_key="soak-local-key",
                          client_factory=OfflineProvider)
        worker = threading.Thread(target=gateway.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{gateway.server_port}/v1/responses"
        ids = []
        try:
            with requests.Session() as client:
                client.headers["Authorization"] = "Bearer soak-local-key"
                for index in range(275):
                    result = client.post(url, json={
                        "model": "deepseek-web", "input": f"Offline request {index}",
                        "stream": False,
                    }, timeout=8)
                    self.assertEqual(result.status_code, 200, f"request {index}: {result.text[:150]}")
                    obj = result.json()
                    self.assertEqual(obj["output_text"], "OFFLINE_SOAK_OK")
                    ids.append(obj["id"])

                self.assertEqual(len(gateway.response_history), 256)
                self.assertIsNone(gateway.recall(ids[0]))
                self.assertIsNotNone(gateway.recall(ids[-1]))

                # The latest conversation is still available for a tool result
                # or new message, while evicted contexts fail explicitly.
                continued = client.post(url, json={
                    "model": "deepseek-web", "previous_response_id": ids[-1],
                    "input": "Follow up on offline request 274",
                }, timeout=8)
                self.assertEqual(continued.status_code, 200, continued.text[:150])
                self.assertEqual(continued.json()["output_text"], "OFFLINE_SOAK_OK")

                expired = client.post(url, json={
                    "model": "deepseek-web", "previous_response_id": ids[0],
                    "input": "Invalid old continuation",
                }, timeout=8)
                self.assertEqual(expired.status_code, 400)
                self.assertEqual(expired.json()["error"]["type"], "invalid_request_error")
                self.assertEqual(len(gateway.response_history), 256)
        finally:
            gateway.shutdown()
            gateway.server_close()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
