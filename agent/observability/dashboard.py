"""Compatibility wrapper for the Phase 2 observability package."""

from .. import dashboard as _legacy_module
from ..dashboard import *  # noqa: F403

__all__ = getattr(
    _legacy_module,
    "__all__",
    [name for name in globals() if not name.startswith("_")],
)
