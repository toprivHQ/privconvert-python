"""Official Python client for the PrivConvert API.

    from privconvert import PrivConvert

    pc = PrivConvert()  # reads PRIVCONVERT_API_KEY
    pc.convert("word-to-pdf", "contract.docx").save("contract.pdf")
"""

from ._client import PrivConvert, Result, Job, JobStatus
from ._errors import (
    PrivConvertError,
    AuthenticationError,
    BadRequestError,
    FileTooLargeError,
    UnsupportedTypeError,
    ConversionFailedError,
    RateLimitError,
    ConcurrencyLimitError,
    ServerError,
    JobExpiredError,
    JobNotFoundError,
    StorageError,
)
from ._webhooks import verify_webhook, WebhookVerificationError
from ._version import __version__

__all__ = [
    "PrivConvert",
    "Result",
    "Job",
    "JobStatus",
    "PrivConvertError",
    "AuthenticationError",
    "BadRequestError",
    "FileTooLargeError",
    "UnsupportedTypeError",
    "ConversionFailedError",
    "RateLimitError",
    "ConcurrencyLimitError",
    "ServerError",
    "JobExpiredError",
    "JobNotFoundError",
    "StorageError",
    "verify_webhook",
    "WebhookVerificationError",
    "__version__",
]
