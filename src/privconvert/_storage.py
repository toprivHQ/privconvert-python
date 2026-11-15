from __future__ import annotations

import re
from typing import Any, Mapping, NamedTuple, Optional

from ._errors import StorageError

# The largest file any plan accepts. Checked before the object is read so a
# wrong key cannot pull gigabytes into memory.
MAX_OBJECT_BYTES = 500 * 1024 * 1024

_S3_RE = re.compile(r"^s3://([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/(.+)$")


class Location(NamedTuple):
    bucket: str
    key: str

    def __str__(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


def parse_location(url: str) -> Location:
    s = str(url or "")
    if s.startswith(("gs://", "az://", "azure://", "https://", "http://")):
        raise StorageError("Only s3:// locations are supported for now.")
    m = _S3_RE.match(s)
    if not m or m.group(2).endswith("/"):
        raise StorageError("Expected a location like s3://bucket/path/to/file.")
    return Location(m.group(1), m.group(2))


def default_client() -> Any:
    try:
        import boto3
    except ImportError:
        raise StorageError("S3 support needs boto3: pip install 'privconvert[s3]'") from None
    return boto3.client("s3")


def _provider_error(action: str, loc: Location, e: Exception) -> StorageError:
    err = getattr(e, "response", None) or {}
    code = (err.get("Error") or {}).get("Code") if isinstance(err, dict) else None
    return StorageError(f"Could not {action} {loc}: {code or type(e).__name__}", code=code)


def exists(client: Any, loc: Location) -> bool:
    try:
        client.head_object(Bucket=loc.bucket, Key=loc.key)
        return True
    except Exception as e:
        err = getattr(e, "response", None) or {}
        code = str((err.get("Error") or {}).get("Code", "")) if isinstance(err, dict) else ""
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise _provider_error("check", loc, e) from None


def read_object(client: Any, loc: Location) -> bytes:
    try:
        resp = client.get_object(Bucket=loc.bucket, Key=loc.key)
    except Exception as e:
        raise _provider_error("read", loc, e) from None
    body = resp["Body"]
    try:
        size = resp.get("ContentLength")
        if size is not None and int(size) > MAX_OBJECT_BYTES:
            raise StorageError(f"{loc} is larger than {MAX_OBJECT_BYTES // (1024 * 1024)} MB.")
        data = body.read(MAX_OBJECT_BYTES + 1)
    finally:
        body.close()
    if len(data) > MAX_OBJECT_BYTES:
        raise StorageError(f"{loc} is larger than {MAX_OBJECT_BYTES // (1024 * 1024)} MB.")
    return data


def write_object(
    client: Any,
    loc: Location,
    data: bytes,
    content_type: Optional[str],
    *,
    overwrite: bool,
    upload_args: Optional[Mapping[str, Any]],
) -> None:
    args = dict(upload_args or {})
    if content_type:
        args.setdefault("ContentType", content_type)
    if not overwrite:
        args["IfNoneMatch"] = "*"
    args.update(Bucket=loc.bucket, Key=loc.key, Body=data)
    try:
        client.put_object(**args)
    except Exception as e:
        raise _provider_error("write", loc, e) from None
