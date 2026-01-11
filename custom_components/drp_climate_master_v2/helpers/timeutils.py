# custom_components/drp_climate/domain/timeutils.py
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo
from typing import Any, Iterable, Optional, Tuple

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

__all__ = [
    "UTC",
    "now_utc",
    "ensure_tz",
    "to_utc",
    "to_tz",
    "round_dt",
    "next_tick",
    "seconds_since",
    "humanize_timedelta",
    "parse_duration",
    "within_time_window",
    "window_contains",
    "rate_next_allowed",
    "rate_is_allowed",
]

UTC = timezone.utc


# ────────────────────────────── Clock & TZ ──────────────────────────────
def ha_timezone(hass: HomeAssistant) -> Tuple[Optional[str], tzinfo]:
    """Restituisce il nome e l'oggetto tzinfo della timezone di Home Assistant."""
    tz_name: str | None = hass.config.time_zone          # es. "Europe/Rome"
    tzinfo: tzinfo = dt_util.get_time_zone(tz_name) or dt_util.UTC if tz_name else dt_util.UTC
    return tz_name, tzinfo

def now_utc() -> datetime:
    """Datetime-aware in UTC."""
    return datetime.now(UTC)

def now_tz(tz: tzinfo) -> datetime:
    """Datetime-aware in tz."""
    return datetime.now(tz)

def ensure_tz(dt: datetime, tz: tzinfo) -> datetime:
    """
    Se dt è naive, assume che sia già nella timezone tz (no conversion),
    altrimenti converte alla timezone tz.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)

def to_utc(dt: datetime) -> datetime:
    """Converte (o marca) dt in UTC."""
    return ensure_tz(dt, UTC).astimezone(UTC)

def to_tz(dt: datetime, tz: tzinfo | str) -> datetime:
    """Converte dt in una timezone (accetta tzinfo o stringa IANA)."""
    tzinfo = ZoneInfo(tz) if isinstance(tz, str) else tz
    return ensure_tz(dt, tzinfo)

def to_local_date(d: Optional[date | datetime]) -> Optional[date]:
    """Converte un input date/datetime in date.

    Non applica conversioni di timezone: in HA, passa un datetime già in timezone HA.
    - date -> date
    - datetime -> datetime.date()
    - None -> None
    """
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    return d
    
def as_iso_local(dt_like: Any) -> Optional[str]:
    """
    Converte un input (str/datetime) in ISO 8601 **locale** (stringa) per il campo Forecast['datetime'].
    - Se è str: prova a parse con HA (gestisce anche timezone); se è data-only (YYYY-MM-DD) crea mezzanotte locale.
    - Se è datetime: lo porta in timezone locale e lo serializza ISO.
    """
    if isinstance(dt_like, datetime):
        return dt_util.as_local(dt_like).isoformat()

    # attenzione: datetime è anche un date, quindi questo va dopo il check datetime
    if isinstance(dt_like, date):
        return dt_util.start_of_local_day(dt_like).isoformat()

    if isinstance(dt_like, str):
        dt = dt_util.parse_datetime(dt_like)
        if dt is not None:
            return dt_util.as_local(dt).isoformat()

        # formato data "YYYY-MM-DD"
        try:
            y, m, d = map(int, dt_like.split("-"))
            return dt_util.start_of_local_day(date(y, m, d)).isoformat()
        except Exception:
            return None

    return None

# ────────────────────────────── Rounding & ticks ──────────────────────────────

def round_dt(dt: datetime, *, delta: timedelta, method: str = "floor") -> datetime:
    """
    Arrotonda dt al multiplo di 'delta' rispetto all'epoch UTC.
    method ∈ {'floor','ceil','nearest'}.
    """
    if delta <= timedelta(0):
        raise ValueError("delta must be positive")

    # usa epoch UTC per evitare problemi di DST
    ts = to_utc(dt).timestamp()
    q = delta.total_seconds()
    if method == "floor":
        new = ts - (ts % q)
    elif method == "ceil":
        new = ts if (ts % q) == 0 else ts + (q - (ts % q))
    elif method == "nearest":
        new = round(ts / q) * q
    else:
        raise ValueError("method must be 'floor', 'ceil', or 'nearest'")
    return datetime.fromtimestamp(new, tz=UTC).astimezone(dt.tzinfo or UTC)


def next_tick(dt: datetime, interval: timedelta) -> datetime:
    """Restituisce il prossimo bordo di 'interval' ≥ dt."""
    return round_dt(dt + timedelta(microseconds=1), delta=interval, method="ceil")


# ────────────────────────────── Diffs & humanize ──────────────────────────────

def seconds_since(past: Optional[datetime], *, now: Optional[datetime] = None) -> float:
    """Secondi trascorsi da 'past' (aware o naive). Se past è None → +inf (usato per 'sempre consentito')."""
    if past is None:
        return float("inf")
    _now = now or now_utc()
    a = to_utc(_now)
    b = to_utc(past)
    return (a - b).total_seconds()


def humanize_timedelta(td: timedelta, *, max_parts: int = 3) -> str:
    """
    Rende una stringa compatta per td, es. 1h 5m 3s.
    max_parts limita il numero di componenti non zero.
    """
    total = int(abs(td.total_seconds()))
    sign = "-" if td.total_seconds() < 0 else ""
    parts: list[str] = []
    for unit, secs in (("w", 7 * 86400), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if total >= secs:
            value, total = divmod(total, secs)
            parts.append(f"{value}{unit}")
        if len(parts) >= max_parts:
            break
    return sign + (" ".join(parts) if parts else "0s")


# ────────────────────────────── Durations parsing ──────────────────────────────

_ISO_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)

def parse_duration(text: str) -> timedelta:
    """
    Converte stringhe di durata in timedelta.
    Supporta:
      - Token: '90m', '1h30m', '2d4h', '45s', '1w2d'
      - Orario: 'HH:MM' o 'HH:MM:SS'
      - ISO-8601 parziale: 'PT1H30M', 'P2DT3H'
    """
    s = (text or "").strip().lower()
    if not s:
        raise ValueError("empty duration")

    # HH:MM(:SS)
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        hh, mm, *rest = s.split(":")
        ss = rest[0] if rest else "0"
        return timedelta(hours=int(hh), minutes=int(mm), seconds=int(ss))

    # ISO 8601 PnDTnHnMnS
    m = _ISO_RE.match(s)
    if m:
        days = int(m.group("days") or 0)
        hours = int(m.group("hours") or 0)
        minutes = int(m.group("minutes") or 0)
        seconds = float(m.group("seconds") or 0)
        return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)

    # Token liberi (es. 1h30m, 2d, 45s, 1w2d)
    total = timedelta(0)
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)([wdhms])", s):
        found = True
        value = float(num)
        if unit == "w":
            total += timedelta(days=7 * value)
        elif unit == "d":
            total += timedelta(days=value)
        elif unit == "h":
            total += timedelta(hours=value)
        elif unit == "m":
            total += timedelta(minutes=value)
        elif unit == "s":
            total += timedelta(seconds=value)
    if found:
        return total

    raise ValueError(f"cannot parse duration: {text!r}")


# ────────────────────────────── Daily windows ──────────────────────────────

def within_time_window(
    ts: datetime,
    start: time,
    end: time,
    *,
    tz: tzinfo | str | None = None,
    inclusive_end: bool = False,
) -> bool:
    """
    True se l'istante 'ts' cade nella finestra [start, end) locale al giorno di ts.
    Se end <= start → finestra notturna (attraversa la mezzanotte).
    """
    tzinfo = ZoneInfo(tz) if isinstance(tz, str) else (tz or ts.tzinfo or UTC)
    local = ts.astimezone(tzinfo)

    s = datetime.combine(local.date(), start, tzinfo)
    e = datetime.combine(local.date(), end, tzinfo)

    if e <= s:  # overnight (es. 22:00 → 06:00)
        in_first = s <= local if inclusive_end else s <= local < s.replace(hour=23, minute=59, second=59, microsecond=999999)
        # Comodo: valuta anche la porzione dopo mezzanotte
        e_next = e + timedelta(days=1)
        return local >= s or local < e
    # finestra diurna
    return (s <= local <= e) if inclusive_end else (s <= local < e)


def window_contains(
    ts: datetime,
    windows: Iterable[Tuple[time, time]],
    *,
    tz: tzinfo | str | None = None,
    inclusive_end: bool = False,
) -> bool:
    """True se 'ts' cade in almeno una finestra della lista."""
    return any(
        within_time_window(ts, s, e, tz=tz, inclusive_end=inclusive_end)
        for (s, e) in windows
    )


# ────────────────────────────── Rate limit / debounce ──────────────────────────────

def rate_next_allowed(
    last: Optional[datetime],
    min_interval: timedelta,
    *,
    now: Optional[datetime] = None,
) -> datetime:
    """
    Prossimo istante nel quale un'azione è consentita, dato l'ultimo evento 'last'
    e l'intervallo minimo 'min_interval'.
    """
    _now = now or now_utc()
    if last is None:
        return _now
    return to_utc(last) + min_interval


def rate_is_allowed(
    last: Optional[datetime],
    min_interval: timedelta,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """True se ora ≥ (last + min_interval)."""
    _now = now or now_utc()
    return to_utc(_now) >= rate_next_allowed(last, min_interval, now=_now)
