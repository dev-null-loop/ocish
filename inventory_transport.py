from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

import oci


class InventoryTransportMixin:
    """Shared OCI call retry, failure metadata, and collection helpers."""

    @staticmethod
    def _is_throttling_error(exc: Exception) -> bool:
        return isinstance(exc, oci.exceptions.ServiceError) and exc.status == 429

    def _retry_oci_call(
        self, func: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        delay = self.THROTTLE_BASE_DELAY
        for attempt in range(self.THROTTLE_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                if (
                    not self._is_throttling_error(exc)
                    or attempt >= self.THROTTLE_RETRIES
                ):
                    self._record_oci_failure(func, exc)
                    raise
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    def _record_oci_failure(self, func: Callable[..., object], exc: Exception) -> None:
        self._record_failure(
            getattr(func, "__name__", type(func).__name__), exc, state="error"
        )

    def record_partial_failure(self, operation: str, exc: Exception) -> None:
        """Record a skipped optional OCI read for the current stat envelope."""
        self._record_failure(operation, exc, state="partial")

    def _record_failure(self, operation: str, exc: Exception, *, state: str) -> None:
        status = getattr(exc, "status", None)
        code = getattr(exc, "code", None)
        message = str(getattr(exc, "message", None) or exc)
        normalized = message.casefold()
        kind = "error"
        if status in {401, 403}:
            kind = "permission-denied"
        elif "not subscribed" in normalized:
            kind = "region-not-subscribed"
        elif status == 404 or "not available" in normalized:
            kind = "service-unavailable"
        elif status == 429:
            kind = "throttled"
        self.last_oci_failure = {
            "state": state,
            "kind": kind,
            "operation": operation,
            "status": status,
            "code": code,
            "message": message,
            "request_id": getattr(exc, "request_id", None),
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _collection_items(value: object) -> list[object]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        items = getattr(value, "items", None)
        if isinstance(items, list):
            return items
        return [value]
