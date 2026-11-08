"""Costanti di base dell'integrazione.

Modulo "base": importabile da qualsiasi layer. Non deve importare homeassistant
né altri moduli del package (vedi tests/unit/test_architecture.py).
"""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "drp_climate_master"
INTEGRATION_NAME: Final = "DRP Climate Master"

LOGGER: Final = logging.getLogger(__package__)
