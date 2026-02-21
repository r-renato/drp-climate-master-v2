from __future__ import annotations

from typing import Protocol, Optional
from typing_extensions import runtime_checkable

@runtime_checkable
class SupplyPumpsDevice(Protocol):
    """Interfaccia per un device 'electrovalve' controllabile."""

    async def async_set_direct_power(self, *, power: Optional[bool] = None) -> None: ...
    async def async_set_adj_power(self, *, power: Optional[bool] = None) -> None: ...
    async def async_set_mix_adj_setpoints(self, *, value: Optional[float] = None) -> None: ...
