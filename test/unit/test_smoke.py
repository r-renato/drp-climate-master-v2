"""Smoke test S0: package importabile e manifest coerente."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.drp_climate_master_v3 import const

PACKAGE_DIR = Path(const.__file__).parent
MANIFEST = json.loads((PACKAGE_DIR / "manifest.json").read_text(encoding="utf-8"))

# Chiavi ammesse dal manifest HA (sottoinsieme rilevante). Una chiave fuori
# lista e' quasi sempre un errore (la v2 aveva le chiavi spurie "r" e "rr").
ALLOWED_KEYS = {
    "domain",
    "name",
    "codeowners",
    "config_flow",
    "documentation",
    "integration_type",
    "iot_class",
    "issue_tracker",
    "requirements",
    "version",
    "dependencies",
    "after_dependencies",
    "single_config_entry",
    "loggers",
    "quality_scale",
}
REQUIRED_KEYS = {"domain", "name", "codeowners", "documentation", "version"}


def test_domain_matches_manifest() -> None:
    assert MANIFEST["domain"] == const.DOMAIN == PACKAGE_DIR.name


def test_manifest_has_no_stray_keys() -> None:
    assert set(MANIFEST) <= ALLOWED_KEYS, set(MANIFEST) - ALLOWED_KEYS


def test_manifest_has_required_keys() -> None:
    assert set(MANIFEST) >= REQUIRED_KEYS


def test_iot_class_is_local() -> None:
    """Impianto locale: mai cloud_* (errore presente nel manifest v2)."""
    assert not str(MANIFEST["iot_class"]).startswith("cloud")


def test_v3_coexists_with_v2() -> None:
    assert const.DOMAIN != "drp_climate_master_v2"
