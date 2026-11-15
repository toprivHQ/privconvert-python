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

    def test_other_clouds(self):
        self.assertEqual(tuple(_storage.parse_location("gs://my_bucket/a/b.docx")), ("gcs", "my_bucket", "a/b.docx", None))
        for url in ["az://acct01/docs/in/a.docx", "https://acct01.blob.core.windows.net/docs/in/a.docx"]:
            loc = _storage.parse_location(url)
            self.assertEqual(tuple(loc), ("azure", "docs", "in/a.docx", "acct01"))
            self.assertEqual(str(loc), "az://acct01/docs/in/a.docx")

    def test_rejects(self):
        for bad in ["", "my-bucket/x", "s3://my-bucket/", "s3://my-bucket/dir/", "s3://UPPER/x", "s3://ab/x",
                    "gs://b/x", "https://b.s3.amazonaws.com/x", "http://acct01.blob.core.windows.net/cc1/x",
                    "https://acct01.blob.core.windows.net.evil.com/cc1/x", "https://evil.com/acct01.blob.core.windows.net/cc1/x",
                    "az://acct01/cc1/", "az://Acct/c1/x", "s3://bkt/a.pdf?x=1", "ftp://bkt/x"]:
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
            self.pc.convert_storage("word-to-pdf", "s3://bkt/in/a.docx", "ftp://bkt/a.pdf", s3_client=self.s3)
        self.assertEqual(self.s3.puts, [])

    def test_cross_cloud_s3_to_gcs(self):
        gcs = FakeGCS()
        self.pc.convert_storage("word-to-pdf", "s3://bkt/in/a.docx", "gs://out_bkt/a.pdf",
                                s3_client=self.s3, gcs_client=gcs, upload_args={"timeout": 30})
        self.assertEqual(gcs.objects[("out_bkt", "a.pdf")], b"%PDF-1.4")
        self.assertEqual(gcs.uploads[-1], {"content_type": "application/pdf", "timeout": 30})
        self.assertEqual(self.s3.puts, [])

    def test_gcs_no_overwrite_and_missing(self):
        gcs = FakeGCS()
        gcs.objects[("bk1", "in.docx")] = b"docx"
        self.pc.convert_storage("word-to-pdf", "gs://bk1/in.docx", "gs://bk1/out.pdf", gcs_client=gcs, overwrite=False)
        self.assertEqual(gcs.uploads[-1]["if_generation_match"], 0)
        with self.assertRaises(StorageError) as cm:
            self.pc.convert_storage("word-to-pdf", "gs://bk1/in.docx", "gs://bk1/out.pdf", gcs_client=gcs, overwrite=False)
        self.assertEqual(cm.exception.code, "DestinationExists")
        with self.assertRaises(StorageError) as cm:
            self.pc.convert_storage("word-to-pdf", "gs://bk1/nope.docx", "gs://bk1/x.pdf", gcs_client=gcs)
        self.assertEqual(cm.exception.code, "NotFound")

    def test_missing_library_message(self):
        try:
            import google.cloud.storage  # noqa: F401
            self.skipTest("google-cloud-storage is installed")
        except ImportError:
            pass
        with self.assertRaises(StorageError) as cm:
            self.pc.convert_storage("word-to-pdf", "gs://bk1/in.docx", "gs://bk1/out.pdf")
        self.assertIn("privconvert[cloud]", str(cm.exception))

    def test_azure_account_mismatch(self):
        class Svc:
            account_name = "other01"
        with self.assertRaises(StorageError):
            self.pc.convert_storage("word-to-pdf", "az://acct01/cc1/a.docx", "az://acct01/cc1/a.pdf", azure_client=Svc())


class FakeGCS:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        fake = self

        class Blob:
            def __init__(self, bucket, key):
                self.bucket, self.key = bucket, key
                data = fake.objects.get((bucket, key))
                self.size = None if data is None else len(data)
                self.generation = 1

            def exists(self):
                return (self.bucket, self.key) in fake.objects

            def download_as_bytes(self, **kw):
                return fake.objects[(self.bucket, self.key)]

            def upload_from_string(self, data, **kw):
                fake.uploads.append(kw)
                if kw.get("if_generation_match") == 0 and self.exists():
                    e = Exception("precondition")
                    e.code = 412
                    raise e
                fake.objects[(self.bucket, self.key)] = data

        class Bucket:
            def __init__(self, name):
                self.name = name

            def blob(self, key):
                return Blob(self.name, key)

            def get_blob(self, key):
                return Blob(self.name, key) if (self.name, key) in fake.objects else None

        self._bucket = Bucket

    def bucket(self, name):
        return self._bucket(name)


if __name__ == "__main__":
    unittest.main()
