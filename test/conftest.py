"""Fixture globali.

Il plugin `pytest-homeassistant-custom-component` fornisce la fixture `hass`.
Viene caricato se installato; i test puri (unit/) non ne hanno bisogno e
girano anche senza.
"""

from __future__ import annotations

import importlib.util

import pytest

_HA_PLUGIN = "pytest_homeassistant_custom_component"
HAS_HA_PLUGIN = importlib.util.find_spec(_HA_PLUGIN) is not None

pytest_plugins = [_HA_PLUGIN] if HAS_HA_PLUGIN else []

if HAS_HA_PLUGIN:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):  # type: ignore[no-untyped-def]
        """Abilita il caricamento di custom_components/ nei test con `hass`."""
        yield
