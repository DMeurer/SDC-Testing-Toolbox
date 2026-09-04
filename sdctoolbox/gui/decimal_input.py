"""Validation for Decimal values typed into GUI fields."""

from __future__ import annotations

from decimal import Decimal


class DecimalInputError(ValueError):
    """A field-specific error suitable for showing directly to the user."""


def parse_decimal_input(
    text: str,
    field: str,
    *,
    positive: bool = False,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    """Parse a finite Decimal and optionally enforce positivity or inclusive bounds."""
    raw = text.strip()
    try:
        value = Decimal(raw)
    except (ArithmeticError, TypeError, ValueError) as exc:
        msg = f"The {field} must be a finite number; {raw!r} is not valid."
        raise DecimalInputError(msg) from exc

    if not value.is_finite():
        msg = f"The {field} must be a finite number; {raw!r} is not valid."
        raise DecimalInputError(msg)

    try:
        if positive and value <= 0:
            msg = f"The {field} must be greater than zero."
            raise DecimalInputError(msg)
        if minimum is not None and value < minimum:
            msg = f"The {field} must be at least {minimum}."
            raise DecimalInputError(msg)
        if maximum is not None and value > maximum:
            msg = f"The {field} must be at most {maximum}."
            raise DecimalInputError(msg)
    except DecimalInputError:
        raise
    except (ArithmeticError, TypeError, ValueError) as exc:
        msg = f"The {field} cannot be compared with its allowed range."
        raise DecimalInputError(msg) from exc

    return value
