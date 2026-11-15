from __future__ import annotations

from typing import Optional


class PrivConvertError(Exception):
    """Base class for every error raised by this package."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retry_after = retry_after

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status!r}, code={self.code!r}, message={self.message!r})"


class AuthenticationError(PrivConvertError):
    """401: missing, invalid or revoked API key."""


class BadRequestError(PrivConvertError):
    """400: malformed request, unknown tool, bad parameter or unreadable input."""


class FileTooLargeError(PrivConvertError):
    """413: the file is above your plan's size limit."""


class UnsupportedTypeError(PrivConvertError):
    """415: the input file type is not valid for this tool."""


class ConversionFailedError(PrivConvertError):
    """422: the file was read but could not be converted."""


class RateLimitError(PrivConvertError):
    """429 rate_limited: per-minute rate or monthly quota exceeded."""


class ConcurrencyLimitError(RateLimitError):
    """429 concurrency_limited: too many conversions running at once."""


class ServerError(PrivConvertError):
    """5xx: a problem on the server side. Safe to retry."""


class JobNotFoundError(PrivConvertError):
    """404 for a job: deleted, never existed, or lost in a service restart."""


class JobExpiredError(PrivConvertError):
    """410: the job result passed its retention window and was deleted."""


class StorageError(PrivConvertError):
    """Reading from or writing to your cloud storage failed. ``code`` holds the
    provider's error code when there is one, e.g. ``NoSuchKey`` or ``AccessDenied``."""


_BY_STATUS = {
    400: BadRequestError,
    401: AuthenticationError,
    403: AuthenticationError,
    413: FileTooLargeError,
    415: UnsupportedTypeError,
    422: ConversionFailedError,
    410: JobExpiredError,
}


def error_for(
    status: int,
    message: str,
    code: Optional[str],
    retry_after: Optional[float],
    *,
    job: bool = False,
) -> PrivConvertError:
    if status == 429:
        cls = ConcurrencyLimitError if code == "concurrency_limited" else RateLimitError
    elif status == 404 and job:
        cls = JobNotFoundError
    elif status >= 500:
        cls = ServerError
    else:
        cls = _BY_STATUS.get(status, PrivConvertError)
    return cls(message, status=status, code=code, retry_after=retry_after)
