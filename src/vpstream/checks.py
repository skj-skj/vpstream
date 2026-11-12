"""Checks of values read from JSON, a query or a file: what they are, not what they mean."""

from __future__ import annotations

import math
from typing import Any, TypeGuard


def is_number(value: Any) -> TypeGuard[int | float]:
    """An int or a float. JSON's true and false are ints to Python, and are not numbers."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def is_finite(value: Any) -> TypeGuard[int | float]:
    """A number that is not infinite or NaN (which JSON as Python reads it can hold)."""
    return is_number(value) and math.isfinite(value)


def is_whole(value: Any) -> TypeGuard[int]:
    """An int that is not true or false."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_digits(text: str) -> bool:
    """Only the digits 0-9. `str.isdigit` also takes "²" and other digits `int` refuses."""
    return text.isascii() and text.isdecimal()
