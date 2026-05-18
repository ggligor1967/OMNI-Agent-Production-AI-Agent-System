"""Compatibility wrapper for the Phase 2 storage package."""

from .. import cache as _legacy_module
from ..cache import *  # noqa: F403

__all__ = getattr(
    _legacy_module,
    "__all__",
    [name for name in globals() if not name.startswith("_")],
)
