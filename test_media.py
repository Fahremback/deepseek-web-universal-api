"""Offline protocol coverage for browserless media attachments."""
import unittest
from unittest.mock import MagicMock, patch

from api_client import (
    CompletionAccumulator, DeepSeekAPIError, DeepSeekClient,
    FILE_FETCH_PATH, FILE_UPLOAD_PATH, build_arg_parser,
)


def response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = payload
    return resp


def envelope(data):
    return {"code": 0, "data": {"biz_code": 0, "biz_data": data}}


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.client = DeepSeekClient(token="dummy-testing-token")

    def tearDown(self):
        self.client.session.close()

    def test_upload_multipart_uses_pow_for_upload_path(self):
        challenge = {
            "algorithm": "DeepSeekHashV1",
            "challenge": "00" * 32,
            "salt": "test", "signature": "sig",
            "target_path": FILE_UPLOAD_PATH,
        }
        with patch.object(self.client, "create_pow_challenge", return_value=challenge) as get_pow:
            with patch("api_client.solve_pow", return_value=1):
                with patch.object(self.client.session, "post", return_value=response(envelope({"id": "f123"}))) as post:
                    result = self.client.upload_file(("mini.png", b"abcd"), thinking_enabled=True, wait_for_processing=False)
        get_pow.assert_called_once_with(FILE_UPLOAD_PATH)
        self.assertEqual(result["file_id"], "f123")
        self.assertEqual(result["status"], "UPLOADED")
        kwargs = post.call_args.kwargs
        self.assertTrue(post.call_args.args[0].endswith(FILE_UPLOAD_PATH))
        self.assertEqual(kwargs["files"]["file"][0], "mini.png")
        self.assertEqual(kwargs["files"]["file"][2], "image/png")
        self.assertEqual(kwargs["headers"]["x-file-size"], "4")
        self.assertEqual(kwargs["headers"]["x-thinking-enabled"], "1")
        self.assertNotIn("content-type", kwargs["headers"])
        self.assertEqual(kwargs["headers"]["x-model-type"], "default")
        self.assertIn("x-ds-pow-response", kwargs["headers"])

    def test_file_parser_polls_pending_then_success(self):
        pending = response(envelope({"files": [{"id": "f1", "status": "PENDING"}]}))
        ok = response(envelope({"files": [{"id": "f1", "status": "SUCCESS"}]}))
        with patch.object(self.client.session, "get", side_effect=[pending, ok]) as get:
            with patch("api_client.time.sleep"):
                result = self.client.wait_for_file_ready("f1", poll_interval=0.2)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(get.call_count, 2)
        self.assertTrue(get.call_args.args[0].endswith(FILE_FETCH_PATH))
        self.assertEqual(get.call_args.kwargs["params"], {"file_ids": "f1"})

    def test_parser_failure_is_not_forwarded_to_completion(self):
        failed = response(envelope({"files": [{"id": "f1", "status": "FAILED", "error_code": 50404}]}))
        with patch.object(self.client.session, "get", return_value=failed):
            with self.assertRaisesRegex(DeepSeekAPIError, "50404"):
                self.client.wait_for_file_ready("f1")

    def test_parser_missing_file_fails_early(self):
        missing = response(envelope({"files": []}))
        with patch.object(self.client.session, "get", return_value=missing):
            with self.assertRaisesRegex(DeepSeekAPIError, "missing"):
                self.client.wait_for_file_ready("f1")

    def test_empty_and_missing_local_attachment(self):
        with self.assertRaises(ValueError):
            self.client.upload_file(("empty.txt", b""))
        with self.assertRaises(FileNotFoundError):
            self.client.upload_file("missing_media_never_here.pdf")

    def test_media_refs_are_passed_to_completion_with_existing_refs(self):
        class FakeStream:
            def iter_lines(self, chunk_size=1024):
                return iter([
                    b"event: ready", b'data: {"response_message_id":2}', b"",
                    b'data: {"v":{"response":{"message_id":2,"status":"FINISHED","fragments":[{"type":"RESPONSE","content":"ok"}]}}}', b"",
                    b"event: close", b"data: {}", b"",
                ])
            def close(self):
                pass

        with patch.object(self.client, "upload_file", return_value={
            "file_id": "f1", "filename": "image.png", "status": "SUCCESS"
        }) as upload:
            with patch.object(self.client, "create_completion_stream", return_value=(FakeStream(), CompletionAccumulator())) as complete:
                chunks = list(self.client.stream_completion(
                    chat_session_id="s", prompt="Describe", ref_file_ids=["old"],
                    attachments=[("image.png", b"abc")], thinking_enabled=True,
                ))
        self.assertEqual([c.event for c in chunks if c.event == "file"], ["file"])
        self.assertEqual(self.client.last_file_ids, ["old", "f1"])
        self.assertEqual("".join(c.text for c in chunks), "ok")
        self.assertEqual(complete.call_args.kwargs["ref_file_ids"], ["old", "f1"])
        upload.assert_called_once()

    def test_cli_repeatable_files(self):
        args = build_arg_parser().parse_args(["--file", "a.png", "--file", "b.pdf", "Analise"])
        self.assertEqual(args.files, ["a.png", "b.pdf"])


if __name__ == "__main__":
    unittest.main()
