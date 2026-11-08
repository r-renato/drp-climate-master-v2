"""Test di architettura: fa rispettare i confini tra strati della pipeline.

Pipeline (docs/architecture.md):
    monitor -> signals/safety/comfort -> decision -> control -> drivers/HA
Piano di osservazione (pub/sub): bacheca dei segnali e bus di eventi (`bus`),
osservatori (`observers`) e adattatore meteo (`weather`) NON comandano l'impianto.

Regole applicate:
1. LAYER: un componente puo' importare solo da layer INFERIORI (o da se stesso
   o dai moduli BASE). Mai da layer superiori, mai da "fratelli" dello stesso
   layer (eccetto il layer 0, che e' orchestrazione/entry-point).
2. PURI: i componenti in PURE_COMPONENTS non importano mai `homeassistant`.
3. CLASSIFICAZIONE: ogni modulo/pacchetto di primo livello deve appartenere a
   un layer (o a BASE). Un componente nuovo senza layer fa fallire il test:
   la decisione architetturale va presa esplicitamente.
4. PIANO DI OSSERVAZIONE: i componenti in FORBIDDEN_IMPORTS (osservatori, meteo)
   non importano mai `control` o `drivers`: non possono emettere comandi e il
   veto del dew-guard resta in un percorso unico e lineare.

Il controllo e' fatto con `ast` (nessuna dipendenza esterna) e ignora i
componenti non ancora creati: funziona dallo step S0 in poi.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "drp_climate_master"
ABSOLUTE_PREFIX = f"custom_components.{PACKAGE}"
ROOT = Path(__file__).resolve().parents[2] / "custom_components" / PACKAGE

# Sottocomponenti: una sotto-cartella con layer e purezza propri (`weather/pure`).
SUBCOMPONENTS: dict[tuple[str, str], str] = {("weather", "pure"): "weather.pure"}


@dataclass(frozen=True, slots=True)
class Layer:
    """Un livello della pipeline: componenti fratelli e indipendenti."""

    name: str
    components: frozenset[str]
    allow_intra_layer: bool = False


# Dal piu' alto (index 0) al piu' basso. Si importa solo verso il basso.
LAYERS: tuple[Layer, ...] = (
    Layer(
        "orchestrazione",
        frozenset(
            {
                "coordinator",
                "supervisor",
                "climate",
                "select",
                "sensor",
                "switch",
                "diagnostics",
                "config_flow",
                "runtime",
                "weather",
                "observers",
            }
        ),
        allow_intra_layer=True,
    ),
    Layer("attuazione", frozenset({"control"})),
    Layer("decisione-e-hw", frozenset({"decision", "drivers"})),
    Layer("segnali-e-guardrail", frozenset({"safety", "signals", "comfort", "weather.pure"})),
    Layer("osservazione", frozenset({"monitor"})),
    Layer("modelli-e-configurazione", frozenset({"profiles", "config"})),
    Layer("fondamenta", frozenset({"domain", "physics", "bus"})),
)

# Importabili da qualsiasi layer; non devono dipendere da nessun componente.
BASE: frozenset[str] = frozenset({"const", "log"})

# Logica pura: nessun import di homeassistant, funzioni testabili senza `hass`.
PURE_COMPONENTS: frozenset[str] = frozenset(
    {"physics", "signals", "safety", "comfort", "profiles", "decision", "domain", "bus", "weather.pure"}
)

# Piano di osservazione: questi componenti non possono importare l'attuazione.
FORBIDDEN_IMPORTS: dict[str, frozenset[str]] = {
    "observers": frozenset({"control", "drivers"}),
    "weather": frozenset({"control", "drivers"}),
}

_LAYER_OF: dict[str, int] = {c: i for i, layer in enumerate(LAYERS) for c in layer.components}


@dataclass(frozen=True, slots=True)
class ImportRef:
    """Un import risolto: `component` e' il componente interno (None se esterno)."""

    external: str | None
    component: str | None
    line: int


def component_of(path: Path, root: Path = ROOT) -> str:
    """Componente di primo livello di un file (nome pacchetto o nome modulo)."""
    rel = path.relative_to(root)
    first = rel.parts[0]
    if len(rel.parts) > 2 and (first, rel.parts[1]) in SUBCOMPONENTS:
        return SUBCOMPONENTS[(first, rel.parts[1])]
    return first[:-3] if first.endswith(".py") else first


def _component_of_parts(parts: list[str]) -> str:
    """Componente di un import risolto in parti (gestisce i sottocomponenti)."""
    if len(parts) > 1 and (parts[0], parts[1]) in SUBCOMPONENTS:
        return SUBCOMPONENTS[(parts[0], parts[1])]
    return parts[0]


def collect_imports(source: str, pkg_parts: tuple[str, ...]) -> list[ImportRef]:
    """Estrae gli import di un modulo e li risolve rispetto al package.

    `pkg_parts` = percorso (relativo a ROOT) del pacchetto che contiene il modulo,
    es. () per `climate.py`, ("signals",) per `signals/builder.py`.
    """
    refs: list[ImportRef] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                refs.append(_resolve_absolute(alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                refs.append(_resolve_absolute(node.module or "", node.lineno))
                continue
            up = node.level - 1
            if up > len(pkg_parts):
                continue  # esce dal package: non interno, ignorato
            base = list(pkg_parts[: len(pkg_parts) - up])
            if node.module:
                target = base + node.module.split(".")
                refs.append(ImportRef(None, _component_of_parts(target), node.lineno))
            elif base:
                for alias in node.names:
                    refs.append(ImportRef(None, _component_of_parts([*base, alias.name]), node.lineno))
            else:  # `from . import x`, `from .. import x` a livello root
                for alias in node.names:
                    refs.append(ImportRef(None, alias.name, node.lineno))
    return refs


def _resolve_absolute(dotted: str, line: int) -> ImportRef:
    if dotted == ABSOLUTE_PREFIX or dotted.startswith(ABSOLUTE_PREFIX + "."):
        rest = dotted[len(ABSOLUTE_PREFIX) :].lstrip(".")
        return ImportRef(None, _component_of_parts(rest.split(".")) if rest else None, line)
    return ImportRef(dotted.split(".")[0], None, line)


def layer_violations(component: str, refs: list[ImportRef]) -> list[str]:
    """Violazioni della regola 1 per un file appartenente a `component`."""
    if component in BASE or component not in _LAYER_OF:
        return []
    own = _LAYER_OF[component]
    out: list[str] = []
    for ref in refs:
        target = ref.component
        if target is None or target == component or target in BASE or target not in _LAYER_OF:
            continue
        tgt = _LAYER_OF[target]
        if tgt > own:
            continue  # verso il basso: ok
        if tgt == own and LAYERS[own].allow_intra_layer:
            continue
        direction = "layer superiore" if tgt < own else "componente fratello"
        out.append(f"riga {ref.line}: {component} -> {target} ({direction})")
    return out


def forbidden_violations(component: str, refs: list[ImportRef]) -> list[str]:
    """Violazioni della regola 4: osservatori/meteo che importano l'attuazione."""
    banned = FORBIDDEN_IMPORTS.get(component, frozenset())
    return [f"riga {r.line}: {component} importa {r.component} (vietato)" for r in refs if r.component in banned]


def purity_violations(component: str, refs: list[ImportRef]) -> list[str]:
    """Violazioni della regola 2: componenti puri che importano homeassistant."""
    if component not in PURE_COMPONENTS:
        return []
    return [f"riga {r.line}: {component} importa homeassistant" for r in refs if r.external == "homeassistant"]


def _py_files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _pkg_parts(path: Path) -> tuple[str, ...]:
    return path.relative_to(ROOT).parts[:-1]


# --------------------------------------------------------------------------
# Test sull'albero reale
# --------------------------------------------------------------------------


def test_layers_are_respected() -> None:
    problems: list[str] = []
    for path in _py_files():
        if path == ROOT / "__init__.py":
            continue
        refs = collect_imports(path.read_text(encoding="utf-8"), _pkg_parts(path))
        for v in layer_violations(component_of(path), refs):
            problems.append(f"{path.relative_to(ROOT)}: {v}")
    assert not problems, "Violazioni di layer:\n" + "\n".join(problems)


def test_pure_components_do_not_import_homeassistant() -> None:
    problems: list[str] = []
    for path in _py_files():
        refs = collect_imports(path.read_text(encoding="utf-8"), _pkg_parts(path))
        for v in purity_violations(component_of(path), refs):
            problems.append(f"{path.relative_to(ROOT)}: {v}")
    assert not problems, "Layer puri con import HA:\n" + "\n".join(problems)


def test_observation_plane_does_not_import_actuation() -> None:
    problems: list[str] = []
    for path in _py_files():
        refs = collect_imports(path.read_text(encoding="utf-8"), _pkg_parts(path))
        for v in forbidden_violations(component_of(path), refs):
            problems.append(f"{path.relative_to(ROOT)}: {v}")
    assert not problems, "Osservatori/meteo con import vietati:\n" + "\n".join(problems)


def test_every_top_level_component_is_classified() -> None:
    known = set(_LAYER_OF) | BASE | {"__init__"}
    found = {component_of(p) for p in _py_files()}
    unknown = sorted(found - known)
    assert not unknown, f"Componenti senza layer (aggiornare LAYERS/BASE): {unknown}"


def test_base_modules_do_not_depend_on_components() -> None:
    problems: list[str] = []
    for path in _py_files():
        if component_of(path) not in BASE:
            continue
        refs = collect_imports(path.read_text(encoding="utf-8"), _pkg_parts(path))
        problems += [
            f"{path.relative_to(ROOT)}: riga {r.line} importa {r.component or r.external}"
            for r in refs
            if r.component is not None or r.external == "homeassistant"
        ]
    assert not problems, "I moduli BASE devono essere indipendenti:\n" + "\n".join(problems)


def test_layer_table_is_consistent() -> None:
    names = [c for layer in LAYERS for c in layer.components]
    assert len(names) == len(set(names)), "componente in piu' layer"
    assert not (set(names) & BASE)
    assert set(names) >= PURE_COMPONENTS


# --------------------------------------------------------------------------
# Auto-test del checker (prova che le violazioni vengono davvero rilevate)
# --------------------------------------------------------------------------


def _check(component: str, source: str, pkg: tuple[str, ...]) -> list[str]:
    refs = collect_imports(source, pkg)
    return (
        layer_violations(component, refs) + purity_violations(component, refs) + forbidden_violations(component, refs)
    )


def test_checker_flags_upward_import() -> None:
    src = "from ..control.sequencer import run\n"
    assert _check("signals", src, ("signals",))


def test_checker_flags_sibling_import() -> None:
    src = "from ..signals import builder\n"
    assert _check("safety", src, ("safety",))


def test_checker_allows_downward_import() -> None:
    src = "from ..monitor.model import PlantSnapshot\nfrom ..physics.psychrometrics import dew_point\n"
    assert _check("signals", src, ("signals",)) == []


def test_checker_flags_homeassistant_in_pure_layer() -> None:
    assert _check("decision", "from homeassistant.core import HomeAssistant\n", ("decision",))
    assert _check("decision", "import homeassistant.util.dt as dt\n", ("decision",))


def test_checker_allows_homeassistant_in_drivers() -> None:
    assert _check("drivers", "from homeassistant.core import HomeAssistant\n", ("drivers",)) == []


def test_checker_resolves_absolute_imports() -> None:
    src = f"from {ABSOLUTE_PREFIX}.control import fsm\n"
    assert _check("signals", src, ("signals",))


def test_checker_resolves_root_level_relative_imports() -> None:
    # climate.py (layer 0) puo' importare control; physics (fondamenta) no.
    assert _check("climate", "from . import control\n", ()) == []
    assert _check("physics", "from . import climate\n", ())


def test_checker_allows_base_and_own_component() -> None:
    src = "from ..const import DOMAIN\nfrom .model import X\n"
    assert _check("signals", src, ("signals",)) == []


def test_orchestration_layer_allows_intra_layer_imports() -> None:
    assert _check("climate", "from .coordinator import Coordinator\n", ()) == []


def test_checker_flags_observer_importing_control() -> None:
    assert _check("observers", "from ..control import fsm\n", ("observers",))
    assert _check("observers", "from ..drivers import hmi080\n", ("observers",))


def test_checker_allows_observer_importing_bus_and_decision_types() -> None:
    assert (
        _check(
            "observers", "from ..bus import EventBus\nfrom ..decision.contracts import PlantDecision\n", ("observers",)
        )
        == []
    )


def test_weather_adapter_cannot_import_actuation() -> None:
    assert _check("weather", "from ..control import fsm\n", ("weather",))


def test_weather_pure_is_a_pure_subcomponent() -> None:
    assert _check("weather.pure", "from homeassistant.core import HomeAssistant\n", ("weather", "pure"))
    # l'adattatore HA puo' importare la parte pura (verso il basso)
    assert _check("weather", "from .pure import rmot\n", ("weather",)) == []
    # la parte pura non puo' importare l'adattatore
    assert _check("weather.pure", "from .. import adapter\n", ("weather", "pure"))


def test_subcomponent_resolution() -> None:
    assert component_of(ROOT / "weather" / "pure" / "rmot.py") == "weather.pure"
    assert component_of(ROOT / "weather" / "adapter.py") == "weather"
    assert component_of(ROOT / "bus" / "board.py") == "bus"
