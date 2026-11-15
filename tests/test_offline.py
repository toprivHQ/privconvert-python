"""Offline tests: no network, no API key needed."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import privconvert
from privconvert import PrivConvert, verify_webhook
from privconvert._client import _multipart, _filename_from

VECTOR_KEY = "pjk_testvector_do_not_use_0000000000000000000"
VECTOR_TS = 1760000000
VECTOR_BODY = (
    b'{"event":"job.finished","event_id":"evt_00000000000000000000000000000000",'
    b'"id":"job_000000000000000000000000","status":"finished","tool":"txt-to-pdf"}'
)
VECTOR_SIG = "t=1760000000,v1=609fa0d5493200864ae983137780d6fa6cfd7946953052fcaf4f6d4b8792803f"


class WebhookTests(unittest.TestCase):
    def test_published_vector(self):
        self.assertTrue(verify_webhook(VECTOR_KEY, VECTOR_SIG, VECTOR_BODY, now=VECTOR_TS))

    def test_tampered_body(self):
        self.assertFalse(verify_webhook(VECTOR_KEY, VECTOR_SIG, VECTOR_BODY + b" ", now=VECTOR_TS))

    def test_wrong_key(self):
        self.assertFalse(verify_webhook(VECTOR_KEY + "x", VECTOR_SIG, VECTOR_BODY, now=VECTOR_TS))

    def test_stale_timestamp(self):
        self.assertFalse(verify_webhook(VECTOR_KEY, VECTOR_SIG, VECTOR_BODY, now=VECTOR_TS + 301))

    def test_malformed(self):
        for sig in ("", "t=1,v1=zz", "v1=abc", None, "t=1760000000,v1=" + "0" * 63):
            self.assertFalse(verify_webhook(VECTOR_KEY, sig, VECTOR_BODY, now=VECTOR_TS))

    def test_str_body_rejected(self):
        self.assertFalse(verify_webhook(VECTOR_KEY, VECTOR_SIG, VECTOR_BODY.decode(), now=VECTOR_TS))

    def test_raise_on_error(self):
        with self.assertRaises(privconvert.WebhookVerificationError):
            verify_webhook(VECTOR_KEY, "bad", VECTOR_BODY, raise_on_error=True)


class ClientValidationTests(unittest.TestCase):
    def test_requires_key(self):
        with self.assertRaises(privconvert.PrivConvertError):
            PrivConvert(api_key="")

    def test_refuses_plain_http(self):
        with self.assertRaises(privconvert.PrivConvertError):
            PrivConvert(api_key="pk_live_x", base_url="http://example.com/api")

    def test_allows_local_http(self):
        PrivConvert(api_key="pk_live_x", base_url="http://127.0.0.1:9/api")

    def test_key_not_in_repr(self):
        pc = PrivConvert(api_key="pk_live_supersecret")
        self.assertNotIn("supersecret", repr(pc))

    def test_job_key_not_in_repr(self):
        self.assertNotIn("pjk_secret", repr(privconvert.Job(id="job_1", key="pjk_secret")))

    def test_bad_tool(self):
        pc = PrivConvert(api_key="pk_live_x")
        for t in ("../me", "a/b", "", "Word To PDF"):
            with self.assertRaises(privconvert.PrivConvertError):
                pc.convert(t, b"x")

    def test_multipart_header_injection(self):
        body, _ = _multipart([], [("file", 'evil"\r\nX-Injected: 1', b"data")])
        self.assertNotIn(b"\r\nX-Injected", body)

    def test_filename_parse(self):
        self.assertEqual(_filename_from('attachment; filename="a.pdf"'), "a.pdf")
        self.assertEqual(_filename_from("attachment; filename*=UTF-8''%D7%90.pdf"), "\u05d0.pdf")
        self.assertEqual(_filename_from('attachment; filename="../../etc/x"'), "x")


class _Handler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def _reply(self, status, body, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(n)
        _Handler.calls.append((self.path, self.headers.get("Authorization")))
        if self.path == "/api/convert/busy":
            if sum(1 for p, _ in _Handler.calls if p == self.path) < 2:
                return self._reply(503, {"error": {"code": "server_busy", "message": "busy"}}, {"Retry-After": "0"})
            return self._reply(200, b"OK", {"Content-Disposition": 'attachment; filename="out.txt"'})
        if self.path == "/api/convert/quota":
            return self._reply(429, {"detail": "quota used"}, {"Retry-After": "86400"})
        if self.path == "/api/convert/conc":
            return self._reply(429, {"error": {"code": "concurrency_limited", "message": "slow"}}, {"Retry-After": "0"})
        return self._reply(
            200, b"%PDF-1.4", {"Content-Disposition": 'attachment; filename="x.pdf"', "X-Credits-Remaining": "41"}
        )


class FakeServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pc = PrivConvert(api_key="pk_live_test", base_url=f"http://127.0.0.1:{cls.srv.server_port}/api")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_convert(self):
        r = self.pc.convert("word-to-pdf", b"hello", filename="a.docx")
        self.assertEqual(r.content, b"%PDF-1.4")
        self.assertEqual(r.filename, "x.pdf")
        self.assertEqual(r.credits_remaining, 41)
        self.assertEqual(_Handler.calls[-1][1], "Bearer pk_live_test")

    def test_retries_busy(self):
        r = self.pc.convert("busy", b"x")
        self.assertEqual(r.content, b"OK")

    def test_no_long_sleep_on_quota(self):
        with self.assertRaises(privconvert.RateLimitError) as cm:
            self.pc.convert("quota", b"x")
        self.assertEqual(cm.exception.retry_after, 86400)
        self.assertEqual(cm.exception.message, "quota used")

    def test_concurrency_error_type(self):
        with self.assertRaises(privconvert.ConcurrencyLimitError):
            self.pc.convert("conc", b"x")


if __name__ == "__main__":
    unittest.main()
