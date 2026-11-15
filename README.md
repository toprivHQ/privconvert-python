# PrivConvert for Python

Official Python client for the [PrivConvert API](https://privconvert.com/developers/): file conversion for your app with zero data retention. Every file is processed in memory on our side and never written to disk.

- No dependencies, standard library only
- Python 3.10+
- Large files (above 90 MB) are uploaded in parts automatically
- Async jobs, webhook signature verification and typed errors included

## Install

```bash
pip install privconvert
```

## Quick start

Create a key on your [account page](https://privconvert.com/account/#apikeys) (new accounts can start a free trial of 25 conversions) and set it as an environment variable:

```bash
export PRIVCONVERT_API_KEY=pk_live_...
```

```python
from privconvert import PrivConvert

pc = PrivConvert()

result = pc.convert("word-to-pdf", "contract.docx")
result.save("contract.pdf")
print(result.credits_remaining)
```

`file` can be a path, `bytes`, or a binary file object. Extra keyword arguments are sent as form fields:

```python
pc.convert("compress-image", open("photo.png", "rb"), quality=70).save("photo-small.png")
pc.convert("resize-image", image_bytes, filename="in.jpg", width=800)
```

The tool id is the same slug as on the site. List them with `pc.tools()` or `pc.tools(category="document")`.

## Many files at once

Up to 20 files in one request, returned as a ZIP. A batch costs one slot of your per-minute rate limit.

```python
zip_result = pc.convert_batch("png-to-jpg", ["a.png", "b.png", "c.png"])
zip_result.save("converted.zip")
if zip_result.partial:
    print(zip_result.failed_count, "files were skipped, see PRIVCONVERT-ERRORS.txt in the ZIP")
```

## Async jobs

```python
job = pc.submit_job("pptx-to-pdf", "deck.pptx")
# job.key is returned once. Keep it if you need to download later or verify webhooks.
result = pc.wait(job)          # polls, downloads, then deletes the stored result
result.save("deck.pdf")
```

With a webhook instead of polling:

```python
job = pc.submit_job("pptx-to-pdf", "deck.pptx", webhook_url="https://example.com/hooks/privconvert")
save_somewhere(job.id, job.key)
# later, when the webhook arrives:
result = pc.download_job(job)
```

## Verifying webhooks

Pass the raw request bytes, not parsed JSON:

```python
from privconvert import verify_webhook

@app.post("/hooks/privconvert")
def hook():
    job_key = load_job_key(request.json["id"])
    if not verify_webhook(job_key, request.headers.get("X-PrivConvert-Signature"), request.get_data()):
        abort(400)
    ...
```

Deliveries older than 5 minutes are rejected as replays (`tolerance_s` to change it).

## Errors

Every error is a `PrivConvertError` with `status`, `code`, `message` and `retry_after`:

| Exception | When |
| --- | --- |
| `AuthenticationError` | Missing or invalid key (401) |
| `BadRequestError` | Bad request or unreadable input (400) |
| `FileTooLargeError` | Above your plan's size limit (413) |
| `UnsupportedTypeError` | Wrong input type for the tool (415) |
| `ConversionFailedError` | File read but could not be converted (422) |
| `RateLimitError` | Per-minute rate or monthly quota exceeded (429) |
| `ConcurrencyLimitError` | Too many conversions at once (429) |
| `ServerError` | Problem on our side (5xx) |

Short 429, 502, 503 and 504 responses are retried automatically (`max_retries=2`), honouring `Retry-After`. A long `Retry-After`, which means the monthly quota is used up, raises at once instead of sleeping.

## Usage and limits

```python
pc.me()
# {'tier': 'growth', 'quota': 25000, 'used': 1234, 'remaining': 23766, 'rpm': 120, ...}
```

## Security notes

- The key is read from `PRIVCONVERT_API_KEY` or passed explicitly, and never appears in `repr()` or error messages.
- Requests go over HTTPS only. A plain `http://` base URL is refused.
- This package writes nothing to disk except where you call `Result.save()`.

## Links

- [API documentation](https://privconvert.com/docs/)
- [Pricing and free trial](https://privconvert.com/developers/)
- Support: contact@topriv.com

MIT License
