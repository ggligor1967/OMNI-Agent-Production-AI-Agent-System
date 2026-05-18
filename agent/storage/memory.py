"""Compatibility wrapper for the Phase 2 storage package."""

from .. import memory as _legacy_module
from ..memory import *  # noqa: F403

__all__ = getattr(
    _legacy_module,
    "__all__",
    [name for name in globals() if not name.startswith("_")],
)
