"""Shared retry policy for CLI, Telegram actions and queue buttons."""
from datetime import datetime
from decimal import Decimal, InvalidOperation


def payment_retry_allowed(item):
    if item.get("currency") != "RUB":
        return False
    if item.get("status") in ("ready", "rejected", "retry_exhausted"):
        return True
    if item.get("status") != "manual_error" or item.get("failure_phase") != "before_write":
        return False
    # An operator must repair malformed local data before resetting this error.
    # Legacy manual_error records have no proof of a pre-write failure.
    try:
        amount = Decimal(item["amount"])
        if not amount.is_finite() or amount <= 0:
            return False
        datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        if not isinstance(item["description"], str) or not item["description"].strip():
            return False
        for field in ("attempts", "queue_attempts"):
            if int(item.get(field, 0)) < 0:
                return False
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        return False
    return True


def refund_retry_phase(item):
    if not item.get("receipt_uuid"):
        return None
    status = item.get("status")
    if status in ("ready", "cancellation_rejected"):
        return "ready"
    if status in ("cancelled", "replacement_rejected"):
        return "cancelled"
    return None
