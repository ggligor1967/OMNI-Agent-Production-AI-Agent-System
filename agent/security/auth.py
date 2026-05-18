"""Compatibility wrapper for the Phase 2 security package."""

from .. import auth as _legacy_module
from ..auth import *  # noqa: F403

__all__ = getattr(
    _legacy_module,
    "__all__",
    [name for name in globals() if not name.startswith("_")],
)
