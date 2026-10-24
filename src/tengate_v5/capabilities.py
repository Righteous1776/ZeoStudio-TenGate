from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .errors import UnsupportedCapability


@dataclass(frozen=True, slots=True)
class CapabilityPrism:
    fetch_pending: bool = False
    dispatch: bool = False
    acknowledge: bool = False
    post_evidence: bool = False
    verify_webhook: bool = False
    resolve_identity: bool = False
    health: bool = True


class GateAdapter(Protocol):
    def capabilities(self) -> CapabilityPrism: ...
    def health(self) -> dict[str, str]: ...


def require_capability(adapter: GateAdapter, name: str) -> None:
    prism = adapter.capabilities()
    if not hasattr(prism, name) or not getattr(prism, name):
        raise UnsupportedCapability(f"adapter capability unavailable: {name}")

