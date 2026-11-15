"""Live checks against the real API. Needs PRIVCONVERT_API_KEY; spends a few credits.

    PRIVCONVERT_API_KEY=pk_live_... python tests/live_check.py [--large]
"""

import io
import os
import sys
import zipfile

import privconvert
from privconvert import PrivConvert


def main() -> int:
    pc = PrivConvert()
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))

    me = pc.me()
    check("me", "remaining" in me and "tier" in me, f"tier={me.get('tier')} remaining={me.get('remaining')}")

    tools = pc.tools(category="data")
    check("tools", any(t.get("id") == "json-to-csv" for t in tools), f"{len(tools)} data tools")

    r = pc.convert("json-to-csv", b'[{"a":1,"b":2},{"a":3,"b":4}]', filename="t.json")
    check("convert", r.content.replace(b"\r", b"").startswith(b"a,b\n1,2"), f"file={r.filename} left={r.credits_remaining}")

    b = pc.convert_batch("json-to-csv", [io.BytesIO(b'[{"x":1}]'), b'[{"y":2}]', b"not json"])
    names = zipfile.ZipFile(io.BytesIO(b.content)).namelist()
    check("batch partial", b.partial and b.failed_count == 1 and b.converted_count == 2, f"{names}")

    job = pc.submit_job("json-to-csv", b'[{"j":9}]', filename="job.json")
    res = pc.wait(job, poll_interval=1)
    check("async job", res.content.replace(b"\r", b"").startswith(b"j\n9"), f"id={job.id}")
    try:
        pc.get_job(job)
        check("job deleted after wait", False)
    except privconvert.JobNotFoundError:
        check("job deleted after wait", True)

    try:
        pc.convert("json-to-csv", b"this is not json at all {")
        check("error mapping", False)
    except privconvert.PrivConvertError as e:
        check("error mapping", e.status in (400, 422), f"{type(e).__name__} {e.status} {e.message[:60]}")

    try:
        PrivConvert(api_key="pk_live_" + "x" * 40).me()
        check("bad key", False)
    except privconvert.AuthenticationError:
        check("bad key", True)

    if "--large" in sys.argv:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            z.writestr("big.bin", os.urandom(95 * 1024 * 1024))
        data = buf.getvalue()
        r = pc.convert("zip-to-targz", data, filename="big.zip")
        check("chunked upload", r.content[:2] == b"\x1f\x8b" and len(r.content) > 90 * 1024 * 1024,
              f"in={len(data)>>20}MB out={len(r.content)>>20}MB")

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
