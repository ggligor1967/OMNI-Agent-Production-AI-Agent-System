"""Compatibility wrapper for the Phase 2 integrations package."""

from .. import notifications as _legacy_module
from ..notifications import *  # noqa: F403

__all__ = getattr(
    _legacy_module,
    "__all__",
    [name for name in globals() if not name.startswith("_")],
)
