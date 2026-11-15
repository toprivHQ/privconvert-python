from __future__ import annotations

import json
import os
import posixpath
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import IO, Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from . import _storage
from ._errors import PrivConvertError, StorageError, error_for
from ._version import __version__

DEFAULT_BASE_URL = "https://privconvert.com/api"

# A single POST accepts up to 100 MB; above this the file goes through the
# chunked upload flow. Parts stay well below the 95 MB per-part ceiling so a
# slow link never holds one request open long enough to time out at the edge.
DIRECT_LIMIT = 90 * 1024 * 1024
CHUNK_SIZE = 20 * 1024 * 1024

# A Retry-After longer than this means the monthly quota is used up, not a
# momentary limit, so the call fails instead of sleeping.
MAX_RETRY_WAIT_S = 60.0

_TOOL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_STATUS_BY_CODE = {
    "bad_request": 400,
    "unauthorized": 401,
    "file_too_large": 413,
    "unsupported_type": 415,
    "conversion_failed": 422,
    "rate_limited": 429,
    "concurrency_limited": 429,
    "server_error": 500,
    "server_busy": 503,
}

FileInput = Union[str, "os.PathLike[str]", bytes, bytearray, memoryview, IO[bytes]]


@dataclass
class Result:
    """A converted file, held in memory."""

    content: bytes
    filename: Optional[str]
    content_type: Optional[str]
    headers: Dict[str, str] = field(repr=False, default_factory=dict)

    @property
    def credits_remaining(self) -> Optional[int]:
        v = self.headers.get("x-credits-remaining")
        return int(v) if v and v.isdigit() else None

    @property
    def converted_count(self) -> Optional[int]:
        v = self.headers.get("x-converted-count")
        return int(v) if v and v.isdigit() else None

    @property
    def failed_count(self) -> int:
        v = self.headers.get("x-failed-count")
        return int(v) if v and v.isdigit() else 0

    @property
    def partial(self) -> bool:
        return "x-partial-success" in self.headers

    @property
    def sanitize_report(self) -> Optional[Dict[str, Any]]:
        v = self.headers.get("x-sanitize-report")
        if not v:
            return None
        try:
            return json.loads(v)
        except ValueError:
            return None

    def save(self, path: Union[str, "os.PathLike[str]"]) -> str:
        """Write the result to ``path`` and return the path."""
        p = os.fspath(path)
        with open(p, "wb") as f:
            f.write(self.content)
        return p


@dataclass
class Job:
    """A submitted async job. ``key`` is returned exactly once: store it."""

    id: str
    key: str = field(repr=False)
    status: str = "processing"


@dataclass
class JobStatus:
    id: str
    status: str
    raw: Dict[str, Any] = field(repr=False)

    @property
    def done(self) -> bool:
        return self.status in ("finished", "failed")

    @property
    def error(self) -> Optional[Dict[str, Any]]:
        e = self.raw.get("error")
        return e if isinstance(e, dict) else None

    @property
    def expires_at(self) -> Optional[int]:
        return self.raw.get("expires_at")


class PrivConvert:
    """Client for the PrivConvert API.

    The key is read from ``api_key`` or the ``PRIVCONVERT_API_KEY`` environment
    variable. Files are read into memory and never written to disk by this
    package; only ``Result.save`` writes, and only where you tell it to.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 300.0,
        max_retries: int = 2,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("PRIVCONVERT_API_KEY", "")
        key = (key or "").strip()
        if not key:
            raise PrivConvertError("No API key. Pass api_key=... or set PRIVCONVERT_API_KEY.")
        if any(c.isspace() for c in key) or not key.isprintable():
            raise PrivConvertError("The API key contains invalid characters.")
        self._key = key
        self.base_url = _check_base_url(base_url)
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))

    def __repr__(self) -> str:
        return f"PrivConvert(base_url={self.base_url!r})"

    # ── conversions ──────────────────────────────────────────────────────

    def convert(
        self,
        tool: str,
        file: FileInput,
        *,
        filename: Optional[str] = None,
        **params: Any,
    ) -> Result:
        """Convert one file. Files above 90 MB are uploaded in parts automatically.

        Extra keyword arguments are sent as form fields, e.g. ``quality=80``.
        """
        tool = _check_tool(tool)
        name, data = _read_input(file, filename)
        if len(data) > DIRECT_LIMIT:
            return self._convert_chunked(tool, name, data, params)
        fields = _form_params(params)
        return self._request_file("POST", f"/convert/{tool}", fields=fields, files=[("file", name, data)])

    def convert_batch(
        self,
        tool: str,
        files: Iterable[FileInput],
        **params: Any,
    ) -> Result:
        """Run one tool over up to 20 files; the result is a ZIP.

        Check ``result.failed_count`` / ``result.partial``: a batch where some
        files failed still succeeds with the files that converted.
        """
        tool = _check_tool(tool)
        parts = []
        for i, f in enumerate(files):
            name, data = _read_input(f, None)
            if name == "file":
                name = f"file{i + 1}"
            parts.append(("files", name, data))
        if not parts:
            raise PrivConvertError("convert_batch needs at least one file.")
        if len(parts) > 20:
            raise PrivConvertError("convert_batch takes at most 20 files per request.")
        fields = [("tool", tool)] + _form_params(params)
        return self._request_file("POST", "/convert-batch", fields=fields, files=parts)

    def convert_storage(
        self,
        tool: str,
        source: str,
        destination: str,
        *,
        s3_client: Any = None,
        gcs_client: Any = None,
        azure_client: Any = None,
        overwrite: bool = True,
        upload_args: Optional[Mapping[str, Any]] = None,
        **params: Any,
    ) -> Result:
        """Convert a file in cloud storage and write the result back to storage.

        Locations are ``s3://bucket/key``, ``gs://bucket/key``,
        ``az://account/container/blob`` or
        ``https://account.blob.core.windows.net/container/blob``. Source and
        destination may be in different clouds. The file moves between your
        storage and PrivConvert from wherever this code runs, using your own
        credentials (each provider's standard chain, or the client you pass).
        Your credentials are never sent to PrivConvert. The cloud libraries
        are used if installed; ``pip install 'privconvert[cloud]'`` adds them.

        For S3-compatible storage such as Backblaze B2 or Cloudflare R2, pass
        an ``s3_client`` created with that provider's ``endpoint_url``.

        ``upload_args`` go to the destination's upload call (``put_object``,
        ``Blob.upload_from_string`` or ``BlobClient.upload_blob``). With
        ``overwrite=False`` an existing destination is left untouched and
        ``StorageError`` is raised before any credit is spent.
        """
        tool = _check_tool(tool)
        src = _storage.parse_location(source)
        dst = _storage.parse_location(destination)
        clients = {"s3_client": s3_client, "gcs_client": gcs_client, "azure_client": azure_client}
        reader = _storage.backend(src, **clients)
        same = (src.provider, src.account) == (dst.provider, dst.account)
        writer = reader if same else _storage.backend(dst, **clients)
        if not overwrite and writer.exists(dst):
            raise StorageError(f"{dst} already exists.", code="DestinationExists")
        data = reader.read(src)
        result = self.convert(tool, data, filename=posixpath.basename(src.key), **params)
        writer.write(dst, result.content, result.content_type, overwrite, upload_args or {})
        return result

    # ── async jobs ───────────────────────────────────────────────────────

    def submit_job(
        self,
        tool: str,
        file: FileInput,
        *,
        filename: Optional[str] = None,
        webhook_url: Optional[str] = None,
        **params: Any,
    ) -> Job:
        """Submit an async job. Returns at once with the job id and one-time key."""
        tool = _check_tool(tool)
        name, data = _read_input(file, filename)
        fields = _form_params(params)
        if webhook_url:
            if not str(webhook_url).startswith("https://"):
                raise PrivConvertError("webhook_url must be an https:// URL.")
            fields.append(("webhook_url", str(webhook_url)))
        body = self._request_json("POST", f"/jobs/{tool}", fields=fields, files=[("file", name, data)])
        if not isinstance(body, dict) or "id" not in body or "key" not in body:
            raise PrivConvertError("Unexpected response when submitting the job.")
        return Job(id=str(body["id"]), key=str(body["key"]), status=str(body.get("status", "processing")))

    def get_job(self, job_id: Union[str, Job]) -> JobStatus:
        jid = _check_job_id(job_id)
        body = self._request_json("GET", f"/jobs/{jid}", job=True)
        return JobStatus(id=jid, status=str(body.get("status", "")), raw=body)

    def download_job(self, job: Job) -> Result:
        jid = _check_job_id(job)
        return self._request_file(
            "GET", f"/jobs/{jid}/download", extra_headers={"X-Job-Key": job.key}, job=True
        )

    def delete_job(self, job_id: Union[str, Job]) -> None:
        jid = _check_job_id(job_id)
        self._request("DELETE", f"/jobs/{jid}", job=True)

    def wait(
        self,
        job: Job,
        *,
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
        delete_after: bool = True,
    ) -> Result:
        """Poll until the job finishes, then download the result.

        Raises the same error a synchronous call would have raised if the job
        failed. With ``delete_after`` the stored result is wiped right after
        download instead of waiting out the retention window.
        """
        deadline = time.monotonic() + timeout
        interval = max(0.5, poll_interval)
        while True:
            st = self.get_job(job)
            if st.status == "finished":
                break
            if st.status == "failed":
                e = st.error or {}
                code = e.get("code")
                raise error_for(
                    _STATUS_BY_CODE.get(str(code), 422),
                    str(e.get("message") or "The conversion failed."),
                    code,
                    None,
                )
            if time.monotonic() + interval > deadline:
                raise PrivConvertError(f"Job {job.id} did not finish within {timeout:.0f}s.")
            time.sleep(interval)
            interval = min(interval * 1.5, 10.0)
        result = self.download_job(job)
        if delete_after:
            try:
                self.delete_job(job)
            except PrivConvertError:
                pass
        return result

    # ── account & catalog ────────────────────────────────────────────────

    def me(self) -> Dict[str, Any]:
        """Your plan, monthly quota and current usage."""
        return self._request_json("GET", "/me")

    def tools(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """The public catalog of every tool, optionally filtered by category."""
        path = "/tools"
        if category:
            path += "?" + urllib.parse.urlencode({"category": category})
        body = self._request_json("GET", path, auth=False)
        return list(body.get("tools", [])) if isinstance(body, dict) else []

    # ── internals ────────────────────────────────────────────────────────

    def _convert_chunked(self, tool: str, name: str, data: bytes, params: Mapping[str, Any]) -> Result:
        chunks = [data[i : i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]
        init = self._request_json(
            "POST",
            "/upload-init",
            fields=[
                ("total_size", str(len(data))),
                ("total_chunks", str(len(chunks))),
                ("filename", name),
                ("tool", tool),
            ],
        )
        upload_id = str(init.get("upload_id", ""))
        if not upload_id:
            raise PrivConvertError("The server did not return an upload id.")
        for i, chunk in enumerate(chunks):
            self._request_json(
                "POST",
                "/upload-chunk",
                fields=[("upload_id", upload_id), ("chunk_index", str(i))],
                files=[("file", "chunk", chunk)],
            )
        fields = [("upload_id", upload_id)] + _form_params(params)
        return self._request_file("POST", "/convert-chunked", fields=fields)

    def _request_file(self, method: str, path: str, **kw: Any) -> Result:
        status, headers, body = self._request(method, path, **kw)
        return Result(
            content=body,
            filename=_filename_from(headers.get("content-disposition", "")),
            content_type=headers.get("content-type"),
            headers=headers,
        )

    def _request_json(self, method: str, path: str, **kw: Any) -> Dict[str, Any]:
        status, headers, body = self._request(method, path, **kw)
        try:
            out = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            raise PrivConvertError("The server returned a response that is not JSON.", status=status)
        return out if isinstance(out, dict) else {"data": out}

    def _request(
        self,
        method: str,
        path: str,
        *,
        fields: Optional[List[Tuple[str, str]]] = None,
        files: Optional[List[Tuple[str, str, bytes]]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        auth: bool = True,
        job: bool = False,
    ) -> Tuple[int, Dict[str, str], bytes]:
        url = self.base_url + path
        headers = {"User-Agent": f"privconvert-python/{__version__}", "Accept": "*/*"}
        if auth:
            headers["Authorization"] = f"Bearer {self._key}"
        if extra_headers:
            headers.update(extra_headers)
        body: Optional[bytes] = None
        if fields is not None or files is not None:
            body, ctype = _multipart(fields or [], files or [])
            headers["Content-Type"] = ctype
        elif method in ("POST", "PUT"):
            body = b""

        attempt = 0
        while True:
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, _lower(resp.headers.items()), resp.read()
            except urllib.error.HTTPError as e:
                status = e.code
                h = _lower(e.headers.items()) if e.headers else {}
                raw = e.read() if hasattr(e, "read") else b""
                message, code = _parse_error(raw, status)
                retry_after = _retry_after(h.get("retry-after"))
                retryable = status in (429, 502, 503, 504)
                wait = retry_after if retry_after is not None else min(2.0 * (2 ** attempt), 20.0)
                if retryable and attempt < self.max_retries and wait <= MAX_RETRY_WAIT_S:
                    attempt += 1
                    time.sleep(wait)
                    continue
                raise error_for(status, message, code, retry_after, job=job) from None
            except urllib.error.URLError as e:
                raise PrivConvertError(f"Could not reach PrivConvert: {e.reason}") from None


def _check_base_url(url: str) -> str:
    u = urllib.parse.urlsplit(str(url).rstrip("/"))
    if u.scheme == "https" and u.hostname:
        return urllib.parse.urlunsplit(u)
    if u.scheme == "http" and u.hostname in _LOCAL_HOSTS:
        return urllib.parse.urlunsplit(u)
    raise PrivConvertError("base_url must use https:// so the API key is never sent in clear text.")


def _check_tool(tool: str) -> str:
    t = str(tool or "").strip().lower()
    if not _TOOL_RE.match(t):
        raise PrivConvertError(f"Invalid tool id: {tool!r}")
    return t


def _check_job_id(job: Union[str, Job]) -> str:
    jid = job.id if isinstance(job, Job) else str(job or "")
    if not _JOB_ID_RE.match(jid):
        raise PrivConvertError("Invalid job id.")
    return jid


def _read_input(file: FileInput, filename: Optional[str]) -> Tuple[str, bytes]:
    if isinstance(file, (bytes, bytearray, memoryview)):
        return (filename or "file"), bytes(file)
    if isinstance(file, (str, os.PathLike)):
        p = os.fspath(file)
        with open(p, "rb") as f:
            data = f.read()
        return (filename or os.path.basename(p) or "file"), data
    if hasattr(file, "read"):
        data = file.read()
        if isinstance(data, str):
            raise PrivConvertError("Open the file in binary mode ('rb').")
        name = filename or os.path.basename(str(getattr(file, "name", "") or "")) or "file"
        return name, bytes(data)
    raise PrivConvertError("file must be a path, bytes, or a binary file object.")


def _form_params(params: Mapping[str, Any]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (list, dict)):
            v = json.dumps(v)
        out.append((str(k), str(v)))
    return out


def _safe_header_value(s: str) -> str:
    return re.sub(r'[\r\n"\\]', "_", s)[:255] or "file"


def _multipart(fields: List[Tuple[str, str]], files: List[Tuple[str, str, bytes]]) -> Tuple[bytes, str]:
    boundary = "----privconvert" + secrets.token_hex(16)
    parts: List[bytes] = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{_safe_header_value(name)}"\r\n\r\n'.encode()
            + value.encode("utf-8")
            + b"\r\n"
        )
    for name, fname, data in files:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{_safe_header_value(name)}"; filename="{_safe_header_value(fname)}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            + data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _lower(items: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    return {k.lower(): v for k, v in items}


def _parse_error(raw: bytes, status: int) -> Tuple[str, Optional[str]]:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return f"HTTP {status}", None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or f"HTTP {status}"), err.get("code")
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail, body.get("code")
        if isinstance(detail, dict):
            return str(detail.get("message") or f"HTTP {status}"), detail.get("code")
    return f"HTTP {status}", None


def _retry_after(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None


def _filename_from(cd: str) -> Optional[str]:
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I)
    if m:
        return os.path.basename(urllib.parse.unquote(m.group(1)))
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    return os.path.basename(m.group(1)) if m else None
