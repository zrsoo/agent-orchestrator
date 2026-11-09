"""No provider-specific values are required by the scheduler."""
from dataclasses import dataclass, field
from typing import Protocol, Callable


@dataclass
class Bucket:
    name: str
    remaining: float | None = None
    window_minutes: int | None = None
    resets_at: float | None = None
    minimum: float = 0


@dataclass
class Quota:
    buckets: list[Bucket] = field(default_factory=list)
    allowed: bool = True


@dataclass
class Outcome:
    status: str  # completed, failed, quota, interrupted, blocked
    text: str = ''
    diagnostic: str = ''


class TransportError(RuntimeError):
    pass


class InputSizeError(ValueError):
    """An unchanged input cannot succeed by retrying the transport."""


class QuotaExhausted(RuntimeError):
    pass


class AgentBackend(Protocol):
    def quota(self) -> Quota: ...
    def session(self, workspace: str, session_id: str | None) -> str: ...
    def run(self, session_id: str, prompt: str, emit: Callable, control: Callable) -> Outcome: ...
    def interrupt(self, session_id: str, turn_id: str): ...
    def close(self): ...
