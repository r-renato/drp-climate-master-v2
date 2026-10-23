"""outdoor_temperature.py — Temperatura esterna per il modello RC del planner zone.

Funzione pura (nessun I/O, nessuna dipendenza da Home Assistant a runtime):
sceglie la sorgente della temperatura esterna con degradazione controllata e
dichiara quale sorgente è stata usata, così il log di commissioning può
spiegarla (``t_out_mpc_src``).
"""
from __future__ import annotations

from typing import Any, Final

from ....helpers.utils import as_float

OUTDOOR_SRC_SENSOR: Final[str] = "global_outdoor_temperature"
"""Sensore istantaneo aggregato, fresco e sufficiente: sorgente preferita."""

OUTDOOR_SRC_SENSOR_STALE: Final[str] = "global_outdoor_temperature_stale"
"""Ultima lettura del sensore aggregato, ma stale/insufficiente."""

OUTDOOR_SRC_FORECAST_MEAN: Final[str] = "season.weather.weather_day_signals.t_mean"
"""Media giornaliera di oggi dal forecast meteo (sensore del tutto assente)."""

OUTDOOR_SRC_MISSING: Final[str] = "missing"
"""Nessuna sorgente disponibile: il piano MPC va abortito."""


def resolve_outdoor_temperature(snapshot: Any) -> tuple[float | None, str]:
    """Temperatura esterna per la predizione RC e sorgente usata.

    Ordine di priorità (degradazione controllata):

    1. ``global_outdoor_temperature`` fresca e sufficiente: sorgente corretta
       per l'orizzonte breve del modello RC (es. 12 passi da 10 min).
    2. ``global_outdoor_temperature`` stale/insufficiente ma con un valore: di
       solito vecchia di minuti, quindi più rappresentativa di una media del giorno.
    3. Media giornaliera ``t_mean`` del forecast (``season.weather``): solo se il
       sensore non ha alcun valore. Mai ``t_smooth`` (media mobile di settimane):
       con T esterna "di maggio" a inizio inverno la deriva predetta sarebbe
       quasi nulla e il piano incoerente con la realtà istantanea.
    4. ``(None, "missing")``: il chiamante deve abortire il piano.

    Nota: finché ``t_mean`` non è la media vera (tmax+tmin)/2 ma la sola Tmax, la
    sorgente 3 è polarizzata verso il caldo; per questo resta ultima risorsa.
    """
    av = getattr(snapshot, "global_outdoor_temperature", None)
    sensor_value = as_float(getattr(av, "value", None)) if av is not None else None

    if (
        sensor_value is not None
        and not getattr(av, "is_stale", False)
        and not getattr(av, "is_insufficient", False)
    ):
        return float(sensor_value), OUTDOOR_SRC_SENSOR

    if sensor_value is not None:
        return float(sensor_value), OUTDOOR_SRC_SENSOR_STALE

    # SeasonState espone i segnali meteo in ``weather.weather_day_signals``.
    season = getattr(snapshot, "season", None)
    weather = getattr(season, "weather", None)
    signals = getattr(weather, "weather_day_signals", None)
    t_mean = as_float(getattr(signals, "t_mean", None))
    if t_mean is not None:
        return float(t_mean), OUTDOOR_SRC_FORECAST_MEAN

    return None, OUTDOOR_SRC_MISSING
