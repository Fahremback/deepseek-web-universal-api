"""Conservative HTTP pacing and stop-on-limit handling for web sessions.

This module does not solve challenges, obscure automation, or retry blocked
requests. The user must complete any challenge on the service's own website.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit

import requests


class RateLimitPause(requests.RequestException):
    """A server cooldown or conservative local usage budget is active."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(0.0, retry_after)
        super().__init__(f"DeepSeek requests paused; retry after {math.ceil(self.retry_after)} seconds")


class RateLimitStateError(requests.RequestException):
    """The shared state is inaccessible: refuse to send rather than discard limits."""


def _cooldown_path(origin: str) -> Path:
    """No account data is stored, only a wall-clock pause deadline per origin."""
    if os.name == "nt":
        parent = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif os.sys.platform == "darwin":
        parent = Path.home() / "Library" / "Application Support"
    else:
        parent = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:24]
    return parent / "DeepSeekWebClient" / f"cooldown-{digest}.json"


@contextmanager
def _locked_file(path: Path):
    """Lock across processes before reading/updating one deadline file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix" and path.parent.name == "DeepSeekWebClient":
        # Tighten permissions on pre-existing application state directories.
        # Do not chmod arbitrary caller-provided parents (such as /tmp).
        path.parent.chmod(0o700)
    with path.with_suffix(".lock").open("a+b") as lock:
        if os.name == "posix":
            os.fchmod(lock.fileno(), 0o600)
        if os.name == "nt":
            import msvcrt
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_state(path: Path) -> dict[str, float | list[float]]:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # First use is not corruption; only a missing file starts with zero.
        return {"not_before": 0.0, "next_request": 0.0, "request_times": [],
                "error_streak": 0, "last_error_at": 0.0}
    except UnicodeError as exc:
        raise RateLimitStateError("Unreadable shared request-limit state; request not sent") from exc
    # Existing state must never reset the limit just because it is invalid.
    try:
        payload = json.loads(contents)
        if not isinstance(payload, dict):
            raise ValueError("state is not an object")
        if not {"not_before", "next_request"}.issubset(payload):
            raise ValueError("missing request-limit timestamps")
        state = {}
        for key in ("not_before", "next_request"):
            value = float(payload[key])
            if not math.isfinite(value) or value < 0:
                raise ValueError("invalid request-limit timestamp")
            state[key] = value
        # Support pre-budget state files without removing existing cooldowns.
        # Keep a rolling 24-hour ledger of attempted HTTP requests. No prompt,
        # token, cookie, IP, URL path, or account identifier is persisted.
        stamps = payload.get("request_times", [])
        if not isinstance(stamps, list) or len(stamps) > 10_000:
            raise ValueError("invalid request ledger")
        previous = -1.0
        for stamp in stamps:
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
                raise ValueError("invalid request timestamp")
            if not math.isfinite(stamp) or stamp < 0 or stamp < previous:
                raise ValueError("invalid request timestamp order")
            previous = stamp
        state["request_times"] = stamps
        try:
            streak = payload.get("error_streak", 0)
            last_error_at = float(payload.get("last_error_at", 0))
            if (isinstance(streak, bool) or not isinstance(streak, int)
                    or not 0 <= streak <= 100 or not math.isfinite(last_error_at)
                    or last_error_at < 0):
                raise ValueError("invalid upstream error history")
            state["error_streak"] = streak
            state["last_error_at"] = last_error_at
        except (ValueError, TypeError, OverflowError) as exc:
            raise RateLimitStateError("Corrupt shared request-limit state; request not sent") from exc
        return state
    except (ValueError, TypeError, OverflowError) as exc:
        raise RateLimitStateError("Corrupt shared request-limit state; request not sent") from exc


def _write_state(path: Path, state: dict[str, float | list[float]]) -> None:
    """Atomic replacement ensures a crash never leaves a truncated pause file."""
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".cooldown-", delete=False) as output:
            temporary = output.name
            json.dump(state, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class OriginGate:
    def __init__(self, *, interval: float = 2.0, clock=None, sleep=None,
                 wall_clock=None, cooldown_file: Path | None = None,
                 hourly_limit: int = 90, daily_limit: int = 400) -> None:
        self.lock = threading.RLock()
        self.interval = max(0.0, float(interval))
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.wall_clock = wall_clock or time.time
        self.cooldown_file = cooldown_file
        self.hourly_limit = max(1, int(hourly_limit))
        self.daily_limit = max(1, int(daily_limit))
        self.next_at = 0.0
        self.cooldown_until = 0.0
        self.request_times: list[float] = []
        self.error_streak = 0
        self.last_error_at = 0.0

    def before(self) -> None:
        now = self.clock()
        remaining = max(0.0, self.cooldown_until - now)
        delay = max(0.0, self.next_at - now)
        persisted = False
        if self.cooldown_file is not None:
            try:
                with _locked_file(self.cooldown_file):
                    state = _read_state(self.cooldown_file)
                    wall_now = self.wall_clock()
                    remaining = max(remaining, state["not_before"] - wall_now)
                    if remaining <= 0:
                        stamps = [stamp for stamp in state["request_times"]
                                  if stamp > wall_now - 86400]
                        last_hour = [stamp for stamp in stamps if stamp > wall_now - 3600]
                        hourly_pause = (last_hour[-self.hourly_limit] + 3600 - wall_now
                                        if len(last_hour) >= self.hourly_limit else 0.0)
                        daily_pause = (stamps[-self.daily_limit] + 86400 - wall_now
                                       if len(stamps) >= self.daily_limit else 0.0)
                        if max(hourly_pause, daily_pause) > 0:
                            raise RateLimitPause(max(hourly_pause, daily_pause))
                        delay = max(delay, state["next_request"] - wall_now)
                        state["next_request"] = wall_now + delay + self.interval
                        stamps.append(wall_now + delay)
                        state["request_times"] = stamps
                        _write_state(self.cooldown_file, state)
                        persisted = True
            except RateLimitPause:
                raise
            except OSError as exc:
                raise RateLimitStateError("Cannot verify shared request limits; request not sent") from exc
        else:
            wall_now = self.wall_clock()
            self.request_times = [stamp for stamp in self.request_times
                                  if stamp > wall_now - 86400]
            last_hour = [stamp for stamp in self.request_times if stamp > wall_now - 3600]
            hourly_pause = (last_hour[-self.hourly_limit] + 3600 - wall_now
                            if len(last_hour) >= self.hourly_limit else 0.0)
            daily_pause = (self.request_times[-self.daily_limit] + 86400 - wall_now
                           if len(self.request_times) >= self.daily_limit else 0.0)
            if max(hourly_pause, daily_pause) > 0:
                raise RateLimitPause(max(hourly_pause, daily_pause))
            self.request_times.append(wall_now + delay)
        if remaining > 0:
            raise RateLimitPause(remaining)
        if delay > 0:
            self.sleep(delay)
        self.next_at = self.clock() + self.interval
        if persisted:
            # Another process may have received a new server cooldown while
            # we were sleeping. Recheck before allowing any network traffic.
            try:
                with _locked_file(self.cooldown_file):
                    remaining = _read_state(self.cooldown_file)["not_before"] - self.wall_clock()
            except OSError as exc:
                raise RateLimitStateError("Cannot verify shared request limits; request not sent") from exc
            if remaining > 0:
                raise RateLimitPause(remaining)

    def after(self, response: requests.Response) -> None:
        if response.status_code not in (403, 429, 500, 502, 503, 504):
            return
        # 403 can indicate a CAPTCHA, WAF challenge or suspension. Do not
        # automatically re-authenticate or resubmit it.
        defaults = {403: 600.0, 429: 60.0}
        seconds = defaults.get(response.status_code, 30.0)
        raw = response.headers.get("Retry-After", "").strip()
        if raw:
            try:
                seconds = float(raw)
            except ValueError:
                try:
                    deadline = parsedate_to_datetime(raw)
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=timezone.utc)
                    seconds = (deadline - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    pass
        if not math.isfinite(seconds) or seconds <= 0:
            seconds = defaults.get(response.status_code, 30.0)
        seconds = max(0.0, seconds)
        wall_now = self.wall_clock()
        if response.status_code in (403, 429) and self.cooldown_file is None:
            self.error_streak = (min(100, self.error_streak + 1)
                                 if wall_now - self.last_error_at <= 86400 else 1)
            self.last_error_at = wall_now
            base, ceiling = ((600.0, 86400.0) if response.status_code == 403
                             else (60.0, 3600.0))
            seconds = max(seconds, min(ceiling, base * (2 ** min(self.error_streak - 1, 12))))
        if self.cooldown_file is not None:
            try:
                with _locked_file(self.cooldown_file):
                    state = _read_state(self.cooldown_file)
                    if response.status_code in (403, 429):
                        streak = (min(100, state["error_streak"] + 1)
                                  if wall_now - state["last_error_at"] <= 86400 else 1)
                        state["error_streak"] = streak
                        state["last_error_at"] = wall_now
                        base, ceiling = ((600.0, 86400.0) if response.status_code == 403
                                         else (60.0, 3600.0))
                        seconds = max(seconds, min(ceiling, base * (2 ** min(streak - 1, 12))))
                    state["not_before"] = max(state["not_before"], wall_now + seconds)
                    _write_state(self.cooldown_file, state)
            except OSError as exc:
                raise RateLimitStateError("Cannot persist server cooldown; automation halted") from exc
        self.cooldown_until = max(self.cooldown_until, self.clock() + seconds)


_GATES: dict[str, OriginGate] = {}
_GATES_LOCK = threading.Lock()


def _origin_identity(url: str) -> tuple[str, str, int]:
    """Canonical protocol, host and port for credential-safe origin checks."""
    try:
        parsed = urlsplit(url)
        scheme, host = parsed.scheme.lower(), parsed.hostname
        port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
        if (scheme not in {"http", "https"} or not host or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or port not in range(1, 65536)):
            raise ValueError("invalid HTTP origin")
        return (scheme, host.lower(), port)
    except ValueError as exc:
        raise requests.RequestException("Invalid HTTP request origin") from exc


def gate_for_origin(url: str) -> OriginGate:
    scheme, host, port = _origin_identity(url)
    normalized_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    # Keep the historic key and persisted file for the normal DeepSeek URL,
    # while sharing its cooldown with equivalent :443 and case variants.
    key = f"{scheme}://{normalized_host}"
    if port != default_port:
        key += f":{port}"
    with _GATES_LOCK:
        if key not in _GATES:
            try:
                interval = float(os.getenv("DEEPSEEK_MIN_REQUEST_INTERVAL", "2"))
            except ValueError:
                interval = 2.0
            # A zero/negative interval must never silently disable pacing.
            # Users may make it slower, but not less conservative than 2s.
            if not math.isfinite(interval) or interval < 2.0:
                interval = 2.0
            def safe_budget(name: str, default: int) -> int:
                try:
                    requested = int(os.getenv(name, str(default)))
                except ValueError:
                    return default
                # Allow tighter limits, but never silently disable these
                # protective defaults with zero, negatives or larger values.
                return requested if 1 <= requested <= default else default

            _GATES[key] = OriginGate(
                interval=interval, cooldown_file=_cooldown_path(key),
                hourly_limit=safe_budget("DEEPSEEK_MAX_REQUESTS_PER_HOUR", 90),
                daily_limit=safe_budget("DEEPSEEK_MAX_REQUESTS_PER_DAY", 400))
        return _GATES[key]


class GuardedSession(requests.Session):
    """Enforce the same gate for every verb, including uploads and SSE setup."""

    def __init__(self, *, origin: str = "", gate: OriginGate | None = None) -> None:
        super().__init__()
        self._bound_origin = _origin_identity(origin) if origin else None
        self._gate = gate or (gate_for_origin(origin) if origin else None)

    def request(self, method, url, **kwargs):
        target_origin = _origin_identity(url)
        if self._bound_origin is not None and target_origin != self._bound_origin:
            raise requests.RequestException("Request target differs from the authenticated origin")
        # requests.Session follows redirects *inside* the parent request()
        # without entering this guard a second time. Disable automatic hops
        # rather than issue unpaced traffic (possibly to another origin).
        # requests.Session.get() supplies allow_redirects=True even when the
        # caller did not request redirects. Always disable the internal hop.
        kwargs["allow_redirects"] = False
        gate = self._gate or gate_for_origin(url)
        # Also serialize request starts for sessions sharing an origin. This
        # protects the underlying requests.Session from overlapping calls.
        with gate.lock:
            gate.before()
            response = super().request(method, url, **kwargs)
            try:
                gate.after(response)
            except RateLimitStateError:
                response.close()
                raise
            return response
