from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Optional, Union

_SIG_RE = re.compile(r"^t=(\d{1,12}),v1=([0-9a-f]{64})$")


class WebhookVerificationError(Exception):
    """Raised by ``verify_webhook(..., raise_on_error=True)``."""


def verify_webhook(
    job_key: str,
    signature: Optional[str],
    raw_body: Union[bytes, bytearray, memoryview],
    *,
    tolerance_s: int = 300,
    now: Optional[float] = None,
    raise_on_error: bool = False,
) -> bool:
    """Check an ``X-PrivConvert-Signature`` header against the raw request body.

    ``raw_body`` must be the exact bytes received, not re-serialized JSON.
    ``job_key`` is the one-time ``key`` returned when the job was submitted.
    Deliveries older than ``tolerance_s`` seconds are rejected as replays.
    """

    def fail(reason: str) -> bool:
        if raise_on_error:
            raise WebhookVerificationError(reason)
        return False

    if not job_key or not isinstance(job_key, str):
        return fail("missing job key")
    if not signature or not isinstance(signature, str):
        return fail("missing signature header")
    if isinstance(raw_body, str):
        return fail("raw_body must be bytes, not str")
    m = _SIG_RE.match(signature.strip())
    if not m:
        return fail("malformed signature header")
    ts = int(m.group(1))
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance_s:
        return fail("timestamp outside tolerance")
    sig_key = hashlib.sha256(b"pc-webhook-sig-v1|" + job_key.encode("utf-8")).digest()
    expected = hmac.new(sig_key, f"{ts}.".encode("ascii") + bytes(raw_body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, m.group(2)):
        return fail("signature mismatch")
    return True
