"""Compatibility wrapper for the Phase 2 LLM package."""

from .. import multi_model_client as _legacy_module
from ..multi_model_client import *  # noqa: F403

__all__ = getattr(
    _legacy_module,
    "__all__",
    [name for name in globals() if not name.startswith("_")],
)
