from __future__ import annotations

from typing import TypeAlias, TypedDict, Required

class Forecast(TypedDict, total=False):
    """Typed weather forecast dict (Home Assistant).

    Nota importante:
    - Per i metodi delle integrazioni (`WeatherEntity.async_forecast_*`) i campi
      canonici sono quelli `native_*` (valori in unità native). :contentReference[oaicite:6]{index=6}
    - I campi legacy senza prefisso `native_` possono esistere in layer di
      compatibilità/UX (es. risposta di `weather.get_forecasts` nei docs utente). :contentReference[oaicite:7]{index=7}
    """

    # --- Metadata (NON parte del set “Forecast data” documentato nei Dev Docs) ---
    failed: int | None
    """(Opzionale / non standard) Indicatore o contatore di errore legato a questa entry.
    Non è definito nello schema forecast dei Developer Docs; trattalo come metadata extra."""

    timezone: str | None
    """(Opzionale / non standard) Timezone associata ai dati (es. 'Europe/Rome').
    Non è definita nello schema forecast dei Developer Docs; preferisci sempre `datetime` in UTC."""

    units: str | None
    """(Opzionale / non standard) Descrittore di unità/sistema (es. 'metric', 'imperial').
    Non è definito nello schema forecast dei Developer Docs; trattalo come metadata extra."""

    # --- Forecast fields (schema Dev Docs) ---
    condition: str | None
    """Condizione meteo prevista (es. 'sunny', 'cloudy', ...). :contentReference[oaicite:8]{index=8}"""

    datetime: Required[str]
    """Timestamp della previsione in formato RFC3339, in UTC. :contentReference[oaicite:9]{index=9}"""

    humidity: float | None
    """Umidità relativa in percentuale (0-100). :contentReference[oaicite:10]{index=10}"""

    precipitation_probability: int | None
    """Probabilità di precipitazioni in percentuale (0-100). :contentReference[oaicite:11]{index=11}"""

    cloud_coverage: int | None
    """Copertura nuvolosa in percentuale (0-100). :contentReference[oaicite:12]{index=12}"""

    native_precipitation: float | None
    """Quantità di precipitazioni (unità native: mm o in). :contentReference[oaicite:13]{index=13}"""

    precipitation: None
    """DEPRECATO (legacy): in molti contesti “interni” non va usato.
    Usa `native_precipitation` per i metodi `async_forecast_*`."""

    native_pressure: float | None
    """Pressione atmosferica (unità native: hPa, mbar, inHg o mmHg). :contentReference[oaicite:14]{index=14}"""

    pressure: None
    """DEPRECATO (legacy): usa `native_pressure` nel layer integrazione."""

    native_temperature: float | None
    """Temperatura principale/alta (°C o °F) in unità native. :contentReference[oaicite:15]{index=15}"""

    temperature: None
    """DEPRECATO (legacy): usa `native_temperature` nel layer integrazione."""

    native_templow: float | None
    """Temperatura minima giornaliera (°C o °F) in unità native. :contentReference[oaicite:16]{index=16}"""

    templow: None
    """DEPRECATO (legacy): usa `native_templow` nel layer integrazione."""

    native_apparent_temperature: float | None
    """Temperatura percepita (feels-like) (°C o °F) in unità native. :contentReference[oaicite:17]{index=17}"""

    wind_bearing: float | str | None
    """Direzione del vento: gradi (azimuth) oppure cardinale (es. 'NW'). :contentReference[oaicite:18]{index=18}"""

    native_wind_gust_speed: float | None
    """Raffica di vento (unità native: Beaufort, m/s, km/h, mi/h, ft/s o kn). :contentReference[oaicite:19]{index=19}"""

    native_wind_speed: float | None
    """Velocità vento (unità native: Beaufort, m/s, km/h, mi/h, ft/s o kn). :contentReference[oaicite:20]{index=20}"""

    wind_speed: None
    """DEPRECATO (legacy): usa `native_wind_speed` nel layer integrazione."""

    native_dew_point: float | None
    """Punto di rugiada (°C o °F) in unità native. :contentReference[oaicite:21]{index=21}"""

    uv_index: float | None
    """Indice UV. :contentReference[oaicite:22]{index=22}"""

    is_daytime: bool | None
    """Obbligatorio per forecast `twice_daily`: True=giorno, False=notte. :contentReference[oaicite:23]{index=23}"""


Historical: TypeAlias = Forecast

