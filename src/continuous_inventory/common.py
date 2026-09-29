from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4


class InventoryError(ValueError):
    """A rejected business operation; callers should surface the reason."""


class Conflict(InventoryError):
    pass


class Forbidden(InventoryError):
    pass


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def quantity(value: Any, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InventoryError("Invalid decimal quantity") from None
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise InventoryError("Quantity must be finite and nonnegative (positive for actions)")
    return result


def money(value: Any) -> Decimal:
    result = quantity(value)
    if result.as_tuple().exponent < -2:
        raise InventoryError("This prototype supports money with at most two decimals")
    return result


@dataclass(frozen=True)
class Actor:
    """Trusted application identity. Must be supplied by authentication in a real API."""
    tenant: str
    name: str
    permissions: frozenset[str]

    def require(self, permission: str) -> None:
        if not self.tenant or not self.name or permission not in self.permissions:
            raise Forbidden(f"Permission required: {permission}")
