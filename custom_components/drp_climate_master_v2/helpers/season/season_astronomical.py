from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from skyfield import almanac
from skyfield.api import Loader

SEASON_NAMES = {
    0: "SPRING",
    1: "SUMMER",
    2: "AUTUMN",
    3: "WINTER",
}

@dataclass(frozen=True)
class AstroSeasonWindow:
    season: str                 # "SPRING" | "SUMMER" | "AUTUMN" | "WINTER"
    start: datetime             # timezone-aware in Europe/Rome
    end: datetime               # timezone-aware in Europe/Rome (fine esclusiva)

def astronomical_season_for(dt: datetime, *, tz_name: str = "Europe/Rome") -> AstroSeasonWindow:
    """
    Ritorna la stagione astronomica corrente (emisfero nord) e la finestra [start, end)
    in timezone locale (Europe/Rome). end è esclusiva.
    """
    tz = ZoneInfo(tz_name)

    # Normalizza dt in timezone locale
    if dt.tzinfo is None:
        dt_local = dt.replace(tzinfo=tz)
    else:
        dt_local = dt.astimezone(tz)

    dt_utc = dt_local.astimezone(timezone.utc)

    # In Home Assistant spesso conviene usare una cartella persistente tipo /config/skyfield
    load = Loader("./skyfield_data")
    ts = load.timescale()
    eph = load("de421.bsp")  # verrà scaricato se manca (serve accesso internet alla prima esecuzione)

    f = almanac.seasons(eph)

    y = dt_local.year
    # Range abbastanza largo per includere l'evento precedente e quello successivo
    t0 = ts.utc(y - 1, 12, 1)
    t1 = ts.utc(y + 1, 4, 1)

    t, events = almanac.find_discrete(t0, t1, f)

    # Converti i tempi Skyfield in datetime UTC
    t_utc = [ti.utc_datetime().replace(tzinfo=timezone.utc) for ti in t]

    # Trova l'intervallo [t_i, t_{i+1}) che contiene dt_utc
    idx = None
    for i in range(len(t_utc) - 1):
        if t_utc[i] <= dt_utc < t_utc[i + 1]:
            idx = i
            break

    if idx is None:
        raise RuntimeError("Intervallo stagione non trovato: estendi t0/t1.")

    season_idx = int(events[idx])
    start_utc = t_utc[idx]
    end_utc = t_utc[idx + 1]

    return AstroSeasonWindow(
        season=SEASON_NAMES[season_idx],
        start=start_utc.astimezone(tz),
        end=end_utc.astimezone(tz),
    )

if __name__ == "__main__":
    now = datetime.now(ZoneInfo("Europe/Rome"))
    w = astronomical_season_for(now)
    print(f"Stagione astronomica: {w.season}")
    print(f"Da: {w.start.isoformat()}")
    print(f"A:  {w.end.isoformat()} (fine esclusiva)")
