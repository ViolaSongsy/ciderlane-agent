"""Bounded HTTP retries using application configuration and one durable key."""

import math
from dataclasses import dataclass, field
from time import sleep
from urllib.parse import urlsplit

import requests


class ReturnLabelProviderError(Exception):
    """A failed attempt, identified by a safe code rather than the response body."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_http_url(value):
    if (not isinstance(value, str) or not value
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme in {"http", "https"} and bool(parts.hostname)
                and parts.username is None and parts.password is None
                and (parts.port is None or parts.port > 0))
    except ValueError:
        return False


@dataclass(frozen=True)
class ReturnLabelProvider:
    url: str
    token: str = field(repr=False)  # Hide the token when this object is printed.
    timeout_seconds: float = 5.0
    max_attempts: int = 3  # Total attempts, including the first request.

    def __post_init__(self):
        # Validate application settings before any HTTP call.
        if (not _is_http_url(self.url) or not self.token.strip()
                or any(c in self.token for c in "\r\n")
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
                or not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 5):
            raise ReturnLabelProviderError("label_provider_not_configured")

    def create_label(self, *, order_id: str, item_id: str, idempotency_key: str) -> str:
        for attempt in range(self.max_attempts):
            try:
                # Every attempt uses the same saved key and trusted identifiers.
                return self._create_label_once(
                    order_id=order_id, item_id=item_id, idempotency_key=idempotency_key,
                )
            except ReturnLabelProviderError as error:
                # Invalid responses and rejected requests will not improve with retries.
                if error.code not in {"label_provider_timeout", "label_provider_unavailable"}:
                    raise
                if attempt + 1 == self.max_attempts:
                    raise
            # Wait longer after each failure: 0.1s, 0.2s, then 0.4s, ...
            sleep(0.1 * 2 ** attempt)

    def _create_label_once(self, *, order_id: str, item_id: str, idempotency_key: str) -> str:
        try:
            with requests.post(
                self.url,
                json={"order_id": order_id, "item_id": item_id},
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Idempotency-Key": idempotency_key,
                    "Accept": "application/json",
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,  # Keep the POST on the configured endpoint.
            ) as response:
                if 500 <= response.status_code < 600:
                    raise ReturnLabelProviderError("label_provider_unavailable")
                if not 200 <= response.status_code < 300:
                    raise ReturnLabelProviderError("label_provider_http_error")
                try:
                    payload = response.json()
                except (requests.exceptions.JSONDecodeError, RecursionError):
                    raise ReturnLabelProviderError("label_provider_invalid_response") from None
        except requests.Timeout:
            raise ReturnLabelProviderError("label_provider_timeout") from None
        except requests.ConnectionError:
            raise ReturnLabelProviderError("label_provider_unavailable") from None
        except requests.RequestException:
            raise ReturnLabelProviderError("label_provider_http_error") from None

        # A 2xx response still needs a valid label URL before we accept it.
        label_url = payload.get("label_url") if isinstance(payload, dict) else None
        if not _is_http_url(label_url):
            raise ReturnLabelProviderError("label_provider_invalid_response")
        return label_url
