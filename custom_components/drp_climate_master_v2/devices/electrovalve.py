from __future__ import annotations

from typing import Protocol, Optional
from typing_extensions import runtime_checkable

@runtime_checkable
class ElectrovalveDevice(Protocol):
    """Interfaccia per un device 'electrovalve' controllabile."""

    async def async_set_circuit_open(self, *, area_name: Optional[str] = None, state: Optional[bool] = None) -> None: ...
