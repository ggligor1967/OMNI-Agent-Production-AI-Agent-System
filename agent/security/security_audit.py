"""Compatibility wrapper for the Phase 2 security package."""

from .. import security_audit as _legacy_module
from ..security_audit import *  # noqa: F403

__all__ = getattr(
    _legacy_module,
    "__all__",
    [name for name in globals() if not name.startswith("_")],
)
