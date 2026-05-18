"""
OMNI AGENT - Trigger & Hooks System
Event-driven architecture for agent actions.
"""
import asyncio
import logging
import inspect
from typing import Callable, Dict, List, Any, Optional
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    # Lifecycle
    AGENT_START        = "agent.start"
    AGENT_STOP         = "agent.stop"
    AGENT_ERROR        = "agent.error"

    # Messages
    MESSAGE_RECEIVED   = "message.received"
    MESSAGE_SENT       = "message.sent"

    # Tools
    TOOL_CALLED        = "tool.called"
    TOOL_RESULT        = "tool.result"
    TOOL_ERROR         = "tool.error"

    # Memory
    MEMORY_SAVED       = "memory.saved"
    MEMORY_RETRIEVED   = "memory.retrieved"

    # Skills
    SKILL_LOADED       = "skill.loaded"
    SKILL_EXECUTED     = "skill.executed"

    # Scheduler
    JOB_STARTED        = "job.started"
    JOB_COMPLETED      = "job.completed"
    JOB_FAILED         = "job.failed"

    # Security
    SECURITY_ALERT     = "security.alert"
    RATE_LIMIT_HIT     = "rate_limit.hit"

    # Custom
    CUSTOM             = "custom"


@dataclass
class Event:
    type: EventType
    data: Dict[str, Any] = field(default_factory=dict)
    source: str = "agent"
    propagate: bool = True  # set False to stop hook chain


@dataclass
class HookRegistration:
    event: EventType
    handler: Callable
    priority: int = 5      # 1=highest, 10=lowest
    name: str = ""
    enabled: bool = True
    once: bool = False      # auto-remove after first call


class HookSystem:
    """Async-first event/hook dispatcher with priority ordering."""

    def __init__(self):
        self._hooks: Dict[str, List[HookRegistration]] = {}
        self._wildcard: List[HookRegistration] = []

    def on(self, event: EventType, handler: Callable,
           priority: int = 5, name: str = "", once: bool = False):
        """Register a hook. Handler may be sync or async."""
        reg = HookRegistration(
            event=event,
            handler=handler,
            priority=priority,
            name=name or handler.__name__,
            once=once
        )
        key = event.value
        if key not in self._hooks:
            self._hooks[key] = []
        self._hooks[key].append(reg)
        self._hooks[key].sort(key=lambda r: r.priority)
        logger.debug(f"Hook registered: {key} → {reg.name}")
        return reg

    def on_any(self, handler: Callable, priority: int = 5):
        """Register a wildcard hook that fires on every event."""
        reg = HookRegistration(event=EventType.CUSTOM, handler=handler,
                               priority=priority, name=handler.__name__)
        self._wildcard.append(reg)
        return reg

    def off(self, name: str):
        """Remove all hooks with the given name."""
        for hooks in self._hooks.values():
            hooks[:] = [h for h in hooks if h.name != name]
        self._wildcard[:] = [h for h in self._wildcard if h.name != name]

    async def emit(self, event: Event) -> List[Any]:
        """Fire all registered hooks for an event. Returns list of results."""
        results = []
        key = event.type.value

        all_hooks = list(self._wildcard)
        if key in self._hooks:
            all_hooks += self._hooks[key]

        all_hooks.sort(key=lambda r: r.priority)

        to_remove = []
        for reg in all_hooks:
            if not reg.enabled:
                continue
            try:
                if inspect.iscoroutinefunction(reg.handler):
                    result = await reg.handler(event)
                else:
                    result = reg.handler(event)
                results.append(result)
                if reg.once:
                    to_remove.append(reg)
                if not event.propagate:
                    logger.debug(f"Event propagation stopped at {reg.name}")
                    break
            except Exception as e:
                logger.error(f"Hook error [{reg.name}] on {key}: {e}")

        # clean one-shot hooks
        for reg in to_remove:
            self.off(reg.name)

        return results

    def emit_sync(self, event: Event) -> List[Any]:
        """Sync wrapper for emit."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return asyncio.ensure_future(self.emit(event))
            return loop.run_until_complete(self.emit(event))
        except RuntimeError:
            return asyncio.run(self.emit(event))

    def list_hooks(self) -> Dict[str, List[str]]:
        return {k: [h.name for h in hooks] for k, hooks in self._hooks.items()}


# Global hook bus
hooks = HookSystem()
