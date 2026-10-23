"""External anchoring of the log head hash with an RFC 3161 timestamp.

The hash chain alone cannot show that the newest events were not cut off.
A timestamp authority (TSA) signs "this hash existed at this time". Once the
head hash is stamped, every event up to it is pinned by a third party's
signature: it cannot be edited, or removed, without the stamp exposing it.
Only events after the latest anchor remain unpinned, and the verifier says
how many those are.

Requests are built in pure Python. Checking a token needs the ``openssl``
command (``openssl ts -verify``) and the system CA certificates; if it is
missing, the check reports "not verified", never "verified".
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import ledger as L
from .ledger import Ledger

TSA_URL = "http://timestamp.digicert.com"

# DER prefix for: TimeStampReq { version 1, messageImprint { sha256, <32 bytes> }, certReq TRUE }
_SHA256_REQ_PREFIX = bytes.fromhex(
    "30390201013031300d0609608648016503040201050004" + "20"
)
_CERT_REQ_TRUE = bytes.fromhex("0101ff")

ANCHOR_RECORDED = "ANCHOR_RECORDED"


class AnchorError(Exception):
    pass


@dataclass(frozen=True)
class AnchorCheck:
    ok: bool
    gen_time: str | None = None
    detail: str = ""


def imprint(head_hash: str) -> bytes:
    """What the TSA signs: SHA-256 of the head hash's hex text."""
    return hashlib.sha256(head_hash.encode("ascii")).digest()


def build_request(digest: bytes) -> bytes:
    if len(digest) != 32:
        raise AnchorError("digest must be 32 bytes")
    return _SHA256_REQ_PREFIX + digest + _CERT_REQ_TRUE


def _http_post(url: str, body: bytes) -> bytes:
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/timestamp-query"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read(200_000)
    except (urllib.error.URLError, OSError) as exc:
        raise AnchorError(f"timestamp authority unreachable ({type(exc).__name__})") from None


def verify_token(head_hash: str, token: bytes, ca_path: str | None = None) -> AnchorCheck:
    """Check a TSA reply against a head hash. Needs openssl; never raises."""
    openssl = shutil.which("openssl")
    if not openssl:
        return AnchorCheck(False, None, "openssl not found; anchor not verified")
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/token.tsr"
        with open(path, "wb") as handle:
            handle.write(token)
        cmd = [openssl, "ts", "-verify", "-digest", imprint(head_hash).hex(), "-in", path]
        cmd += ["-CAfile", ca_path] if ca_path and ca_path.endswith((".pem", ".crt")) else \
            ["-CApath", ca_path or "/etc/ssl/certs"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            text = subprocess.run([openssl, "ts", "-reply", "-in", path, "-text"],
                                  capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            return AnchorCheck(False, None, "openssl failed to run")
    ok = res.returncode == 0 and "Verification: OK" in (res.stdout + res.stderr)
    found = re.search(r"Time stamp: (.+)", text)
    return AnchorCheck(ok, found.group(1).strip() if found else None,
                       "timestamp verified" if ok else "timestamp token does not verify for this head hash")


def anchor_ledger(ledger: Ledger, tsa_url: str = TSA_URL,
                  post: Callable[[str, bytes], bytes] = _http_post,
                  verify: Callable[[str, bytes], AnchorCheck] = verify_token) -> dict:
    """Timestamp the current head hash and record the token in the log.

    The token is checked before it is stored, so a bad reply is never logged
    as if it were an anchor.
    """
    head = ledger.head_hash()
    token = post(tsa_url, build_request(imprint(head)))
    check = verify(head, token)
    if not check.ok:
        raise AnchorError(check.detail)
    event = ledger.append(ANCHOR_RECORDED, {
        "anchored_hash": head, "tsa": tsa_url, "gen_time": check.gen_time,
        "token_b64": base64.b64encode(token).decode("ascii"),
    })
    return {"anchored_hash": head, "gen_time": check.gen_time, "event_seq": event.seq}
