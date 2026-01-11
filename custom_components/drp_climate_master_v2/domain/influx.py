from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class InfluxConfig:
    org: str
    token: str
    bucket: str
    url: str
    