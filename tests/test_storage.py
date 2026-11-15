"""convert_storage against an in-memory S3 stand-in. No boto3 or network needed."""

import io
import threading
import unittest
from http.server import HTTPServer

from privconvert import PrivConvert, StorageError
from privconvert import _storage
from test_offline import _Handler


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Body(io.BytesIO):
    pass


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = []

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _ClientError("404")
        return {}

    def get_object(self, Bucket, Key):
        if Bucket == "locked":
            raise _ClientError("AccessDenied")
        if (Bucket, Key) not in self.objects:
            raise _ClientError("NoSuchKey")
        data = self.objects[(Bucket, Key)]
        return {"Body": _Body(data), "ContentLength": len(data)}

    def put_object(self, **kw):
        self.puts.append(kw)
        if kw.get("IfNoneMatch") == "*" and (kw["Bucket"], kw["Key"]) in self.objects:
            raise _ClientError("PreconditionFailed")
        self.objects[(kw["Bucket"], kw["Key"])] = kw["Body"]
        return {}


class ParseTests(unittest.TestCase):
    def test_valid(self):
        loc = _storage.parse_location("s3://my-bucket/in/contract.docx")
        self.assertEqual((loc.bucket, loc.key), ("my-bucket", "in/contract.docx"))
        self.assertEqual(str(loc), "s3://my-bucket/in/contract.docx")

    def test_rejects(self):
        for bad in ["", "my-bucket/x", "s3://my-bucket/", "s3://my-bucket/dir/", "s3://UPPER/x", "s3://ab/x",
                    "gs://b/x", "https://b.s3.amazonaws.com/x"]:
            with self.assertRaises(StorageError, msg=bad):
                _storage.parse_location(bad)


class ConvertStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pc = PrivConvert(api_key="pk_live_test", base_url=f"http://127.0.0.1:{cls.srv.server_port}/api")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.s3 = FakeS3()
        self.s3.objects[("bkt", "in/a.docx")] = b"docx bytes"

    def test_round_trip(self):
        r = self.pc.convert_storage("word-to-pdf", "s3://bkt/in/a.docx", "s3://out-bkt/a.pdf", s3_client=self.s3)
        self.assertEqual(self.s3.objects[("out-bkt", "a.pdf")], b"%PDF-1.4")
        self.assertEqual(self.s3.puts[-1]["ContentType"], "application/pdf")
        self.assertNotIn("IfNoneMatch", self.s3.puts[-1])
        self.assertEqual(r.credits_remaining, 41)

    def test_upload_args_and_encryption(self):
        self.pc.convert_storage(
            "word-to-pdf", "s3://bkt/in/a.docx", "s3://bkt/a.pdf", s3_client=self.s3,
            upload_args={"ServerSideEncryption": "aws:kms", "Bucket": "evil"},
        )
        put = self.s3.puts[-1]
        self.assertEqual(put["ServerSideEncryption"], "aws:kms")
        self.assertEqual(put["Bucket"], "bkt")

    def test_no_overwrite_checks_before_converting(self):
        self.s3.objects[("bkt", "a.pdf")] = b"old"
        before = len(_Handler.calls)
        with self.assertRaises(StorageError) as cm:
            self.pc.convert_storage("word-to-pdf", "s3://bkt/in/a.docx", "s3://bkt/a.pdf",
                                    s3_client=self.s3, overwrite=False)
        self.assertEqual(cm.exception.code, "DestinationExists")
        self.assertEqual(len(_Handler.calls), before)
        self.assertEqual(self.s3.objects[("bkt", "a.pdf")], b"old")

    def test_no_overwrite_sends_condition(self):
        self.pc.convert_storage("word-to-pdf", "s3://bkt/in/a.docx", "s3://bkt/new.pdf",
                                s3_client=self.s3, overwrite=False)
        self.assertEqual(self.s3.puts[-1]["IfNoneMatch"], "*")

    def test_missing_and_denied(self):
        with self.assertRaises(StorageError) as cm:
            self.pc.convert_storage("word-to-pdf", "s3://bkt/nope.docx", "s3://bkt/a.pdf", s3_client=self.s3)
        self.assertEqual(cm.exception.code, "NoSuchKey")
        with self.assertRaises(StorageError) as cm:
            self.pc.convert_storage("word-to-pdf", "s3://locked/a.docx", "s3://bkt/a.pdf", s3_client=self.s3)
        self.assertEqual(cm.exception.code, "AccessDenied")

    def test_size_cap(self):
        s3 = self.s3
        orig = s3.get_object
        s3.get_object = lambda Bucket, Key: {"Body": _Body(b"x"), "ContentLength": _storage.MAX_OBJECT_BYTES + 1}
        try:
            with self.assertRaises(StorageError):
                self.pc.convert_storage("word-to-pdf", "s3://bkt/in/a.docx", "s3://bkt/a.pdf", s3_client=s3)
        finally:
            s3.get_object = orig

    def test_bad_destination_fails_before_download(self):
        with self.assertRaises(StorageError):
            self.pc.convert_storage("word-to-pdf", "s3://bkt/in/a.docx", "gs://bkt/a.pdf", s3_client=self.s3)
        self.assertEqual(self.s3.puts, [])


if __name__ == "__main__":
    unittest.main()
