from __future__ import annotations

import os
import re
from typing import Any, Mapping, NamedTuple, Optional

from ._errors import StorageError

# The largest file any plan accepts. Checked before the object is read so a
# wrong key cannot pull gigabytes into memory.
MAX_OBJECT_BYTES = 500 * 1024 * 1024
_MB = MAX_OBJECT_BYTES // (1024 * 1024)

CLOUD_EXTRA = "pip install 'privconvert[cloud]'"

_BUCKET = r"([a-z0-9][a-z0-9._-]{1,220}[a-z0-9])"
_S3_RE = re.compile(rf"^s3://{_BUCKET}/(.+)$")
_GS_RE = re.compile(rf"^gs://{_BUCKET}/(.+)$")
_ACCOUNT = r"([a-z0-9]{3,24})"
_CONTAINER = r"([a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?)"
_AZ_RE = re.compile(rf"^az://{_ACCOUNT}/{_CONTAINER}/(.+)$")
_AZ_HTTPS_RE = re.compile(rf"^https://{_ACCOUNT}\.blob\.core\.windows\.net/{_CONTAINER}/(.+)$")

PROVIDERS = {"s3": "Amazon S3", "gcs": "Google Cloud Storage", "azure": "Azure Blob Storage"}


class Location(NamedTuple):
    provider: str  # "s3", "gcs" or "azure"
    bucket: str  # S3 / GCS bucket, or Azure container
    key: str
    account: Optional[str] = None  # Azure storage account

    def __str__(self) -> str:
        if self.provider == "s3":
            return f"s3://{self.bucket}/{self.key}"
        if self.provider == "gcs":
            return f"gs://{self.bucket}/{self.key}"
        return f"az://{self.account}/{self.bucket}/{self.key}"


def parse_location(url: str) -> Location:
    s = str(url or "")
    for rx, provider in ((_S3_RE, "s3"), (_GS_RE, "gcs")):
        m = rx.match(s)
        if m:
            loc = Location(provider, m.group(1), m.group(2))
            break
    else:
        m = _AZ_RE.match(s) or _AZ_HTTPS_RE.match(s)
        if not m:
            raise StorageError(
                "Expected a location like s3://bucket/file, gs://bucket/file, "
                "az://account/container/file or https://account.blob.core.windows.net/container/file."
            )
        loc = Location("azure", m.group(2), m.group(3), m.group(1))
    if loc.key.endswith("/") or "?" in loc.key or "#" in loc.key:
        raise StorageError(f"{loc} must point to a single file.")
    return loc


def _too_big(loc: Location) -> StorageError:
    return StorageError(f"{loc} is larger than {_MB} MB.")


def _check_size(loc: Location, size: Any) -> None:
    if size is not None and int(size) > MAX_OBJECT_BYTES:
        raise _too_big(loc)


# ── Amazon S3 and S3-compatible storage (Backblaze B2, Cloudflare R2, ...) ──


class _S3:
    def __init__(self, client: Any):
        self.c = client

    @staticmethod
    def default() -> Any:
        try:
            import boto3
        except ImportError:
            raise StorageError(f"Amazon S3 support needs boto3: {CLOUD_EXTRA}") from None
        return boto3.client("s3")

    @staticmethod
    def _code(e: Exception) -> Optional[str]:
        err = getattr(e, "response", None) or {}
        code = (err.get("Error") or {}).get("Code") if isinstance(err, dict) else None
        return str(code) if code else None

    def error(self, action: str, loc: Location, e: Exception) -> StorageError:
        code = self._code(e)
        return StorageError(f"Could not {action} {loc}: {code or type(e).__name__}", code=code)

    def exists(self, loc: Location) -> bool:
        try:
            self.c.head_object(Bucket=loc.bucket, Key=loc.key)
            return True
        except Exception as e:
            if self._code(e) in ("404", "NoSuchKey", "NotFound"):
                return False
            raise self.error("check", loc, e) from None

    def read(self, loc: Location) -> bytes:
        try:
            resp = self.c.get_object(Bucket=loc.bucket, Key=loc.key)
        except Exception as e:
            raise self.error("read", loc, e) from None
        body = resp["Body"]
        try:
            _check_size(loc, resp.get("ContentLength"))
            data = body.read(MAX_OBJECT_BYTES + 1)
        finally:
            body.close()
        if len(data) > MAX_OBJECT_BYTES:
            raise _too_big(loc)
        return data

    def write(self, loc: Location, data: bytes, content_type: Optional[str], overwrite: bool,
              upload_args: Mapping[str, Any]) -> None:
        args = dict(upload_args)
        if content_type:
            args.setdefault("ContentType", content_type)
        args.update(Bucket=loc.bucket, Key=loc.key, Body=data)
        try:
            if overwrite:
                self.c.put_object(**args)
                return
            try:
                self.c.put_object(**args, IfNoneMatch="*")
            except Exception as e:
                if self._code(e) in ("PreconditionFailed", "ConditionalRequestConflict"):
                    raise
                # Some S3-compatible services (Backblaze B2 among them) reject the
                # condition itself, so fall back to checking just before writing.
                if self.exists(loc):
                    raise StorageError(f"{loc} already exists.", code="DestinationExists") from None
                self.c.put_object(**args)
        except StorageError:
            raise
        except Exception as e:
            raise self.error("write", loc, e) from None


# ── Google Cloud Storage ──


class _GCS:
    def __init__(self, client: Any):
        self.c = client

    @staticmethod
    def default() -> Any:
        try:
            from google.cloud import storage
        except ImportError:
            raise StorageError(f"Google Cloud Storage support needs google-cloud-storage: {CLOUD_EXTRA}") from None
        return storage.Client()

    @staticmethod
    def _code(e: Exception) -> Optional[str]:
        status = getattr(e, "code", None)
        if isinstance(status, int):
            return {403: "Forbidden", 404: "NotFound", 412: "PreconditionFailed"}.get(status, str(status))
        return None

    def error(self, action: str, loc: Location, e: Exception) -> StorageError:
        code = self._code(e)
        return StorageError(f"Could not {action} {loc}: {code or type(e).__name__}", code=code)

    def _blob(self, loc: Location) -> Any:
        return self.c.bucket(loc.bucket).blob(loc.key)

    def exists(self, loc: Location) -> bool:
        try:
            return bool(self._blob(loc).exists())
        except Exception as e:
            raise self.error("check", loc, e) from None

    def read(self, loc: Location) -> bytes:
        try:
            blob = self.c.bucket(loc.bucket).get_blob(loc.key)
            if blob is None:
                raise StorageError(f"Could not read {loc}: NotFound", code="NotFound")
            _check_size(loc, blob.size)
            # Pinning the generation makes the size check hold for the bytes read.
            data = blob.download_as_bytes(if_generation_match=blob.generation)
        except StorageError:
            raise
        except Exception as e:
            raise self.error("read", loc, e) from None
        if len(data) > MAX_OBJECT_BYTES:
            raise _too_big(loc)
        return data

    def write(self, loc: Location, data: bytes, content_type: Optional[str], overwrite: bool,
              upload_args: Mapping[str, Any]) -> None:
        args = dict(upload_args)
        args.setdefault("content_type", content_type or "application/octet-stream")
        if not overwrite:
            args["if_generation_match"] = 0
        try:
            self._blob(loc).upload_from_string(data, **args)
        except Exception as e:
            raise self.error("write", loc, e) from None


# ── Azure Blob Storage ──


class _Azure:
    def __init__(self, client: Any):
        self.c = client

    @staticmethod
    def default(account: str) -> Any:
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            raise StorageError(f"Azure Blob Storage support needs azure-storage-blob: {CLOUD_EXTRA}") from None
        conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn:
            return BlobServiceClient.from_connection_string(conn)
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError:
            raise StorageError(
                "Azure sign-in needs azure-identity, or set AZURE_STORAGE_CONNECTION_STRING: " + CLOUD_EXTRA
            ) from None
        return BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=DefaultAzureCredential())

    @staticmethod
    def _code(e: Exception) -> Optional[str]:
        code = getattr(e, "error_code", None)
        if code:
            return str(code)
        status = getattr(e, "status_code", None)
        return str(status) if status else None

    def error(self, action: str, loc: Location, e: Exception) -> StorageError:
        code = self._code(e)
        return StorageError(f"Could not {action} {loc}: {code or type(e).__name__}", code=code)

    def _blob(self, loc: Location) -> Any:
        name = getattr(self.c, "account_name", None)
        if name and loc.account and name != loc.account:
            raise StorageError(f"azure_client is for account {name!r}, but {loc} is in {loc.account!r}.")
        return self.c.get_blob_client(container=loc.bucket, blob=loc.key)

    def exists(self, loc: Location) -> bool:
        blob = self._blob(loc)
        try:
            return bool(blob.exists())
        except Exception as e:
            raise self.error("check", loc, e) from None

    def read(self, loc: Location) -> bytes:
        blob = self._blob(loc)
        try:
            props = blob.get_blob_properties()
            _check_size(loc, props.size)
            # Pinning the ETag makes the size check hold for the bytes read.
            data = blob.download_blob(etag=props.etag, match_condition=_if_not_modified()).readall()
        except StorageError:
            raise
        except Exception as e:
            raise self.error("read", loc, e) from None
        if len(data) > MAX_OBJECT_BYTES:
            raise _too_big(loc)
        return data

    def write(self, loc: Location, data: bytes, content_type: Optional[str], overwrite: bool,
              upload_args: Mapping[str, Any]) -> None:
        from azure.storage.blob import ContentSettings

        blob = self._blob(loc)
        args = dict(upload_args)
        if content_type and "content_settings" not in args:
            args["content_settings"] = ContentSettings(content_type=content_type)
        args["overwrite"] = overwrite
        try:
            blob.upload_blob(data, **args)
        except Exception as e:
            raise self.error("write", loc, e) from None


def _if_not_modified() -> Any:
    from azure.core import MatchConditions

    return MatchConditions.IfNotModified


def backend(loc: Location, *, s3_client: Any = None, gcs_client: Any = None, azure_client: Any = None) -> Any:
    """The storage backend for a location, using the given client or the
    provider's default credential chain."""
    if loc.provider == "s3":
        return _S3(s3_client if s3_client is not None else _S3.default())
    if loc.provider == "gcs":
        return _GCS(gcs_client if gcs_client is not None else _GCS.default())
    return _Azure(azure_client if azure_client is not None else _Azure.default(loc.account or ""))
