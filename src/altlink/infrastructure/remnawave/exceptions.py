from __future__ import annotations


class RemnawaveError(Exception):
    """Base integration error."""


class RemnawaveRequestError(RemnawaveError):
    """HTTP or transport failure."""


class RemnawaveNotFoundError(RemnawaveError):
    """Entity not found."""

