"""Offline compliance checks for outgoing HTTP request pacing and server limits."""

import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from request_guard import GuardedSession, OriginGate, RateLimitPause, RateLimitStateError


def response(status, headers=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result.raw = io.BytesIO(b"")
    result._content = b"{}"
    return result


class RequestGuardTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_shared_cooldown_and_lock_are_private_to_os_user(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "DeepSeekWebClient" / "cooldown.json"
            gate = OriginGate(interval=0, cooldown_file=path)
            gate.before()
            gate.after(response(429, {"Retry-After": "60"}))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.with_suffix(".lock").stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_bound_session_refuses_cross_origin_or_protocol_downgrade(self):
        """Never attach the DeepSeek bearer token to a different host."""
        with GuardedSession(origin="https://chat.example.invalid",
                            gate=OriginGate(interval=0)) as client:
            with patch.object(requests.Session, "request") as send:
                for url in (
                    "http://chat.example.invalid/api/v0",
                    "https://other.example.invalid/api/v0",
                    "https://chat.example.invalid.evil.invalid/api/v0",
                    "https://chat.example.invalid:444/api/v0",
                    "https://chat.example.invalid:0/api/v0",
                    "https://guest:password@chat.example.invalid/api/v0",
                ):
                    with self.subTest(url=url), self.assertRaises(requests.RequestException):
                        client.post(url, headers={"Authorization": "Bearer private-test"})
                send.assert_not_called()

    def test_bound_session_accepts_same_origin(self):
        with GuardedSession(origin="https://chat.example.invalid",
                            gate=OriginGate(interval=0)) as client:
            with patch.object(requests.Session, "request", return_value=response(200)) as send:
                result = client.post("https://chat.example.invalid/api/v0/test")
                self.assertEqual(result.status_code, 200)
                send.assert_called_once()

    def test_default_https_port_does_not_count_as_another_origin(self):
        with GuardedSession(origin="https://chat.example.invalid",
                            gate=OriginGate(interval=0)) as client:
            with patch.object(requests.Session, "request", return_value=response(200)) as send:
                self.assertEqual(client.get("https://chat.example.invalid:443/same").status_code, 200)
                send.assert_called_once()

    def test_equivalent_origin_spellings_share_the_same_cooldown(self):
        import request_guard

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(request_guard, "_GATES", {}), \
                 patch.object(request_guard, "_cooldown_path",
                              side_effect=lambda key: Path(directory) / ("same.json" if key ==
                                  "https://chat.example.invalid" else "different.json")):
                canonical = request_guard.gate_for_origin("https://chat.example.invalid")
                canonical.after(response(429, {"Retry-After": "120"}))
                equivalent = request_guard.gate_for_origin("HTTPS://CHAT.EXAMPLE.INVALID:443/path")
                self.assertIs(equivalent, canonical)
                with self.assertRaises(RateLimitPause):
                    equivalent.before()

    def test_repeated_429_increases_cooldown_across_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "errors.json"
            now = [1000.0]

            def next_gate():
                return OriginGate(interval=0, clock=lambda: now[0],
                                  wall_clock=lambda: now[0], cooldown_file=path)

            next_gate().after(response(429))
            with self.assertRaises(RateLimitPause) as first:
                next_gate().before()
            self.assertGreaterEqual(first.exception.retry_after, 59)
            now[0] += 61
            next_gate().after(response(429))
            with self.assertRaises(RateLimitPause) as second:
                next_gate().before()
            self.assertGreaterEqual(second.exception.retry_after, 119)
            now[0] += 121
            next_gate().after(response(429))
            with self.assertRaises(RateLimitPause) as third:
                next_gate().before()
            self.assertGreaterEqual(third.exception.retry_after, 239)

    def test_repeated_403_escalates_and_long_retry_after_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "errors.json"
            now = [1000.0]

            def next_gate():
                return OriginGate(interval=0, clock=lambda: now[0],
                                  wall_clock=lambda: now[0], cooldown_file=path)

            next_gate().after(response(403))
            now[0] += 601
            next_gate().after(response(403))
            with self.assertRaises(RateLimitPause) as second:
                next_gate().before()
            self.assertGreaterEqual(second.exception.retry_after, 1199)
            now[0] += 1201
            next_gate().after(response(403, {"Retry-After": "7200"}))
            with self.assertRaises(RateLimitPause) as third:
                next_gate().before()
            self.assertGreaterEqual(third.exception.retry_after, 7199)

    def test_invalid_escalation_state_refuses_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "errors.json"
            for invalid in ({"error_streak": -1}, {"error_streak": "bad"},
                            {"last_error_at": -100}, {"last_error_at": "NaN"}):
                with self.subTest(value=invalid):
                    path.write_text(json.dumps({"not_before": 0, "next_request": 0,
                                                **invalid}), encoding="utf-8")
                    with self.assertRaises(RateLimitStateError):
                        OriginGate(interval=0, cooldown_file=path).before()

    def test_unavailable_upstream_503_respects_retry_after(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.0]
            path = Path(directory) / "outage.json"
            gate = OriginGate(interval=0, clock=lambda: now[0],
                              wall_clock=lambda: now[0], cooldown_file=path)
            gate.after(response(503, {"Retry-After": "120"}))
            restarted = OriginGate(interval=0, clock=lambda: now[0],
                                   wall_clock=lambda: now[0], cooldown_file=path)
            with self.assertRaises(RateLimitPause) as blocked:
                restarted.before()
            self.assertGreaterEqual(blocked.exception.retry_after, 119)

    def test_repeated_server_errors_use_conservative_cooldown(self):
        for code in (500, 502, 503, 504):
            with self.subTest(status=code):
                now = [1000.0]
                gate = OriginGate(interval=0, clock=lambda: now[0])
                gate.after(response(code))
                with self.assertRaises(RateLimitPause) as blocked:
                    gate.before()
                self.assertGreaterEqual(blocked.exception.retry_after, 29)

    def test_configuration_may_tighten_but_not_disable_default_limits(self):
        import request_guard

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(request_guard, "_GATES", {}), \
                 patch.object(request_guard, "_cooldown_path",
                              return_value=Path(directory) / "settings.json"), \
                 patch.dict(os.environ, {
                     "DEEPSEEK_MIN_REQUEST_INTERVAL": "0",
                     "DEEPSEEK_MAX_REQUESTS_PER_HOUR": "999999",
                     "DEEPSEEK_MAX_REQUESTS_PER_DAY": "0",
                 }):
                gate = request_guard.gate_for_origin("https://example.invalid")
                self.assertGreaterEqual(gate.interval, 2.0)
                self.assertEqual(gate.hourly_limit, 90)
                self.assertEqual(gate.daily_limit, 400)
            with patch.object(request_guard, "_GATES", {}), \
                 patch.object(request_guard, "_cooldown_path",
                              return_value=Path(directory) / "settings2.json"), \
                 patch.dict(os.environ, {
                     "DEEPSEEK_MIN_REQUEST_INTERVAL": "10",
                     "DEEPSEEK_MAX_REQUESTS_PER_HOUR": "5",
                     "DEEPSEEK_MAX_REQUESTS_PER_DAY": "15",
                 }):
                gate = request_guard.gate_for_origin("https://example.invalid")
                self.assertEqual(gate.interval, 10.0)
                self.assertEqual(gate.hourly_limit, 5)
                self.assertEqual(gate.daily_limit, 15)

    def test_default_gateway_restricts_concurrent_requests(self):
        from openai_server import Gateway, build_parser

        server = Gateway(("127.0.0.1", 0))
        try:
            self.assertEqual(build_parser().parse_args([]).max_parallel, 1)
            self.assertTrue(server.slots.acquire(blocking=False))
            self.assertFalse(server.slots.acquire(blocking=False))
            server.slots.release()
        finally:
            server.server_close()

    def test_rolling_hour_quota_is_shared_and_stops_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100_000.0]
            path = Path(directory) / "limits.json"
            for _ in range(2):
                OriginGate(interval=0, clock=lambda: now[0],
                           wall_clock=lambda: now[0], cooldown_file=path,
                           hourly_limit=2, daily_limit=10).before()
            new_gate = OriginGate(interval=0, clock=lambda: now[0],
                                  wall_clock=lambda: now[0], cooldown_file=path,
                                  hourly_limit=2, daily_limit=10)
            client = GuardedSession(gate=new_gate)
            try:
                with patch.object(requests.Session, "request") as send:
                    with self.assertRaises(RateLimitPause) as paused:
                        client.get("https://example.invalid/safe")
                    self.assertGreaterEqual(paused.exception.retry_after, 3599)
                    send.assert_not_called()
                now[0] += 3601
                with patch.object(requests.Session, "request", return_value=response(200)) as send:
                    self.assertEqual(client.get("https://example.invalid/safe").status_code, 200)
                    send.assert_called_once()
            finally:
                client.close()

    def test_rolling_day_quota_survives_hour_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100_000.0]
            path = Path(directory) / "limits.json"
            for _ in range(3):
                OriginGate(interval=0, clock=lambda: now[0], wall_clock=lambda: now[0],
                           cooldown_file=path, hourly_limit=10, daily_limit=3).before()
            now[0] += 3601
            with self.assertRaises(RateLimitPause) as blocked:
                OriginGate(interval=0, clock=lambda: now[0], wall_clock=lambda: now[0],
                           cooldown_file=path, hourly_limit=10, daily_limit=3).before()
            self.assertGreater(blocked.exception.retry_after, 80_000)
            now[0] += 82_801
            OriginGate(interval=0, clock=lambda: now[0], wall_clock=lambda: now[0],
                       cooldown_file=path, hourly_limit=10, daily_limit=3).before()

    def test_rolling_budget_is_enforced_by_separate_python_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.json"
            for _ in range(2):
                OriginGate(interval=0, wall_clock=lambda: 100000,
                           cooldown_file=path, hourly_limit=2, daily_limit=5).before()
            script = (
                "from pathlib import Path\n"
                "from request_guard import OriginGate, RateLimitPause\n"
                "import sys\n"
                "try:\n"
                "    OriginGate(interval=0, wall_clock=lambda: 100000, "
                "cooldown_file=Path(sys.argv[1]), hourly_limit=2, daily_limit=5).before()\n"
                "except RateLimitPause:\n"
                "    print('QUOTA_ENFORCED')\n"
                "else:\n"
                "    raise SystemExit('shared quota was bypassed')\n"
            )
            result = subprocess.run([sys.executable, "-c", script, str(path)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("QUOTA_ENFORCED", result.stdout)

    def test_legacy_cooldown_state_upgrades_without_losing_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-format.json"
            path.write_text(json.dumps({"not_before": 10050, "next_request": 10060}),
                            encoding="utf-8")
            now = [10000]
            gate = OriginGate(interval=0, wall_clock=lambda: now[0],
                              cooldown_file=path)
            with self.assertRaises(RateLimitPause):
                gate.before()
            now[0] = 10100
            gate.before()
            upgraded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(upgraded["request_times"]), 1)
            self.assertEqual(upgraded["not_before"], 10050)

    def test_invalid_or_corrupt_rolling_quota_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "limits.json"
            for invalid in ("x", None, ["NaN"], [-1], [12, 11], [float("inf")]):
                with self.subTest(value=invalid):
                    state = {"not_before": 0, "next_request": 0, "request_times": invalid}
                    path.write_text(json.dumps(state), encoding="utf-8")
                    with self.assertRaises(RateLimitStateError):
                        OriginGate(interval=0, cooldown_file=path).before()

    def test_malformed_retry_after_never_clears_default_cooldown(self):
        for header in ("-5", "NaN", "Infinity", "not-a-date", "0"):
            with self.subTest(value=header):
                now = [100.0]
                gate = OriginGate(interval=0, clock=lambda: now[0])
                gate.after(response(429, {"Retry-After": header}))
                with self.assertRaises(RateLimitPause) as pause:
                    gate.before()
                self.assertGreaterEqual(pause.exception.retry_after, 59.0)

    def test_existing_state_missing_deadline_fields_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cooldown.json"
            for state in ('{}', '{"not_before": 10}', '{"next_request": 10}'):
                with self.subTest(value=state):
                    path.write_text(state, encoding="utf-8")
                    with self.assertRaises(RateLimitStateError):
                        OriginGate(interval=0, cooldown_file=path).before()

    def test_redirect_does_not_create_unpaced_second_http_request(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/end")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with GuardedSession(gate=OriginGate(interval=0)) as client:
                answer = client.get(f"http://127.0.0.1:{server.server_port}/start", timeout=3)
            self.assertEqual(answer.status_code, 302)
            self.assertEqual(seen, ["/start"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_explicit_redirect_setting_cannot_bypass_guard(self):
        with GuardedSession(gate=OriginGate(interval=0)) as client:
            with patch.object(requests.Session, "request", return_value=response(302)) as send:
                result = client.get("https://example.invalid/start", allow_redirects=True)
                self.assertEqual(result.status_code, 302)
                send.assert_called_once()
                self.assertIs(send.call_args.kwargs["allow_redirects"], False)

    def test_deepseek_client_carries_upstream_pause_across_instances(self):
        from api_client import DeepSeekAPIError, DeepSeekClient

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deepseek-pause.json"
            # This test exercises real client HTTP routing, but never accesses
            # any network or touches the user's normal shared limits.
            gate = OriginGate(interval=0, cooldown_file=path)
            with patch("request_guard.gate_for_origin", return_value=gate), \
                 patch("credential_store.load_credentials", return_value={}), \
                 patch.object(requests.Session, "request", return_value=response(429, {"Retry-After": "75"})) as send:
                first = DeepSeekClient(token="offline-test-token", base_url="https://example.invalid")
                try:
                    with self.assertRaises(DeepSeekAPIError):
                        first._post_json("/api/v0/chat_session/create", {})
                finally:
                    first.session.close()
                self.assertEqual(send.call_count, 1)

            # Simulate restarting the gateway by using a new in-memory gate.
            restarted = OriginGate(interval=0, cooldown_file=path)
            with patch("request_guard.gate_for_origin", return_value=restarted), \
                 patch("credential_store.load_credentials", return_value={}), \
                 patch.object(requests.Session, "request") as send:
                second = DeepSeekClient(token="offline-test-token", base_url="https://example.invalid")
                try:
                    with self.assertRaises(RateLimitPause):
                        second._post_json("/api/v0/chat_session/create", {})
                    send.assert_not_called()
                finally:
                    second.session.close()

    def test_corrupt_persisted_state_fails_closed_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pause.json"
            client = GuardedSession(gate=OriginGate(interval=0, cooldown_file=path))
            try:
                for bad_contents in (
                    "{broken", "", "[]", "null", '{"not_before": "NaN"}',
                    '{"not_before": -1}', '{"next_request": "infinity"}',
                ):
                    with self.subTest(contents=bad_contents):
                        path.write_text(bad_contents, encoding="utf-8")
                        with patch.object(requests.Session, "request") as send:
                            with self.assertRaises(RateLimitStateError):
                                client.post("https://example.invalid/resource")
                            send.assert_not_called()
            finally:
                client.close()

    def test_missing_state_file_is_a_valid_first_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pause.json"
            self.assertFalse(path.exists())
            client = GuardedSession(gate=OriginGate(interval=0, cooldown_file=path))
            try:
                with patch.object(requests.Session, "request", return_value=response(200)) as send:
                    self.assertEqual(client.get("https://example.invalid/resource").status_code, 200)
                    send.assert_called_once()
                self.assertTrue(path.exists())
            finally:
                client.close()

    def test_unavailable_shared_state_fails_closed_without_network_call(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = OriginGate(cooldown_file=Path(directory) / "pause.json")
            client = GuardedSession(gate=gate)
            with patch("request_guard._locked_file", side_effect=PermissionError("readonly")), \
                 patch.object(requests.Session, "request") as send:
                with self.assertRaises(RateLimitStateError):
                    client.get("https://example.invalid/resource")
                send.assert_not_called()
            client.close()

    def test_server_retry_after_survives_new_process_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [100.0]
            wall_clock = [10_000.0]
            path = Path(directory) / "pause.json"
            initial = OriginGate(interval=0, clock=lambda: clock[0],
                                 wall_clock=lambda: wall_clock[0], cooldown_file=path)
            initial.after(response(429, {"Retry-After": "120"}))
            recreated = OriginGate(interval=0, clock=lambda: clock[0],
                                   wall_clock=lambda: wall_clock[0], cooldown_file=path)
            with self.assertRaises(RateLimitPause) as pause:
                recreated.before()
            self.assertGreaterEqual(pause.exception.retry_after, 119)
            wall_clock[0] += 121
            recreated.before()

    def test_longer_existing_cooldown_is_not_shortened(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pause.json"
            gate = OriginGate(interval=0, clock=lambda: 100, wall_clock=lambda: 1000,
                              cooldown_file=path)
            gate.after(response(429, {"Retry-After": "180"}))
            other = OriginGate(interval=0, clock=lambda: 100, wall_clock=lambda: 1000,
                               cooldown_file=path)
            other.after(response(429, {"Retry-After": "10"}))
            reloaded = OriginGate(interval=0, clock=lambda: 100, wall_clock=lambda: 1000,
                                  cooldown_file=path)
            with self.assertRaises(RateLimitPause) as pause:
                reloaded.before()
            self.assertGreaterEqual(pause.exception.retry_after, 179)

    def test_new_process_respects_other_process_request_spacing(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.0]
            delays = []
            path = Path(directory) / "pacing.json"

            def pause(seconds):
                delays.append(seconds)
                now[0] += seconds

            def make_gate():
                return OriginGate(interval=3.0, clock=lambda: now[0],
                                  wall_clock=lambda: now[0], sleep=pause,
                                  cooldown_file=path)

            make_gate().before()
            make_gate().before()
            self.assertEqual(delays, [3.0])
            self.assertEqual(now[0], 1003.0)

    def test_separate_python_process_honors_saved_retry_after(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pause.json"
            OriginGate(interval=0, cooldown_file=path).after(
                response(429, {"Retry-After": "90"})
            )
            script = (
                "from pathlib import Path\n"
                "from request_guard import OriginGate, RateLimitPause\n"
                "try:\n"
                "    OriginGate(interval=0, cooldown_file=Path(__import__('sys').argv[1])).before()\n"
                "except RateLimitPause:\n"
                "    print('PAUSED_ACROSS_PROCESS')\n"
                "else:\n"
                "    raise SystemExit('pause was lost')\n"
            )
            result = subprocess.run([sys.executable, "-c", script, str(path)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PAUSED_ACROSS_PROCESS", result.stdout)

    def test_cooldown_created_while_waiting_cancels_reserved_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pause.json"
            now = [1000.0]
            leader = OriginGate(interval=3, clock=lambda: now[0],
                                wall_clock=lambda: now[0], cooldown_file=path)
            leader.before()

            def pause(seconds):
                now[0] += seconds
                OriginGate(interval=0, clock=lambda: now[0],
                           wall_clock=lambda: now[0], cooldown_file=path).after(
                               response(429, {"Retry-After": "120"}))

            follower = OriginGate(interval=3, clock=lambda: now[0],
                                  wall_clock=lambda: now[0], sleep=pause, cooldown_file=path)
            with self.assertRaises(RateLimitPause):
                follower.before()

    def test_successful_calls_are_paced_across_sessions(self):
        clock = [100.0]
        sleeps = []

        def pause(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        gate = OriginGate(interval=2.0, clock=lambda: clock[0], sleep=pause)
        first = GuardedSession(gate=gate)
        second = GuardedSession(gate=gate)
        try:
            with patch.object(requests.Session, "request", return_value=response(200)) as send:
                first.post("https://example.invalid/one")
                second.get("https://example.invalid/two")
                self.assertEqual(send.call_count, 2)
            self.assertEqual(sleeps, [2.0])
        finally:
            first.close()
            second.close()

    def test_retry_after_429_stops_followup_without_second_request(self):
        clock = [100.0]
        gate = OriginGate(interval=0, clock=lambda: clock[0])
        session = GuardedSession(gate=gate)
        try:
            with patch.object(requests.Session, "request", return_value=response(429, {"Retry-After": "120"})) as send:
                first = session.post("https://example.invalid/data")
                self.assertEqual(first.status_code, 429)
                with self.assertRaises(RateLimitPause) as ex:
                    session.get("https://example.invalid/data")
                self.assertGreaterEqual(ex.exception.retry_after, 119)
                self.assertEqual(send.call_count, 1)
            clock[0] += 121
            with patch.object(requests.Session, "request", return_value=response(200)) as send:
                self.assertEqual(session.get("https://example.invalid/data").status_code, 200)
                send.assert_called_once()
        finally:
            session.close()

    def test_429_without_retry_after_uses_cooldown(self):
        gate = OriginGate(interval=0, clock=lambda: 100)
        with patch.object(requests.Session, "request", return_value=response(429)):
            client = GuardedSession(gate=gate)
            self.assertEqual(client.post("https://example.invalid/").status_code, 429)
            with self.assertRaises(RateLimitPause):
                client.post("https://example.invalid/")
            client.close()

    def test_protected_403_does_not_send_again(self):
        gate = OriginGate(interval=0, clock=lambda: 100)
        with patch.object(requests.Session, "request", return_value=response(403)) as send:
            client = GuardedSession(gate=gate)
            self.assertEqual(client.post("https://example.invalid/").status_code, 403)
            with self.assertRaises(RateLimitPause):
                client.post("https://example.invalid/")
            self.assertEqual(send.call_count, 1)
            client.close()


if __name__ == "__main__":
    unittest.main()
