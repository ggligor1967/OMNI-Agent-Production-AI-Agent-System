"""Compatibility wrapper for metrics-style imports under agent.observability."""

from . import *  # noqa: F403

__all__ = [name for name in globals() if not name.startswith("_")]
