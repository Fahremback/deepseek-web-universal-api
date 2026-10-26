#!/usr/bin/env python3
"""
DeepSeek web chat API client (reverse-engineered from recording.har).

Protocol implemented
--------------------
1. POST /api/v0/chat/create_pow_challenge  -> PoW challenge for a target path
2. Solve DeepSeekHashV1 (SHA3-256 variant, Keccak-f[1600] rounds 1..23)
   - prefix = f"{salt}_{expire_at}_"
   - answer = first nonce in [0, difficulty) such that
              DeepSeekHashV1(prefix + str(answer)) == challenge
   - x-ds-pow-response = base64(json({algorithm, challenge, salt, answer,
                                     signature, target_path}))
3. POST /api/v0/chat_session/create        -> chat_session.id
4. POST /api/v0/chat/completion            -> text/event-stream patches

Authentication
--------------
The HAR was captured with placeholders for private credentials. Supply a
Bearer token once through the environment, a token file, or --token. After
the first successful authenticated request, it is saved in the OS credential
vault and automatically reused on later runs. No browser is required.

    DEEPSEEK_TOKEN          required only for initial credential provisioning
    DEEPSEEK_DEVICE_ID      optional; falls back to the device id observed in
                            the companion browser capture
    DEEPSEEK_COOKIES        optional raw Cookie header
    DEEPSEEK_HIF_LEIM       optional x-hif-leim value (browser WAF proof)
    DEEPSEEK_TOKEN_FILE     optional file containing the token (also used to
                            refresh after a 401/403)
    DEEPSEEK_LOCALE         optional, default pt_BR
    DEEPSEEK_TIMEZONE_OFFSET optional, seconds east of UTC

Optional HTTP account login at /api/v0/users/login can refresh expired tokens
using an explicitly saved account login from the Windows credential vault.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
from io import BytesIO
import json
import mimetypes
import os
import platform
import shutil
import struct
import subprocess
import sys
import threading
import time

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit("requests is required: python -m pip install requests") from exc


# ---------------------------------------------------------------------------
# Constants taken from the HAR
# ---------------------------------------------------------------------------

BASE_URL = "https://chat.deepseek.com"
LOGIN_PATH = "/api/v0/users/login"
POW_PATH = "/api/v0/chat/create_pow_challenge"
SESSION_CREATE_PATH = "/api/v0/chat_session/create"
COMPLETION_PATH = "/api/v0/chat/completion"
CONTINUE_PATH = "/api/v0/chat/continue"
FILE_UPLOAD_PATH = "/api/v0/file/upload_file"
FILE_FETCH_PATH = "/api/v0/file/fetch_files"
DEFAULT_TARGET_PATH = COMPLETION_PATH
FILE_FAILURE_STATUSES = {
    "FAILED", "ERROR", "CONTENT_FILTER", "CONTENT_TOO_LONG",
    "CANCELLED", "CONTENT_EMPTY", "_CUSTOM_SYSTEM_ERROR_FAIL",
    "_CUSTOM_FROM_SHARE",
}
CLIENT_VERSION = "2.7.0"
CLIENT_BUNDLE_ID = "com.deepseek.chat"
DEFAULT_LOCALE = "pt_BR"
# The target HAR had ${DEEPSEEK_DEVICE_ID}.  The companion unauthenticated
# browser capture exposed this client-generated device id; it is a harmless
# fallback and can be overridden with DEEPSEEK_DEVICE_ID.
DEFAULT_DEVICE_ID = "0d060fa3-0e2a-47bd-9096-f7a6b728826a"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"'

SCRIPT_DIR = Path(__file__).resolve().parent
_AUTH_REFRESH_LOCK = threading.RLock()
SOLVER_EXE = SCRIPT_DIR / ("ds_pow_solver.exe" if os.name == "nt" else "ds_pow_solver")
SOLVER_SRC = SCRIPT_DIR / "ds_pow_solver.c"

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DeepSeekError(Exception):
    """Base class for client errors."""


class AuthenticationError(DeepSeekError):
    """Missing, invalid, or expired credentials."""


class DeepSeekAPIError(DeepSeekError):
    """The API returned a non-zero code or an HTTP error."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[Any] = None,
        biz_code: Optional[Any] = None,
        trace_id: Optional[str] = None,
        payload: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.biz_code = biz_code
        self.trace_id = trace_id
        self.payload = payload

    def __str__(self) -> str:
        bits = [super().__str__()]
        if self.status is not None:
            bits.append(f"http_status={self.status}")
        if self.code not in (None, 0):
            bits.append(f"code={self.code}")
        if self.biz_code not in (None, 0):
            bits.append(f"biz_code={self.biz_code}")
        if self.trace_id:
            bits.append(f"trace_id={self.trace_id}")
        return " | ".join(bits)


class ProofOfWorkError(DeepSeekError):
    """The PoW challenge could not be solved."""


# ---------------------------------------------------------------------------
# Embedded native PoW solver
# ---------------------------------------------------------------------------
# Kept as a string so a single-file copy can still bootstrap the native solver.
# A precompiled executable and the .c source are also written next to this file.

C_SOURCE = r"""
/*
 * DeepSeekHashV1 proof-of-work solver.
 *
 * The web client uses a SHA3-256 variant whose Keccak-f[1600] permutation
 * skips round 0 (rounds 1..23 only). The server picks a nonce in
 * [0, difficulty) and publishes:
 *
 *     challenge = DeepSeekHashV1(salt + "_" + expire_at + "_" + nonce)
 *
 * Build:  gcc -O3 -o ds_pow_solver ds_pow_solver.c
 * Usage:  ds_pow_solver solve <challenge_hex> <salt> <expire_at> <difficulty>
 * Output: the nonce on stdout, or -1 if none was found.
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static const uint64_t RC[24] = {
    0x0000000000000001ULL, 0x0000000000008082ULL, 0x800000000000808AULL, 0x8000000080008000ULL,
    0x000000000000808BULL, 0x0000000080000001ULL, 0x8000000080008081ULL, 0x8000000000008009ULL,
    0x000000000000008AULL, 0x0000000000000088ULL, 0x0000000080008009ULL, 0x000000008000000AULL,
    0x000000008000808BULL, 0x800000000000008BULL, 0x8000000000008089ULL, 0x8000000000008003ULL,
    0x8000000000008002ULL, 0x8000000000000080ULL, 0x000000000000800AULL, 0x800000008000000AULL,
    0x8000000080008081ULL, 0x8000000000008080ULL, 0x0000000080000001ULL, 0x8000000080008008ULL
};

static inline uint64_t rotl(uint64_t v, unsigned k) {
    return k ? ((v << k) | (v >> (64 - k))) : v;
}

#define KECCAK_ROUND(s0,s1,s2,s3,s4,s5,s6,s7,s8,s9,s10,s11,s12,s13,s14,s15,s16,s17,s18,s19,s20,s21,s22,s23,s24, r) do { \
    uint64_t c0 = s0 ^ s5 ^ s10 ^ s15 ^ s20; \
    uint64_t c1 = s1 ^ s6 ^ s11 ^ s16 ^ s21; \
    uint64_t c2 = s2 ^ s7 ^ s12 ^ s17 ^ s22; \
    uint64_t c3 = s3 ^ s8 ^ s13 ^ s18 ^ s23; \
    uint64_t c4 = s4 ^ s9 ^ s14 ^ s19 ^ s24; \
    uint64_t d0 = c4 ^ rotl(c1, 1); \
    uint64_t d1 = c0 ^ rotl(c2, 1); \
    uint64_t d2 = c1 ^ rotl(c3, 1); \
    uint64_t d3 = c2 ^ rotl(c4, 1); \
    uint64_t d4 = c3 ^ rotl(c0, 1); \
    s0 ^= d0; s5 ^= d0; s10 ^= d0; s15 ^= d0; s20 ^= d0; \
    s1 ^= d1; s6 ^= d1; s11 ^= d1; s16 ^= d1; s21 ^= d1; \
    s2 ^= d2; s7 ^= d2; s12 ^= d2; s17 ^= d2; s22 ^= d2; \
    s3 ^= d3; s8 ^= d3; s13 ^= d3; s18 ^= d3; s23 ^= d3; \
    s4 ^= d4; s9 ^= d4; s14 ^= d4; s19 ^= d4; s24 ^= d4; \
    uint64_t b0 = s0; \
    uint64_t b10 = rotl(s1, 1); \
    uint64_t b20 = rotl(s2, 62); \
    uint64_t b5  = rotl(s3, 28); \
    uint64_t b15 = rotl(s4, 27); \
    uint64_t b16 = rotl(s5, 36); \
    uint64_t b1  = rotl(s6, 44); \
    uint64_t b11 = rotl(s7, 6); \
    uint64_t b21 = rotl(s8, 55); \
    uint64_t b6  = rotl(s9, 20); \
    uint64_t b7  = rotl(s10, 3); \
    uint64_t b17 = rotl(s11, 10); \
    uint64_t b2  = rotl(s12, 43); \
    uint64_t b12 = rotl(s13, 25); \
    uint64_t b22 = rotl(s14, 39); \
    uint64_t b23 = rotl(s15, 41); \
    uint64_t b8  = rotl(s16, 45); \
    uint64_t b18 = rotl(s17, 15); \
    uint64_t b3  = rotl(s18, 21); \
    uint64_t b13 = rotl(s19, 8); \
    uint64_t b14 = rotl(s20, 18); \
    uint64_t b24 = rotl(s21, 2); \
    uint64_t b9  = rotl(s22, 61); \
    uint64_t b19 = rotl(s23, 56); \
    uint64_t b4  = rotl(s24, 14); \
    s0  = b0  ^ (~b1  & b2); \
    s1  = b1  ^ (~b2  & b3); \
    s2  = b2  ^ (~b3  & b4); \
    s3  = b3  ^ (~b4  & b0); \
    s4  = b4  ^ (~b0  & b1); \
    s5  = b5  ^ (~b6  & b7); \
    s6  = b6  ^ (~b7  & b8); \
    s7  = b7  ^ (~b8  & b9); \
    s8  = b8  ^ (~b9  & b5); \
    s9  = b9  ^ (~b5  & b6); \
    s10 = b10 ^ (~b11 & b12); \
    s11 = b11 ^ (~b12 & b13); \
    s12 = b12 ^ (~b13 & b14); \
    s13 = b13 ^ (~b14 & b10); \
    s14 = b14 ^ (~b10 & b11); \
    s15 = b15 ^ (~b16 & b17); \
    s16 = b16 ^ (~b17 & b18); \
    s17 = b17 ^ (~b18 & b19); \
    s18 = b18 ^ (~b19 & b15); \
    s19 = b19 ^ (~b15 & b16); \
    s20 = b20 ^ (~b21 & b22); \
    s21 = b21 ^ (~b22 & b23); \
    s22 = b22 ^ (~b23 & b24); \
    s23 = b23 ^ (~b24 & b20); \
    s24 = b24 ^ (~b20 & b21); \
    s0 ^= RC[r]; \
} while (0)

static void ds_hash(const uint8_t *data, size_t len, uint8_t out[32]) {
    uint64_t s0=0,s1=0,s2=0,s3=0,s4=0,s5=0,s6=0,s7=0,s8=0,s9=0,s10=0,s11=0,s12=0,s13=0,s14=0,s15=0,s16=0,s17=0,s18=0,s19=0,s20=0,s21=0,s22=0,s23=0,s24=0;
    const size_t rate = 136;
    size_t off = 0;
    uint64_t w[17];
    while (off + rate <= len) {
        memcpy(w, data + off, rate);
        s0^=w[0]; s1^=w[1]; s2^=w[2]; s3^=w[3]; s4^=w[4]; s5^=w[5]; s6^=w[6]; s7^=w[7]; s8^=w[8];
        s9^=w[9]; s10^=w[10]; s11^=w[11]; s12^=w[12]; s13^=w[13]; s14^=w[14]; s15^=w[15]; s16^=w[16];
        for (int r = 1; r < 24; r++) KECCAK_ROUND(s0,s1,s2,s3,s4,s5,s6,s7,s8,s9,s10,s11,s12,s13,s14,s15,s16,s17,s18,s19,s20,s21,s22,s23,s24,r);
        off += rate;
    }
    uint8_t buf[136];
    memset(buf, 0, sizeof(buf));
    size_t rem = len - off;
    memcpy(buf, data + off, rem);
    buf[rem] = 0x06;
    buf[135] |= 0x80;
    memcpy(w, buf, rate);
    s0^=w[0]; s1^=w[1]; s2^=w[2]; s3^=w[3]; s4^=w[4]; s5^=w[5]; s6^=w[6]; s7^=w[7]; s8^=w[8];
    s9^=w[9]; s10^=w[10]; s11^=w[11]; s12^=w[12]; s13^=w[13]; s14^=w[14]; s15^=w[15]; s16^=w[16];
    for (int r = 1; r < 24; r++) KECCAK_ROUND(s0,s1,s2,s3,s4,s5,s6,s7,s8,s9,s10,s11,s12,s13,s14,s15,s16,s17,s18,s19,s20,s21,s22,s23,s24,r);
    w[0]=s0; w[1]=s1; w[2]=s2; w[3]=s3;
    memcpy(out, w, 32);
}

static int hex_to_bytes(const char *hex, uint8_t *out, int max) {
    int n = 0;
    while (hex[0] && hex[1] && n < max) {
        unsigned v;
        if (sscanf(hex, "%2x", &v) != 1) return -1;
        out[n++] = (uint8_t)v;
        hex += 2;
    }
    return n;
}

int main(int argc, char **argv) {
    if (argc != 6 || strcmp(argv[1], "solve") != 0) {
        fprintf(stderr, "usage: %s solve <challenge_hex> <salt> <expire_at> <difficulty>\n", argv[0]);
        return 2;
    }
    uint8_t target[32];
    if (hex_to_bytes(argv[2], target, 32) != 32) {
        fprintf(stderr, "invalid challenge\n");
        return 2;
    }
    const char *salt = argv[3];
    long long expire = strtoll(argv[4], NULL, 10);
    long long difficulty = strtoll(argv[5], NULL, 10);
    if (difficulty <= 0) {
        fprintf(stderr, "invalid difficulty\n");
        return 2;
    }
    uint8_t input[256];
    uint8_t h[32];
    int plen = snprintf((char *)input, sizeof(input), "%s_%lld_", salt, expire);
    if (plen <= 0 || plen >= 200) {
        fprintf(stderr, "prefix too long\n");
        return 2;
    }
    for (long long n = 0; n < difficulty; n++) {
        int m = snprintf((char *)input + plen, sizeof(input) - (size_t)plen, "%lld", n);
        ds_hash(input, (size_t)(plen + m), h);
        if (memcmp(h, target, 32) == 0) {
            printf("%lld\n", n);
            return 0;
        }
    }
    printf("-1\n");
    return 1;
}
"""


# ---------------------------------------------------------------------------
# Pure-Python DeepSeekHashV1 (fallback when no C compiler is available)
# ---------------------------------------------------------------------------

_M64 = 0xFFFFFFFFFFFFFFFF
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_PI = [0, 6, 12, 18, 24, 3, 9, 10, 16, 22, 1, 7, 13, 19, 20, 4, 5, 11, 17, 23, 2, 8, 14, 15, 21]
_RHO = [0, 44, 43, 21, 14, 28, 20, 3, 45, 61, 1, 6, 25, 8, 18, 27, 36, 10, 15, 56, 62, 55, 39, 41, 2]


def _rotl(v: int, k: int) -> int:
    return ((v << k) | (v >> (64 - k))) & _M64


def _keccak_f23(s: list[int]) -> None:
    a = list(s)
    for r in range(1, 24):
        c = [(a[i] ^ a[i + 5] ^ a[i + 10] ^ a[i + 15] ^ a[i + 20]) & _M64 for i in range(5)]
        d = [(c[(i + 4) % 5] ^ _rotl(c[(i + 1) % 5], 1)) & _M64 for i in range(5)]
        for i in range(5):
            for j in range(0, 25, 5):
                a[i + j] = (a[i + j] ^ d[i]) & _M64
        b = [_rotl(a[_PI[i]], _RHO[i]) for i in range(25)]
        for j in range(5):
            for i in range(5):
                a[j * 5 + i] = (b[j * 5 + i] ^ ((~b[j * 5 + (i + 1) % 5] & _M64) & b[j * 5 + (i + 2) % 5])) & _M64
        a[0] = (a[0] ^ _RC[r]) & _M64
    for i in range(25):
        s[i] = a[i]


def deepseek_hash_v1(data: bytes) -> bytes:
    """SHA3-256 with Keccak-f[1600] rounds 1..23 (round 0 skipped)."""
    rate = 136
    s = [0] * 25
    off = 0
    while off + rate <= len(data):
        for i in range(rate // 8):
            s[i] ^= struct.unpack_from("<Q", data, off + i * 8)[0]
        _keccak_f23(s)
        off += rate
    buf = bytearray(rate)
    rem = len(data) - off
    buf[:rem] = data[off:]
    buf[rem] = 0x06
    buf[rate - 1] |= 0x80
    for i in range(rate // 8):
        s[i] ^= struct.unpack_from("<Q", buf, i * 8)[0]
    _keccak_f23(s)
    out = bytearray(32)
    for i in range(4):
        struct.pack_into("<Q", out, i * 8, s[i])
    return bytes(out)


# ---------------------------------------------------------------------------
# PoW solver wrapper
# ---------------------------------------------------------------------------

_solver_lock = threading.Lock()
_native_solver_checked = False
_native_solver_path: Optional[Path] = None


def _solver_cache_path() -> Path:
    """Use a per-user, OS/CPU/source-specific cache, not the installed code directory."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    arch = "".join(c for c in platform.machine().lower() if c.isalnum() or c in "_-") or "unknown"
    digest = hashlib.sha256(C_SOURCE.encode("utf-8")).hexdigest()[:16]
    suffix = ".exe" if os.name == "nt" else ""
    return root / "DeepSeekWebClient" / f"ds_pow_solver-{sys.platform}-{arch}-{digest}{suffix}"


def _valid_native_solver(path: Path) -> bool:
    """Reject stale, corrupt or wrong-platform binaries before using them."""
    try:
        result = subprocess.run(
            [str(path), "solve", KNOWN_POW["challenge"], KNOWN_POW["salt"],
             str(KNOWN_POW["expire_at"]), str(KNOWN_POW["difficulty"])],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == str(KNOWN_POW["answer"])
    except (OSError, subprocess.SubprocessError):
        return False


def _ensure_native_solver() -> Optional[Path]:
    """Reuse a verified solver or compile portable C in the user's cache."""
    global _native_solver_checked, _native_solver_path
    with _solver_lock:
        if _native_solver_checked:
            return _native_solver_path
        _native_solver_checked = True
        if SOLVER_EXE.is_file() and _valid_native_solver(SOLVER_EXE):
            _native_solver_path = SOLVER_EXE
            return _native_solver_path
        cache = _solver_cache_path()
        if cache.is_file() and _valid_native_solver(cache):
            _native_solver_path = cache
            return _native_solver_path
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            source = cache.with_suffix(".c")
            if not source.exists() or source.read_text(encoding="utf-8") != C_SOURCE:
                source.write_text(C_SOURCE, encoding="utf-8")
        except OSError:
            return None
        # -march=native would bake the build machine's CPU features into a
        # binary reused on subsequent machines, so compile generic C instead.
        for compiler in ("gcc", "clang", "cc"):
            exe = shutil.which(compiler)
            if not exe:
                continue
            for extra in (["-O3"], []):
                tmp = cache.with_name(cache.name + f".{os.getpid()}.tmp")
                cmd = [exe, *extra, "-o", str(tmp), str(source)]
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                except (OSError, subprocess.SubprocessError):
                    continue
                if proc.returncode == 0 and tmp.exists():
                    try:
                        os.replace(tmp, cache)
                    except OSError:
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
                        continue
                    if _valid_native_solver(cache):
                        _native_solver_path = cache
                        return _native_solver_path
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return None


def solve_pow_python(challenge: dict[str, Any], *, verbose: bool = False) -> int:
    """Slow pure-Python fallback. Only used when no C compiler/exe is available."""
    salt = str(challenge["salt"])
    expire_at = int(challenge["expire_at"])
    difficulty = int(challenge["difficulty"])
    target = bytes.fromhex(str(challenge["challenge"]))
    prefix = f"{salt}_{expire_at}_".encode()
    started = time.time()
    for answer in range(difficulty):
        if deepseek_hash_v1(prefix + str(answer).encode()) == target:
            return answer
        if verbose and answer and answer % 20000 == 0:
            elapsed = time.time() - started
            rate = answer / elapsed if elapsed else 0
            print(
                f"[pow] python fallback: {answer}/{difficulty} "
                f"({rate:.0f} h/s, {elapsed:.0f}s elapsed)",
                file=sys.stderr,
                flush=True,
            )
    raise ProofOfWorkError(f"no nonce found in [0, {difficulty})")


def solve_pow(challenge: dict[str, Any], *, verbose: bool = False) -> int:
    """Solve a DeepSeekHashV1 challenge and return the integer answer."""
    algorithm = challenge.get("algorithm", "DeepSeekHashV1")
    if algorithm != "DeepSeekHashV1":
        raise ProofOfWorkError(f"unsupported PoW algorithm: {algorithm!r}")

    challenge_hex = str(challenge["challenge"])
    salt = str(challenge["salt"])
    expire_at = int(challenge["expire_at"])
    difficulty = int(challenge["difficulty"])
    if not (len(challenge_hex) == 64 and all(c in "0123456789abcdefABCDEF" for c in challenge_hex)):
        raise ProofOfWorkError("challenge is not a 32-byte hex string")
    if difficulty <= 0:
        raise ProofOfWorkError("difficulty must be positive")

    solver = _ensure_native_solver()
    if solver is not None:
        try:
            proc = subprocess.run(
                [str(solver), "solve", challenge_hex, salt, str(expire_at), str(difficulty)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            out = proc.stdout.strip()
            if proc.returncode == 0 and out:
                answer = int(out)
                # A working executable can still yield an incorrect nonce for
                # a particular challenge. Verify the actual hash before using
                # its answer in an authenticated request.
                if (0 <= answer < difficulty and
                        deepseek_hash_v1(f"{salt}_{expire_at}_{answer}".encode()) ==
                        bytes.fromhex(challenge_hex)):
                    return answer
            if verbose:
                print("[pow] native solver returned an invalid result; using Python", file=sys.stderr)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if verbose:
                print(f"[pow] native solver unavailable ({type(exc).__name__}); using Python", file=sys.stderr)

    if verbose:
        print("[pow] using slow pure-Python solver", file=sys.stderr)
    return solve_pow_python(challenge, verbose=verbose)


def build_pow_header(challenge: dict[str, Any], answer: int) -> str:
    payload = {
        "algorithm": challenge.get("algorithm", "DeepSeekHashV1"),
        "challenge": challenge["challenge"],
        "salt": challenge["salt"],
        "answer": answer,
        "signature": challenge["signature"],
        "target_path": challenge.get("target_path", DEFAULT_TARGET_PATH),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# SSE parsing and response reconstruction
# ---------------------------------------------------------------------------


@dataclass
class StreamChunk:
    event: str
    text: str = ""
    thinking: str = ""
    title: Optional[str] = None
    status: Optional[str] = None
    done: bool = False
    raw: Optional[str] = None
    attempt: Optional[int] = None
    file_id: Optional[str] = None
    filename: Optional[str] = None


@dataclass
class ChatResult:
    session_id: str
    text: str
    thinking: str = ""
    title: Optional[str] = None
    status: Optional[str] = None
    request_message_id: Optional[int] = None
    response_message_id: Optional[int] = None
    events: int = 0
    continuations: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    file_ids: list[str] = field(default_factory=list)


def iter_sse(lines: Iterable[Any]) -> Iterator[tuple[str, str]]:
    """Yield (event, data) pairs from an SSE byte/str line stream."""
    event = "message"
    data: list[str] = []
    for raw in lines:
        if raw is None:
            continue
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        line = line.rstrip("\r")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event = "message"
            data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        # Other SSE fields (id:, retry:) are unused by this API.
    if data:
        yield event, "\n".join(data)


class CompletionAccumulator:
    """Apply DeepSeek's JSON-patch style SSE stream to a response object."""

    def __init__(self) -> None:
        self.response: dict[str, Any] = {}
        self.ready: dict[str, Any] = {}
        self.title: Optional[str] = None
        self.closed = False
        self.status: Optional[str] = None
        self.text = ""
        self.thinking = ""
        # Keep the already-delivered text separate from the latest SSE snapshot.
        # /chat/continue may temporarily send an empty/older snapshot before
        # replaying the full accumulated message. Never stream that prefix twice.
        self._emitted_text = ""
        self._emitted_thinking = ""
        self.events = 0
        self.continuations = 0
        self._last_path: Optional[str] = None
        self._last_op: Optional[str] = None

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _index(container: list[Any], segment: str) -> Optional[int]:
        if segment == "-1":
            return len(container) - 1 if container else 0
        try:
            idx = int(segment)
        except (TypeError, ValueError):
            return None
        if idx < 0:
            idx += len(container)
        return idx

    def _get(self, container: Any, segment: str) -> Any:
        if isinstance(container, list):
            idx = self._index(container, segment)
            if idx is not None and 0 <= idx < len(container):
                return container[idx]
            return None
        if isinstance(container, dict):
            return container.get(segment)
        return None

    def _set(self, container: Any, segment: str, value: Any) -> None:
        if isinstance(container, list):
            idx = self._index(container, segment)
            if idx is None:
                return
            if idx < 0:
                idx = 0
            while len(container) <= idx:
                container.append(None)
            container[idx] = value
        elif isinstance(container, dict):
            container[segment] = value

    @staticmethod
    def _is_index_segment(segment: str) -> bool:
        return segment == "-1" or segment.isdigit()

    def _apply(self, path: str, op: str, value: Any) -> None:
        parts = [p for p in str(path).split("/") if p]
        if parts and parts[0] == "response":
            parts = parts[1:]

        if op == "BATCH":
            if isinstance(value, list):
                for sub in value:
                    if isinstance(sub, dict) and "p" in sub:
                        self._apply(sub["p"], sub.get("o", "SET"), sub.get("v"))
            return

        if not parts:
            if op in ("SET", "REPLACE") and isinstance(value, dict):
                self.response = value
            return

        node: Any = self.response
        for i, segment in enumerate(parts[:-1]):
            child = self._get(node, segment)
            if not isinstance(child, (dict, list)):
                child = [] if self._is_index_segment(parts[i + 1]) else {}
                self._set(node, segment, child)
            node = child

        key = parts[-1]
        if op == "APPEND":
            target = self._get(node, key)
            if isinstance(target, list):
                if isinstance(value, list):
                    target.extend(value)
                else:
                    target.append(value)
            elif isinstance(target, str):
                self._set(node, key, target + (value if isinstance(value, str) else str(value)))
            elif isinstance(target, dict) and isinstance(value, dict):
                target.update(value)
            else:
                if target is None and isinstance(value, list):
                    self._set(node, key, list(value))
                elif target is None and isinstance(value, str):
                    self._set(node, key, value)
                else:
                    self._set(node, key, value)
        elif op in ("SET", "REPLACE"):
            self._set(node, key, value)
        # Unknown ops are ignored for forward compatibility.

    # -- full-text views -------------------------------------------------

    def _fragment_text(self, types: tuple[str, ...]) -> str:
        fragments = self.response.get("fragments") if isinstance(self.response, dict) else None
        if not isinstance(fragments, list):
            return ""
        out: list[str] = []
        for frag in fragments:
            if not isinstance(frag, dict):
                continue
            if str(frag.get("type", "")).upper() in types:
                content = frag.get("content")
                if isinstance(content, str):
                    out.append(content)
        return "".join(out)

    def _refresh(self) -> tuple[str, str]:
        self.text = self._fragment_text(("RESPONSE",))
        self.thinking = self._fragment_text(("THINK", "THINKING", "REASONING"))
        status = self.response.get("status") if isinstance(self.response, dict) else None
        quasi = self.response.get("quasi_status") if isinstance(self.response, dict) else None
        # The final status field is authoritative. A stale quasi_status can
        # otherwise keep a finished continuation stuck in INCOMPLETE.
        if status in ("FINISHED", "INCOMPLETE"):
            self.status = status
        elif isinstance(quasi, str) and quasi:
            self.status = quasi
        elif isinstance(status, str):
            self.status = status
        delta_text = ""
        if self.text.startswith(self._emitted_text):
            delta_text = self.text[len(self._emitted_text):]
            self._emitted_text = self.text
        elif not self._emitted_text.startswith(self.text):
            # A genuine server rewrite cannot be represented as an append-only
            # delta. Keep the emitted prefix until a cumulative snapshot catches up.
            pass

        delta_think = ""
        if self.thinking.startswith(self._emitted_thinking):
            delta_think = self.thinking[len(self._emitted_thinking):]
            self._emitted_thinking = self.thinking
        return delta_text, delta_think

    # -- main entry ------------------------------------------------------

    def feed(self, event: str, data: str) -> tuple[str, str]:
        """Consume one SSE event. Returns (new_response_text, new_thinking_text)."""
        self.events += 1

        if event == "ready":
            try:
                self.ready = json.loads(data) if data else {}
            except json.JSONDecodeError:
                self.ready = {}
            rid = self.ready.get("response_message_id")
            if isinstance(rid, int) and isinstance(self.response, dict):
                self.response.setdefault("message_id", rid)
            return ("", "")

        if event == "title":
            try:
                obj = json.loads(data) if data else {}
                if isinstance(obj, dict) and isinstance(obj.get("content"), str):
                    self.title = obj["content"]
            except json.JSONDecodeError:
                pass
            return ("", "")

        if event == "close":
            self.closed = True
            return ("", "")

        if event == "update_session":
            return ("", "")

        if event == "error":
            raise DeepSeekAPIError(f"stream error: {data}")

        if not data or data.strip() == "[DONE]":
            if data.strip() == "[DONE]":
                self.closed = True
            return self._refresh()

        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            if event == "message":
                raise DeepSeekAPIError("DeepSeek SSE message contains malformed JSON") from exc
            return ("", "")

        if not isinstance(obj, dict):
            return ("", "")

        code = obj.get("code")
        biz_code = obj.get("biz_code")
        if code not in (None, 0) or biz_code not in (None, 0):
            raise DeepSeekAPIError(
                str(obj.get("msg") or obj.get("biz_msg") or "stream API error"),
                code=code,
                biz_code=biz_code,
                payload=obj,
            )

        if "v" in obj:
            value = obj["v"]
            if "p" in obj:
                path = str(obj["p"])
                op = str(obj.get("o") or ("APPEND" if path.endswith("/content") and isinstance(value, str) else "SET"))
                self._apply(path, op, value)
                # When DeepSeek adds a new THINK/RESPONSE fragment, following
                # SSE frames omit `p` and contain only the next content delta.
                # They target the newest fragment's content, not the list itself.
                if op == "APPEND" and path.strip("/").endswith("response/fragments") and isinstance(value, list):
                    self._last_path, self._last_op = "response/fragments/-1/content", "APPEND"
                else:
                    self._last_path, self._last_op = path, op
            elif isinstance(value, dict) and "response" in value:
                if isinstance(value["response"], dict):
                    self.response = value["response"]
            elif self._last_path is not None:
                self._apply(self._last_path, self._last_op or "APPEND", value)
        elif "p" in obj:
            path = str(obj["p"])
            op = str(obj.get("o") or ("APPEND" if path.endswith("/content") and isinstance(obj.get("v"), str) else "SET"))
            self._apply(path, op, obj.get("v"))
            self._last_path, self._last_op = path, op

        return self._refresh()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _normalise_token(token: str) -> str:
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _read_token() -> str:
    token = _normalise_token(os.environ.get("DEEPSEEK_TOKEN", ""))
    if token:
        return token
    token_file = os.environ.get("DEEPSEEK_TOKEN_FILE", "").strip()
    if token_file:
        try:
            return _normalise_token(Path(token_file).read_text(encoding="utf-8"))
        except OSError:
            pass
    from credential_store import load_credentials

    cached_token = _normalise_token(load_credentials().get("token", ""))
    if cached_token:
        return cached_token
    # Optional, offline recovery of a previously authenticated DeepSeek origin.
    # This does not launch a browser and is not required on browserless hosts.
    try:
        from import_existing_session import try_import

        if try_import():
            return _normalise_token(load_credentials().get("token", ""))
    except (ImportError, OSError, ValueError):
        pass
    return ""


def _parse_cookie_header(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for piece in raw.split(";"):
        if "=" in piece:
            name, value = piece.split("=", 1)
            name = name.strip()
            if name:
                cookies[name] = value.strip()
    return cookies


def _default_timezone_offset() -> int:
    try:
        return int(datetime.now().astimezone().utcoffset().total_seconds())  # type: ignore[union-attr]
    except (TypeError, ValueError, AttributeError):
        return -10800


class DeepSeekClient:
    """Authenticated client for the DeepSeek chat web API.

    Example::

        client = DeepSeekClient()          # token/device id from the environment
        result = client.chat("Hello!")
        print(result.text)
    """

    def __init__(
        self,
        token: Optional[str] = None,
        device_id: Optional[str] = None,
        *,
        base_url: str = BASE_URL,
        locale: Optional[str] = None,
        timezone_offset: Optional[int] = None,
        cookies: Optional[str] = None,
        hif_leim: Optional[str] = None,
        timeout: float = 30.0,
        stream_timeout: float = 180.0,
        verbose: bool = False,
        allow_unauthed: bool = False,
    ) -> None:
        self.token = _normalise_token(token) if token is not None else _read_token()

        from credential_store import load_credentials

        saved_auth = load_credentials()
        # An explicit token or an environment-provisioned account may differ
        # from the cached account. Never mix its browser proof, device ID, or
        # cookies with a different bearer token.
        if _normalise_token(saved_auth.get("token", "")) != self.token:
            saved_auth = {}
        self._persist_auth_on_success = bool(
            token is not None
            or os.environ.get("DEEPSEEK_TOKEN", "").strip()
            or os.environ.get("DEEPSEEK_TOKEN_FILE", "").strip()
        )

        self.device_id = (
            device_id
            or os.environ.get("DEEPSEEK_DEVICE_ID", "").strip()
            or saved_auth.get("device_id", "")
            or DEFAULT_DEVICE_ID
        )
        self.base_url = base_url.rstrip("/")
        self.locale = locale or os.environ.get("DEEPSEEK_LOCALE", "").strip() or DEFAULT_LOCALE
        if timezone_offset is None:
            env_tz = os.environ.get("DEEPSEEK_TIMEZONE_OFFSET", "").strip()
            if env_tz:
                try:
                    timezone_offset = int(env_tz)
                except ValueError:
                    timezone_offset = None
            if timezone_offset is None:
                timezone_offset = _default_timezone_offset()
        self.timezone_offset = timezone_offset
        self.timeout = timeout
        self.stream_timeout = stream_timeout
        self.verbose = verbose

        self.last_accumulator: Optional[CompletionAccumulator] = None
        self.last_file_ids: list[str] = []

        from request_guard import GuardedSession
        self.session = GuardedSession(origin=self.base_url)
        raw_cookies = cookies if cookies is not None else os.environ.get("DEEPSEEK_COOKIES", "")
        self.session.cookies.update(saved_auth.get("cookies", {}))
        if raw_cookies:
            self.session.cookies.update(_parse_cookie_header(raw_cookies))
        self.hif_leim = (
            hif_leim
            if hif_leim is not None
            else os.environ.get("DEEPSEEK_HIF_LEIM", "").strip()
            or saved_auth.get("hif_leim", "")
        )
        if not self.token and not allow_unauthed:
            try:
                if self._login_with_saved_credentials():
                    return
            except (AuthenticationError, requests.RequestException) as exc:
                raise AuthenticationError("Saved DeepSeek login failed; authenticate again with --login") from exc
            raise AuthenticationError(
                "No DeepSeek session token or saved account login. "
                "Use --login to sign in, or supply DEEPSEEK_TOKEN once."
            )

    # -- headers ---------------------------------------------------------

    def _referer(self, session_id: Optional[str] = None) -> str:
        if session_id:
            return f"{self.base_url}/a/chat/s/{session_id}"
        return f"{self.base_url}/"

    def _headers(
        self,
        *,
        referer: Optional[str] = None,
        extra: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "accept": "*/*",
            "authorization": f"Bearer {self.token}",
            "content-type": "application/json",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "x-client-bundle-id": CLIENT_BUNDLE_ID,
            "x-client-locale": self.locale,
            "x-client-platform": "web",
            "x-client-timezone-offset": str(self.timezone_offset),
            "x-client-version": CLIENT_VERSION,
            "x-device-id": self.device_id,
            "x-device-model": "",
            "Referer": referer or self._referer(),
        }
        if self.hif_leim:
            headers["x-hif-leim"] = self.hif_leim
        if extra:
            headers.update(extra)
        return headers

    # -- errors / auth refresh ------------------------------------------

    def login(self, *, email: str = "", mobile: str = "", area_code: str = "", password: str) -> None:
        """Login once and save account credentials securely for future renewals."""
        credentials = {
            "email": email.strip(), "mobile": mobile.strip(),
            "area_code": area_code.strip(), "password": password,
        }
        with _AUTH_REFRESH_LOCK:
            self._login_with_credentials(credentials, save_login=True)

    def _login_with_saved_credentials(self) -> bool:
        from credential_store import load_login_credentials

        credentials = load_login_credentials()
        return self._login_with_credentials(credentials) if credentials else False

    def _login_with_credentials(self, credentials: dict[str, str], *, save_login: bool = False) -> bool:
        """Exchange account credentials for a fresh DeepSeek Web session token."""
        email = credentials.get("email", "").strip()
        mobile = credentials.get("mobile", "").strip()
        password = credentials.get("password", "")
        if not password or not (email or mobile):
            raise AuthenticationError("DeepSeek login requires email/mobile and password")
        headers = self._headers(referer=f"{self.base_url}/sign_in", extra={"Origin": self.base_url})
        headers.pop("authorization", None)
        resp = self.session.post(
            f"{self.base_url}{LOGIN_PATH}",
            json={
                "email": email, "mobile": mobile, "password": password,
                "area_code": credentials.get("area_code", ""),
                "device_id": self.device_id, "os": "web",
            },
            headers=headers,
            timeout=self.timeout,
        )
        try:
            if resp.status_code != 200:
                raise AuthenticationError(f"DeepSeek HTTP login failed (HTTP {resp.status_code})")
            try:
                result = resp.json()
            except ValueError as exc:
                raise AuthenticationError("DeepSeek HTTP login returned invalid JSON") from exc
            if not isinstance(result, dict):
                raise AuthenticationError("Unexpected DeepSeek HTTP login response")
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            if result.get("code") not in (None, 0) or data.get("biz_code") not in (None, 0):
                raise AuthenticationError("DeepSeek HTTP login was rejected by the server")
            auth_data = data.get("biz_data") if isinstance(data.get("biz_data"), dict) else {}
            user = auth_data.get("user") if isinstance(auth_data.get("user"), dict) else {}
            token = _normalise_token(user.get("token", ""))
            if not token:
                raise AuthenticationError("DeepSeek HTTP login returned no token")
            from credential_store import save_credentials, save_login_credentials

            if save_login and not save_login_credentials(**credentials):
                raise AuthenticationError("Could not save DeepSeek login in the operating-system keyring")
            if not save_credentials(token, device_id=self.device_id,
                                    hif_leim=self.hif_leim, cookies=self.session.cookies.get_dict()):
                raise AuthenticationError("Could not save DeepSeek token in the operating-system keyring")
            self.token = token
            self._persist_auth_on_success = False
            return True
        finally:
            resp.close()

    def _reload_token(self) -> bool:
        with _AUTH_REFRESH_LOCK:
            from credential_store import load_credentials

            saved = load_credentials()
            cached_token = _normalise_token(saved.get("token", ""))
            if cached_token and cached_token != self.token:
                self.token = cached_token
                self.hif_leim = saved.get("hif_leim", "")
                self.device_id = saved.get("device_id", "") or self.device_id
                self.session.cookies.clear()
                self.session.cookies.update(saved.get("cookies", {}))
                return True
            try:
                if self._login_with_saved_credentials():
                    return True
            except (AuthenticationError, requests.RequestException):
                pass  # One attempt; the calling request reports its auth failure.
            # Comet may have renewed its own web session while this gateway
            # still holds an expired token. Reimport it without a browser.
            try:
                from import_existing_session import try_import

                if try_import():
                    refreshed = load_credentials()
                    refreshed_token = _normalise_token(refreshed.get("token", ""))
                    # Comet can renew its proof, device ID or session cookies
                    # while keeping the very same bearer token. A rejected
                    # request must still retry with that refreshed session.
                    changed = (
                        refreshed_token != self.token
                        or refreshed.get("hif_leim", "") != self.hif_leim
                        or (refreshed.get("device_id", "") or self.device_id) != self.device_id
                        or refreshed.get("cookies", {}) != self.session.cookies.get_dict()
                    )
                    if refreshed_token and changed:
                        self.token = refreshed_token
                        self.hif_leim = refreshed.get("hif_leim", "")
                        self.device_id = refreshed.get("device_id", "") or self.device_id
                        self.session.cookies.clear()
                        self.session.cookies.update(refreshed.get("cookies", {}))
                        return True
            except (ImportError, OSError, RuntimeError, ValueError):
                pass
            replacement = _read_token()
            if replacement and replacement != self.token:
                self.token = replacement
                self.hif_leim = ""
                self.session.cookies.clear()
                return True
            return False

    def _persist_valid_credentials(self) -> None:
        if not self._persist_auth_on_success:
            return
        from credential_store import save_credentials

        if save_credentials(
            self.token,
            device_id=self.device_id,
            hif_leim=self.hif_leim,
            cookies=self.session.cookies.get_dict(),
        ):
            self._persist_auth_on_success = False

    @staticmethod
    def _is_auth_like(code: Any, message: str) -> bool:
        if str(code) in {"40001", "40003"}:
            return True
        text = f"{code} {message}".lower()
        return any(
            word in text
            for word in ("unauthor", "unauthorised", "unauthorized", "token", "auth", "login", "expire", "401", "403")
        )

    @staticmethod
    def _error_payload(resp: "requests.Response") -> dict[str, Any]:
        try:
            data = resp.json()
            return data if isinstance(data, dict) else {"raw": data}
        except ValueError:
            return {"raw": (resp.text or "")[:2000]}

    def _raise_http(self, resp: "requests.Response") -> None:
        payload = self._error_payload(resp)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        message = (
            payload.get("msg")
            or payload.get("message")
            or data.get("biz_msg")
            or (payload.get("raw") if isinstance(payload.get("raw"), str) else "")
            or f"HTTP {resp.status_code}"
        )
        if resp.status_code in (401, 403):
            raise AuthenticationError(
                f"DeepSeek rejected the credentials (HTTP {resp.status_code}): {message}. "
                "Reauthenticate with --login, or supply a valid DEEPSEEK_TOKEN."
            )
        raise DeepSeekAPIError(
            str(message),
            status=resp.status_code,
            code=payload.get("code"),
            biz_code=data.get("biz_code"),
            trace_id=resp.headers.get("x-ds-trace-id"),
            payload=payload,
        )

    # -- generic requests ------------------------------------------------

    def _unwrap(self, resp: "requests.Response") -> Any:
        content_type = resp.headers.get("content-type", "").lower()
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if "text/html" in content_type or not isinstance(payload, dict):
            # An HTTP 200 can carry an intermediary's HTML challenge page or
            # otherwise malformed JSON, not a successful API response. Stop
            # before persisting credentials or starting another request.
            raise DeepSeekAPIError(
                "DeepSeek returned an unexpected non-API response; check the official website",
                status=resp.status_code,
            )
        code = payload.get("code")
        if code not in (None, 0):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            raise DeepSeekAPIError(
                str(payload.get("msg") or data.get("biz_msg") or "API error"),
                status=resp.status_code,
                code=code,
                biz_code=data.get("biz_code"),
                trace_id=resp.headers.get("x-ds-trace-id"),
                payload=payload,
            )
        block = payload.get("data")
        if isinstance(block, dict):
            code = block.get("biz_code")
            if code not in (None, 0):
                raise DeepSeekAPIError(
                    str(block.get("biz_msg") or payload.get("msg") or "API error"),
                    status=resp.status_code,
                    code=payload.get("code"),
                    biz_code=code,
                    trace_id=resp.headers.get("x-ds-trace-id"),
                    payload=payload,
                )
            return block.get("biz_data")
        return block

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        referer: Optional[str] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        for attempt in range(2):
            resp = self.session.post(
                url,
                json=payload,
                headers=self._headers(referer=referer),
                timeout=self.timeout,
            )
            try:
                if resp.status_code in (401, 403) and attempt == 0 and self._reload_token():
                    continue
                if resp.status_code >= 400:
                    self._raise_http(resp)
                data = self._unwrap(resp)
                self._persist_valid_credentials()
                return data
            except DeepSeekAPIError as exc:
                if attempt == 0 and self._is_auth_like(exc.code, str(exc)) and self._reload_token():
                    continue
                raise
            finally:
                resp.close()
        raise AuthenticationError("authentication failed after token reload")

    # -- public API steps ------------------------------------------------

    def create_pow_challenge(self, target_path: str = DEFAULT_TARGET_PATH, *, session_id: Optional[str] = None) -> dict[str, Any]:
        """GET-ish POST that returns the challenge dict (algorithm, salt, ...)."""
        body = {"target_path": target_path}
        data = self._post_json(POW_PATH, body, referer=self._referer(session_id))
        if not isinstance(data, dict) or "challenge" not in data:
            raise DeepSeekAPIError(f"unexpected PoW response: {data!r}", payload=data)
        challenge = data["challenge"]
        if not isinstance(challenge, dict):
            raise DeepSeekAPIError("PoW response did not contain a challenge object", payload=data)
        return challenge

    def create_chat_session(self) -> dict[str, Any]:
        """Create a new chat session and return the chat_session object."""
        data = self._post_json(SESSION_CREATE_PATH, {}, referer=self._referer())
        if not isinstance(data, dict) or not isinstance(data.get("chat_session"), dict):
            raise DeepSeekAPIError(f"unexpected session response: {data!r}", payload=data)
        return data["chat_session"]

    def upload_file(
        self,
        file: Any,
        *,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        model_type: str = "default",
        thinking_enabled: bool = False,
        wait_for_processing: bool = True,
        parse_timeout: float = 300.0,
        poll_interval: float = 3.0,
    ) -> dict[str, Any]:
        """Upload a local file or (filename, bytes) with DeepSeek Web's PoW.

        Only explicit local paths or caller-supplied bytes are accepted. This
        never downloads a URL and does not use or launch a browser.
        """
        source = file
        if isinstance(source, tuple):
            if len(source) != 2 or not isinstance(source[0], str):
                raise TypeError("file tuple must be (filename, bytes)")
            if filename is not None:
                raise ValueError("filename must not be repeated")
            filename, source = source
        if isinstance(source, (bytes, bytearray, memoryview)):
            data = bytes(source)
            if not filename:
                raise ValueError("filename is required for bytes uploads")
            size = len(data)
            file_handle = BytesIO(data)
        elif isinstance(source, (str, os.PathLike)):
            path = Path(source)
            if not path.is_file():
                raise FileNotFoundError(f"attachment not found: {path}")
            size = path.stat().st_size
            filename = filename or path.name
            file_handle = path.open("rb")
        else:
            raise TypeError("file must be a local path, bytes or (filename, bytes)")
        if not size:
            file_handle.close()
            raise ValueError("cannot upload an empty file")

        upload_name = Path(filename).name
        if not upload_name:
            file_handle.close()
            raise ValueError("filename must not be empty")
        content_type = mime_type or mimetypes.guess_type(upload_name)[0] or "application/octet-stream"

        try:
            challenge = self.create_pow_challenge(FILE_UPLOAD_PATH)
            header = build_pow_header(challenge, solve_pow(challenge, verbose=self.verbose))
            headers = self._headers(extra={
                "x-ds-pow-response": header,
                "x-file-size": str(size),
                "x-model-type": model_type,
                "x-thinking-enabled": "1" if thinking_enabled else "0",
                "Origin": self.base_url,
            })
            # requests must generate the multipart boundary itself.
            headers.pop("content-type", None)
            with file_handle:
                for attempt in range(2):
                    file_handle.seek(0)
                    resp = self.session.post(
                        f"{self.base_url}{FILE_UPLOAD_PATH}",
                        files={"file": (upload_name, file_handle, content_type)},
                        headers=headers,
                        timeout=(self.timeout, self.stream_timeout),
                    )
                    try:
                        if resp.status_code in (401, 403) and attempt == 0 and self._reload_token():
                            headers["authorization"] = f"Bearer {self.token}"
                            continue
                        if resp.status_code >= 400:
                            self._raise_http(resp)
                        uploaded = self._unwrap(resp)
                        break
                    finally:
                        resp.close()
                else:
                    raise AuthenticationError("file upload failed after token reload")
        finally:
            # Also close the handle when PoW generation fails.
            file_handle.close()

        file_id = uploaded.get("id") if isinstance(uploaded, dict) else None
        if not file_id:
            raise DeepSeekAPIError(f"DeepSeek upload did not return a file id: {uploaded!r}")
        self._persist_valid_credentials()
        result = {
            "file_id": str(file_id),
            "filename": upload_name,
            "size": size,
            "mime_type": content_type,
            "status": "UPLOADED",
        }
        if wait_for_processing:
            file_info = self.wait_for_file_ready(
                str(file_id), timeout=parse_timeout, poll_interval=poll_interval
            )
            result["status"] = "SUCCESS"
            result["file_info"] = file_info
        return result

    def wait_for_file_ready(
        self,
        file_id: str,
        *,
        timeout: float = 300.0,
        poll_interval: float = 3.0,
    ) -> dict[str, Any]:
        """Poll file parsing until SUCCESS; fail explicitly on rejected files."""
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        deadline = time.monotonic() + timeout
        while True:
            for attempt in range(2):
                resp = self.session.get(
                    f"{self.base_url}{FILE_FETCH_PATH}",
                    params={"file_ids": str(file_id)},
                    headers=self._headers(extra={"accept": "application/json"}),
                    timeout=self.timeout,
                )
                try:
                    if resp.status_code in (401, 403) and attempt == 0 and self._reload_token():
                        continue
                    if resp.status_code >= 400:
                        self._raise_http(resp)
                    data = self._unwrap(resp)
                    break
                finally:
                    resp.close()
            files = data.get("files") if isinstance(data, dict) else None
            info = next(
                (f for f in files if isinstance(f, dict) and str(f.get("id")) == str(file_id)),
                None,
            ) if isinstance(files, list) else None
            if info is None and isinstance(files, list) and len(files) == 1:
                info = files[0]
            if not isinstance(info, dict):
                raise DeepSeekAPIError(f"file status response missing {file_id}: {data!r}")
            status = str(info.get("status") or "").upper()
            if status == "SUCCESS":
                return info
            if status in FILE_FAILURE_STATUSES:
                raise DeepSeekAPIError(
                    f"DeepSeek rejected attachment {file_id}: {status} ({info.get('error_code')})",
                    payload=info,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeepSeekAPIError(f"file parsing timed out: {file_id}, last status={status}")
            time.sleep(min(poll_interval, remaining))

    def upload_files(self, files: Iterable[Any], **kwargs: Any) -> list[dict[str, Any]]:
        """Upload multiple local attachments, preserving their order."""
        return [self.upload_file(file, **kwargs) for file in files]

    def create_completion_stream(
        self,
        *,
        chat_session_id: str,
        prompt: str,
        parent_message_id: Optional[int] = None,
        model_type: str = "default",
        thinking_enabled: bool = False,
        search_enabled: bool = True,
        ref_file_ids: Optional[list[Any]] = None,
        action: Any = None,
        preempt: bool = False,
    ) -> tuple["requests.Response", CompletionAccumulator]:
        """Fetch a fresh PoW challenge and open the SSE completion request."""
        challenge = self.create_pow_challenge(DEFAULT_TARGET_PATH, session_id=chat_session_id)
        answer = solve_pow(challenge, verbose=self.verbose)
        pow_header = build_pow_header(challenge, answer)

        body = {
            "chat_session_id": chat_session_id,
            "parent_message_id": parent_message_id,
            "model_type": model_type,
            "prompt": prompt,
            "ref_file_ids": ref_file_ids or [],
            "thinking_enabled": bool(thinking_enabled),
            "search_enabled": bool(search_enabled),
            "action": action,
            "preempt": bool(preempt),
        }
        url = f"{self.base_url}{COMPLETION_PATH}"
        extra = {"x-ds-pow-response": pow_header}
        referer = self._referer(chat_session_id)

        last_error: Optional[Exception] = None
        for attempt in range(2):
            resp = self.session.post(
                url,
                json=body,
                headers=self._headers(referer=referer, extra=extra),
                stream=True,
                timeout=(self.timeout, self.stream_timeout),
            )
            if resp.status_code in (401, 403) and attempt == 0 and self._reload_token():
                resp.close()
                continue
            if resp.status_code >= 400:
                try:
                    self._raise_http(resp)
                finally:
                    resp.close()

            ctype = resp.headers.get("content-type", "").lower()
            if "text/event-stream" in ctype:
                return resp, CompletionAccumulator()

            if "text/html" in ctype:
                resp.close()
                raise DeepSeekAPIError(
                    "DeepSeek returned an HTML page instead of a response stream; check the official website",
                    status=resp.status_code,
                )

            # The API sometimes reports logical errors as 200 + JSON.
            payload = self._error_payload(resp)
            message = str(payload.get("msg") or payload.get("message") or payload.get("raw") or "unexpected response")
            code = payload.get("code")
            resp.close()
            if attempt == 0 and self._is_auth_like(code, message) and self._reload_token():
                last_error = AuthenticationError(message)
                continue
            if self._is_auth_like(code, message):
                raise AuthenticationError(
                    f"DeepSeek rejected the request: {message}. Update DEEPSEEK_TOKEN and retry."
                )
            raise DeepSeekAPIError(message, code=code, payload=payload)

        raise AuthenticationError(f"completion request failed after token reload: {last_error}")

    def continue_completion_stream(
        self, *, chat_session_id: str, message_id: int, fallback_to_resume: bool = True
    ) -> "requests.Response":
        """Press DeepSeek Web's Continue button via HTTP; no new user prompt or PoW."""
        body = {
            "chat_session_id": chat_session_id,
            "message_id": message_id,
            "fallback_to_resume": bool(fallback_to_resume),
        }
        for attempt in range(2):
            resp = self.session.post(
                f"{self.base_url}{CONTINUE_PATH}",
                json=body,
                headers=self._headers(referer=self._referer(chat_session_id)),
                stream=True,
                timeout=(self.timeout, self.stream_timeout),
            )
            if resp.status_code in (401, 403) and attempt == 0 and self._reload_token():
                resp.close()
                continue
            if resp.status_code >= 400:
                try:
                    self._raise_http(resp)
                finally:
                    resp.close()
            if "text/event-stream" in resp.headers.get("content-type", "").lower():
                return resp
            try:
                payload = self._error_payload(resp)
                self._unwrap(resp)
            finally:
                resp.close()
            raise DeepSeekAPIError(f"Continue expected an SSE stream: {payload!r}", payload=payload)
        raise AuthenticationError("Continue request failed after token reload")

    def stream_completion(
        self,
        *,
        chat_session_id: str,
        prompt: str,
        parent_message_id: Optional[int] = None,
        model_type: str = "default",
        thinking_enabled: bool = False,
        search_enabled: bool = True,
        ref_file_ids: Optional[list[Any]] = None,
        attachments: Optional[Iterable[Any]] = None,
        action: Any = None,
        preempt: bool = False,
        auto_continue: bool = True,
        max_continue_attempts: int = 20,
    ) -> Iterator[StreamChunk]:
        """Yield one logical reply; continue server-side INCOMPLETE generations."""
        if max_continue_attempts < 0:
            raise ValueError("max_continue_attempts must be non-negative")
        attached_ids = list(ref_file_ids or [])
        if attachments is not None:
            items = [attachments] if isinstance(attachments, (str, os.PathLike, bytes)) else attachments
            for item in items:
                uploaded = self.upload_file(
                    item, thinking_enabled=thinking_enabled, model_type=model_type
                )
                attached_ids.append(uploaded["file_id"])
                yield StreamChunk(
                    event="file", status=uploaded["status"],
                    filename=uploaded["filename"], file_id=uploaded["file_id"],
                )
        self.last_file_ids = [str(ref) for ref in attached_ids]
        resp, acc = self.create_completion_stream(
            chat_session_id=chat_session_id,
            prompt=prompt,
            parent_message_id=parent_message_id,
            model_type=model_type,
            thinking_enabled=thinking_enabled,
            search_enabled=search_enabled,
            ref_file_ids=attached_ids,
            action=action,
            preempt=preempt,
        )
        self.last_accumulator = acc
        try:
            while True:
                acc.closed = False
                for event, data in iter_sse(resp.iter_lines(chunk_size=1024)):
                    before_title = acc.title
                    delta_text, delta_think = acc.feed(event, data)
                    if acc.closed and acc.status not in ("FINISHED", "INCOMPLETE"):
                        raise DeepSeekAPIError(
                            f"DeepSeek closed generation with non-terminal status: {acc.status or 'unknown'}"
                        )
                    if acc.closed and acc.status == "FINISHED" and (
                        acc.text != acc._emitted_text or acc.thinking != acc._emitted_thinking
                    ):
                        raise DeepSeekAPIError(
                            "DeepSeek final response diverged from previously streamed content"
                        )
                    title = acc.title if acc.title != before_title else None
                    if delta_text or delta_think or title or acc.closed or event in ("ready", "title"):
                        yield StreamChunk(
                            event=event,
                            text=delta_text,
                            thinking=delta_think,
                            title=title,
                            status=acc.status,
                            done=acc.closed and (acc.status != "INCOMPLETE" or not auto_continue),
                            raw=data,
                        )
                    if acc.closed:
                        # Ignore any trailing close marker after [DONE]. A
                        # logical generation has exactly one terminal event.
                        break
                if not acc.closed:
                    raise DeepSeekAPIError("DeepSeek SSE stream ended without a close event")
                if acc.status != "INCOMPLETE" or not auto_continue:
                    break
                if acc.continuations >= max_continue_attempts:
                    raise DeepSeekAPIError(
                        f"DeepSeek remained INCOMPLETE after {acc.continuations} continuation(s)"
                    )
                mid = (acc.ready or {}).get("response_message_id") or acc.response.get("message_id")
                if not isinstance(mid, int):
                    raise DeepSeekAPIError("Cannot continue INCOMPLETE response without message_id")
                acc.continuations += 1
                yield StreamChunk(event="continue", status="INCOMPLETE", attempt=acc.continuations)
                resp.close()
                resp = self.continue_completion_stream(chat_session_id=chat_session_id, message_id=mid)
        finally:
            resp.close()

    def chat(
        self,
        prompt: str,
        *,
        chat_session_id: Optional[str] = None,
        parent_message_id: Optional[int] = None,
        model_type: str = "default",
        thinking_enabled: bool = False,
        search_enabled: bool = True,
        ref_file_ids: Optional[list[Any]] = None,
        attachments: Optional[Iterable[Any]] = None,
        action: Any = None,
        preempt: bool = False,
        on_text: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_chunk: Optional[Callable[[StreamChunk], None]] = None,
        auto_continue: bool = True,
        max_continue_attempts: int = 20,
        create_session: bool = True,
    ) -> ChatResult:
        """Create a session if needed, stream one completion, and return the result."""
        session_id = chat_session_id
        if not session_id:
            if not create_session:
                raise ValueError("chat_session_id is required when create_session=False")
            session_id = str(self.create_chat_session()["id"])

        self.last_accumulator = None
        for chunk in self.stream_completion(
            chat_session_id=session_id,
            prompt=prompt,
            parent_message_id=parent_message_id,
            model_type=model_type,
            thinking_enabled=thinking_enabled,
            search_enabled=search_enabled,
            ref_file_ids=ref_file_ids,
            attachments=attachments,
            action=action,
            preempt=preempt,
            auto_continue=auto_continue,
            max_continue_attempts=max_continue_attempts,
        ):
            if on_chunk:
                on_chunk(chunk)
            if chunk.text and on_text:
                on_text(chunk.text)
            if chunk.thinking and on_thinking:
                on_thinking(chunk.thinking)

        acc = self.last_accumulator or CompletionAccumulator()
        return ChatResult(
            session_id=session_id,
            text=acc.text,
            thinking=acc.thinking,
            title=acc.title,
            status=acc.status,
            request_message_id=(acc.ready or {}).get("request_message_id"),
            response_message_id=(acc.ready or {}).get("response_message_id"),
            events=acc.events,
            continuations=acc.continuations,
            file_ids=list(self.last_file_ids),
        )


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

KNOWN_POW = {
    "algorithm": "DeepSeekHashV1",
    "challenge": "60372a5805b3bd6e48252cf7b85acbfc30f047050fae70396700380de7e383ad",
    "salt": "a5d42a21679d5e8f3bea",
    "answer": 50837,
    "signature": "208c712b70ba77f2157b6cf0dfd2b4fa18bf1a8660c6c1549827e8126afb8382",
    "target_path": "/api/v0/chat/completion",
    "expire_at": 1792860993718,
    "difficulty": 144000,
}

SSE_FIXTURE = """event: ready
data: {"request_message_id":1,"response_message_id":2,"model_type":"default"}

event: update_session
data: {"updated_at":1792860699.773943}

data: {"v":{"response":{"message_id":2,"parent_id":1,"model":"","role":"ASSISTANT","thinking_enabled":false,"ban_edit":false,"ban_regenerate":false,"status":"WIP","incomplete_message":null,"accumulated_token_usage":0,"feedback":null,"inserted_at":1792860699.7627048,"search_enabled":true,"fragments":[{"id":2,"type":"RESPONSE","content":"PR","references":[],"stage_id":1}],"conversation_mode":"DEFAULT","has_pending_fragment":false,"auto_continue":false,"search_triggered":false,"extra_search_providers":[]}}}

data: {"p":"response/fragments/-1/content","o":"APPEND","v":"ON"}

data: {"v":"TO"}

data: {"p":"response","o":"BATCH","v":[{"p":"accumulated_token_usage","v":46},{"p":"quasi_status","v":"FINISHED"}]}

data: {"p":"response/status","o":"SET","v":"FINISHED"}

event: update_session
data: {"updated_at":1792860699.945451}

event: title
data: {"content":"Pronto"}

event: close
data: {"click_behavior":"none","auto_resume":false}
"""


def run_selftest() -> int:
    failures: list[str] = []

    # 1. Pure-Python hash against the answer recovered from the HAR payload.
    expected = bytes.fromhex(KNOWN_POW["challenge"])
    prefix = f"{KNOWN_POW['salt']}_{KNOWN_POW['expire_at']}_"
    got = deepseek_hash_v1(prefix.encode() + str(KNOWN_POW["answer"]).encode())
    if got != expected:
        failures.append(f"python hash mismatch: {got.hex()}")
    else:
        print("[selftest] DeepSeekHashV1 known vector: OK")

    # 2. Native solver must recover the same answer.
    solver = _ensure_native_solver()
    if solver is None:
        print("[selftest] native solver: SKIPPED (no compiler and no executable)")
    else:
        proc = subprocess.run(
            [
                str(solver),
                "solve",
                KNOWN_POW["challenge"],
                KNOWN_POW["salt"],
                str(KNOWN_POW["expire_at"]),
                str(KNOWN_POW["difficulty"]),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = proc.stdout.strip()
        if proc.returncode == 0 and out.isdigit() and int(out) == KNOWN_POW["answer"]:
            print(f"[selftest] native solver ({solver.name}): OK answer={out}")
        else:
            failures.append(f"native solver mismatch: rc={proc.returncode} out={out!r} err={proc.stderr!r}")

    # 3. Header encoding round-trip.
    header = build_pow_header(KNOWN_POW, KNOWN_POW["answer"])
    decoded = json.loads(base64.b64decode(header))
    if decoded.get("answer") != KNOWN_POW["answer"] or decoded.get("challenge") != KNOWN_POW["challenge"]:
        failures.append(f"PoW header mismatch: {decoded!r}")
    else:
        print("[selftest] x-ds-pow-response encoding: OK")

    # 4. SSE fixture must reconstruct "PRONTO".
    acc = CompletionAccumulator()
    for event, data in iter_sse(SSE_FIXTURE.splitlines()):
        acc.feed(event, data)
    if acc.text != "PRONTO":
        failures.append(f"SSE text mismatch: {acc.text!r}")
    elif acc.title != "Pronto" or acc.status != "FINISHED" or not acc.closed:
        failures.append(f"SSE metadata mismatch: title={acc.title!r} status={acc.status!r} closed={acc.closed}")
    else:
        print("[selftest] SSE patch reconstruction: OK (text='PRONTO', title='Pronto', status=FINISHED)")

    if failures:
        for failure in failures:
            print(f"[selftest] FAIL: {failure}", file=sys.stderr)
        return 1
    print("[selftest] all checks passed")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DeepSeek web chat API client with DeepSeekHashV1 proof-of-work.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python api_client.py \"Explain TLS in one paragraph\"\n"
            "  python api_client.py --thinking --show-thinking \"Why is the sky blue?\"\n"
            "  python -u api_client.py --stream --thinking \"Explain gravity\"\n"
            "  python api_client.py --session-id <uuid> --parent-message-id 42 \"Continue\"\n"
            "  python api_client.py --selftest\n\n"
            "Environment: DEEPSEEK_TOKEN (required), DEEPSEEK_DEVICE_ID, DEEPSEEK_COOKIES,\n"
            "DEEPSEEK_TOKEN_FILE, DEEPSEEK_HIF_LEIM, DEEPSEEK_LOCALE, DEEPSEEK_TIMEZONE_OFFSET."
        ),
    )
    parser.add_argument("prompt", nargs="?", help="User message. Reads stdin when omitted and not a TTY.")
    parser.add_argument("--token", help="Override DEEPSEEK_TOKEN.")
    parser.add_argument("--login", action="store_true", help="HTTP account login; save the session and login in the OS keyring.")
    parser.add_argument("--login-web", action="store_true", help="Open the official DeepSeek login page in the chosen browser and save the verified session in the OS keyring (no password collection).")
    parser.add_argument("--login-browser", choices=("default", "chrome", "msedge", "chromium", "firefox", "webkit"), default="default", help="Browser for --login-web (default: OS default; managed Firefox/Chromium/WebKit supported).")
    parser.add_argument("--login-timeout", type=float, default=300, help="Seconds to allow interactive login in the browser (default: 300).")
    parser.add_argument("--email", default="", help="Email used with --login.")
    parser.add_argument("--mobile", default="", help="Mobile number used with --login.")
    parser.add_argument("--area-code", default="", help="Mobile dialing code used with --login.")
    parser.add_argument("--password-stdin", action="store_true", help="With --login, read password from stdin instead of prompting (never as a CLI argument).")
    parser.add_argument("--forget-credentials", action="store_true", help="Delete stored DeepSeek credentials from the OS keyring.")
    parser.add_argument("--device-id", help="Override DEEPSEEK_DEVICE_ID.")
    parser.add_argument("--session-id", help="Continue an existing chat session.")
    parser.add_argument("--parent-message-id", type=int, help="Parent message id for follow-ups.")
    parser.add_argument("--model", default="default", help="model_type (default: default).")
    parser.add_argument("--thinking", action="store_true", help="Enable thinking/reasoning.")
    parser.add_argument("--no-search", action="store_true", help="Disable web search (default: enabled).")
    parser.add_argument("--file", dest="files", action="append", default=[], metavar="PATH", help="Attach a local image/document; repeat for several files.")
    parser.add_argument("--show-thinking", action="store_true", help="Print reasoning text to stderr.")
    parser.add_argument("--stream", "--jsonl", dest="stream_events", action="store_true", help="Emit flushed JSON Lines events for each text/thinking delta, then a done event.")
    parser.add_argument("--no-auto-continue", action="store_true", help="Do not resume DeepSeek INCOMPLETE output-limit responses.")
    parser.add_argument("--max-continues", type=int, default=20, help="Maximum automatic Continue requests (default: 20).")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print a JSON result instead of raw text.")
    parser.add_argument("--selftest", action="store_true", help="Run offline protocol/PoW/SSE checks.")
    parser.add_argument("--quiet", action="store_true", help="Suppress status messages on stderr.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP connect/read timeout in seconds.")
    parser.add_argument("--stream-timeout", type=float, default=180.0, help="SSE read timeout in seconds.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = build_arg_parser().parse_args(argv)

    if args.selftest:
        return run_selftest()

    if args.forget_credentials:
        from credential_store import clear_credentials

        clear_credentials()
        print("Saved DeepSeek credentials removed.")
        return 0

    if args.login_web:
        from browser_login import login_in_browser

        print("Opening the official DeepSeek login in your browser. Finish signing in there.", flush=True)
        try:
            signed_in = login_in_browser(
                browser_type=args.login_browser, timeout_seconds=args.login_timeout,
            )
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            print(f"browser login error: {exc}", file=sys.stderr)
            return 1
        if not signed_in:
            print("browser login error: No verified session was saved. Try --login-web again.", file=sys.stderr)
            return 1
        print("DeepSeek session verified and saved in the operating-system keyring.")
        return 0

    if args.login:
        if not (args.email or args.mobile):
            print("auth error: --login requires --email or --mobile", file=sys.stderr)
            return 2
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
        elif sys.stdin.isatty():
            password = getpass.getpass("DeepSeek password: ")
        else:
            print("auth error: use --password-stdin when no terminal is available", file=sys.stderr)
            return 2
        if not password:
            print("auth error: password must not be empty", file=sys.stderr)
            return 2
        try:
            client = DeepSeekClient(token="", allow_unauthed=True)
            try:
                client.login(email=args.email, mobile=args.mobile,
                             area_code=args.area_code, password=password)
            finally:
                client.session.close()
        except (AuthenticationError, requests.RequestException) as exc:
            print(f"auth error: {exc}", file=sys.stderr)
            return 1
        print("DeepSeek login succeeded; credentials saved in the OS keyring.")
        return 0

    prompt = args.prompt
    if prompt is None:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt and args.files:
            prompt = "Analise os arquivos anexados."
        if not prompt:
            build_arg_parser().print_help()
            return 2

    try:
        client = DeepSeekClient(
            token=args.token,
            device_id=args.device_id,
            timeout=args.timeout,
            stream_timeout=args.stream_timeout,
            verbose=not args.quiet,
        )

        printed_any = False

        def emit_event(payload: dict[str, Any]) -> None:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()

        def on_chunk(chunk: StreamChunk) -> None:
            if not args.stream_events:
                return
            if chunk.thinking:
                emit_event({"type": "thinking", "delta": chunk.thinking})
            if chunk.text:
                emit_event({"type": "text", "delta": chunk.text})
            if chunk.title:
                emit_event({"type": "title", "title": chunk.title})
            if chunk.event == "continue":
                emit_event({"type": "continue", "attempt": chunk.attempt})
            if chunk.event == "file":
                emit_event({"type": "file", "file_id": chunk.file_id, "filename": chunk.filename, "status": chunk.status})

        def on_text(piece: str) -> None:
            nonlocal printed_any
            if not args.json_output and not args.stream_events:
                printed_any = True
                sys.stdout.write(piece)
                sys.stdout.flush()

        def on_thinking(piece: str) -> None:
            if args.show_thinking and not args.stream_events:
                sys.stderr.write(piece)
                sys.stderr.flush()

        result = client.chat(
            prompt,
            chat_session_id=args.session_id,
            parent_message_id=args.parent_message_id,
            model_type=args.model,
            thinking_enabled=args.thinking,
            search_enabled=not args.no_search,
            attachments=args.files,
            on_text=on_text,
            on_thinking=on_thinking,
            on_chunk=on_chunk,
            auto_continue=not args.no_auto_continue,
            max_continue_attempts=args.max_continues,
        )

        if args.stream_events:
            emit_event(
                {
                    "type": "done",
                    "session_id": result.session_id,
                    "status": result.status,
                    "request_message_id": result.request_message_id,
                    "response_message_id": result.response_message_id,
                    "events": result.events,
                    "continuations": result.continuations,
                    "file_ids": result.file_ids,
                }
            )
        elif args.json_output:
            print(
                json.dumps(
                    {
                        "session_id": result.session_id,
                        "text": result.text,
                        "thinking": result.thinking,
                        "title": result.title,
                        "status": result.status,
                        "request_message_id": result.request_message_id,
                        "response_message_id": result.response_message_id,
                        "events": result.events,
                        "continuations": result.continuations,
                        "file_ids": result.file_ids,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            if printed_any:
                print()
            if not args.quiet:
                print(f"[session_id={result.session_id}]", file=sys.stderr)
        return 0

    except AuthenticationError as exc:
        print(f"auth error: {exc}", file=sys.stderr)
        return 1
    except ProofOfWorkError as exc:
        print(f"pow error: {exc}", file=sys.stderr)
        return 1
    except DeepSeekAPIError as exc:
        print(f"api error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
