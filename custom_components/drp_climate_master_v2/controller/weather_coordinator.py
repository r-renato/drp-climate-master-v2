"""weather_coordinator.py — Orchestratore meteo: thin facade verso HA.

Responsabilità residua dopo il refactoring:
  1. Lifecycle HA (startup, shutdown, scheduling giornaliero).
  2. Orchestrazione HistoricalFetcher + SeasonDetector.
  3. Propagazione SeasonState verso ClimateCoordinator.
  4. Bridge HVAC decider (debounced).

NON contiene più logica di fetch né di season detection:
  vedere historical_fetcher.py e season_detector.py.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Callable, Optional

from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.util import dt as dt_util
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

from .coordinator import ClimateCoordinator
from .weather.historical_fetcher import HistoricalFetcher, HistoricalFetcherConfig
from .weather.season_detector import SeasonDetector, SeasonDetectorConfig
from ..domain.models.season import SeasonState
from ..helpers.logger import log_debug, log_exception, log_info, log_warning
from ..helpers.weather.pirateweather_client import (
    PirateWeatherConfig,
    PirateWeatherTimeMachineClient,
    ProviderOptions,
)
from ..helpers.scheduler import IntervalGatedSchedulerBase, ThrottledAsyncJob
from ..const import DOMAIN, SEASON_STATE

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class WeatherCoordinatorConfig:
    """Runtime config per WeatherCoordinator.

    I parametri di fetch e detection sono delegati alle rispettive dataclass;
    qui rimangono solo i parametri di scheduling e bridge HVAC.
    """

    # Gate giornaliero (persisted across restarts)
    daily_interval: timedelta = timedelta(hours=24)

    # Throttle season detection (run costoso: modello ML + fit)
    season_detect_min_interval: timedelta = timedelta(minutes=30)

    # Debounce decider HVAC (previene feedback loop)
    decider_debounce_cooldown_s: float = 20.0

    # --- Parametri fetch (delegati a HistoricalFetcherConfig) ---
    days_back: int = 730
    always_refresh_today: bool = True
    cache_persist: bool = True
    cache_max_days: int = 740
    cache_save_cooldown_s: float = 60.0

    # DEPRECATO — mantenuto per compatibilità configurazione esistente, non usato.
    # Rimosso dal path attivo: il tick è point-in-time, non periodico.
    # update_interval: timedelta = timedelta(hours=6)


class WeatherCoordinator(IntervalGatedSchedulerBase):
    """Orchestratore meteo: lifecycle HA + scheduling + propagazione SeasonState.

    Dopo il refactoring è un thin facade: la logica di fetch è in
    HistoricalFetcher, quella di season detection in SeasonDetector.

    Fix applicati rispetto alla versione precedente:
      - Bug #2: cache non più inquinata da dati forecast (storico passato
        come copia esplicita a SeasonDetector).
      - Bug #1: is_healthy esposto da HistoricalFetcher; failure refresh non
        più silente verso il layer FSM.
      - Bug #3 (teardown): _unsub_coordinator rimosso PRIMA di super().async_stop().
      - Bug #4 (double write): unico punto di scrittura SeasonState via
        _propagate_season().
      - Bug #5 (cast senza isinstance): validazione runtime spostata in
        SeasonDetector._get_forecast_normalized().
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ClimateCoordinator,
        wc_cfg: WeatherCoordinatorConfig | None = None,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator

        self._entry_id = coordinator.entry_id
        self._unit_system = coordinator.unit_system
        self._runtime_weather_config = coordinator.runtime_weather_config

        self._wc_cfg = wc_cfg or WeatherCoordinatorConfig()

        # --- PirateWeather client ---
        pw_cfg = PirateWeatherConfig(
            lat=self._runtime_weather_config.historical_data.latitude,
            lon=self._runtime_weather_config.historical_data.longitude,
            api_key=self._runtime_weather_config.historical_data.token,
            units=self._unit_system,
        )
        pw_opts = ProviderOptions()
        pw_client = PirateWeatherTimeMachineClient.from_hass(hass, pw_cfg, pw_opts)

        # Cache key (identica alla versione precedente: compatibilità cache on-disk)
        cache_key = (
            f"{DOMAIN}.pirateweather.historical."
            f"{self._unit_system}."
            f"{self._runtime_weather_config.historical_data.latitude:.4f}."
            f"{self._runtime_weather_config.historical_data.longitude:.4f}"
        )

        # --- HistoricalFetcher ---
        fetcher_cfg = HistoricalFetcherConfig(
            days_back=self._wc_cfg.days_back,
            always_refresh_today=self._wc_cfg.always_refresh_today,
            cache_persist=self._wc_cfg.cache_persist,
            cache_max_days=self._wc_cfg.cache_max_days,
            cache_save_cooldown_s=self._wc_cfg.cache_save_cooldown_s,
        )
        self._fetcher = HistoricalFetcher(hass, pw_client, cache_key, fetcher_cfg)

        # --- SeasonDetector ---
        detector_cfg = SeasonDetectorConfig(
            forecast_provider=self._runtime_weather_config.forecast_data.provider,
        )
        self._detector = SeasonDetector(hass, detector_cfg)

        # --- Base scheduler (daily gating + point-in-time tick) ---
        super().__init__(
            hass,
            store_key=cache_key,
            gate_interval=self._wc_cfg.daily_interval,
            tick_interval=timedelta(hours=6),  # usato solo dalla base per next-tick calc
            logger=_LOGGER,
        )

        self._unsub_coordinator: Callable[[], None] | None = None

        # Season detect (throttle 30 min)
        self._season_job = ThrottledAsyncJob(
            hass,
            min_interval=self._wc_cfg.season_detect_min_interval,
            name_prefix="drp_season_detect",
            logger=_LOGGER,
        )

        # # Decider HVAC (debounce 20 s — rompe feedback loop)
        # self._decider_debouncer = Debouncer(
        #     hass,
        #     _LOGGER,
        #     cooldown=self._wc_cfg.decider_debounce_cooldown_s,
        #     immediate=False,
        #     function=self._decide_and_act,
        # )

        self._unsub_hastarted: Optional[Callable[[], None]] = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, self._on_ha_started
        )

        log_info(
            _LOGGER,
            "Inizializzato (id=%s) entry=%s unit=%s daily_interval=%s",
            hex(id(self)),
            self._entry_id,
            self._unit_system,
            self._wc_cfg.daily_interval,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @callback
    def _on_ha_started(self, event: Event) -> None:
        self._hass.async_create_task(self._async_first_refresh(event))

    async def _async_first_refresh(self, event: Event) -> None:
        await self._async_start()
        await self.async_refresh_weather_daily(reason="startup")
        self._schedule_season_detect("startup")
        log_info(_LOGGER, "Startup completato.")

    async def _async_start(self) -> None:
        await self._fetcher.async_load()

        # Registra listener coordinator DOPO il caricamento cache.
        # (Evita callback durante l'inizializzazione.)
        if self._unsub_coordinator is None:
            self._unsub_coordinator = self._coordinator.async_add_listener(
                self._on_coordinator_update
            )

        await super().async_start()

    async def async_stop(self) -> None:
        # FIX Bug #3: rimuovi il listener PRIMA di fermare lo scheduler base,
        # così nessun callback può schedular nuovi task durante il teardown.
        if self._unsub_coordinator is not None:
            self._unsub_coordinator()
            self._unsub_coordinator = None

        await super().async_stop()
        await self._season_job.async_cancel()
        await self._fetcher.async_shutdown()

    # ------------------------------------------------------------------
    # Daily gating (wrapper base scheduler)
    # ------------------------------------------------------------------

    async def async_refresh_weather_daily(self, *, reason: str) -> None:
        """Tenta il refresh solo se il gate giornaliero è scaduto."""
        ran = await super().async_run_if_due(reason=reason)
        if not ran:
            # Gate non scaduto: assicura comunque che i dati cached siano in memoria.
            await self._fetcher.async_load()

    async def _async_on_due(self, reason: str) -> None:
        """Hook base scheduler: eseguito quando il gate giornaliero scatta."""
        await self._fetcher.async_refresh(reason=reason)
        self._schedule_season_detect(reason)

    # ------------------------------------------------------------------
    # Trigger da coordinator updates
    # ------------------------------------------------------------------

    def _on_coordinator_update(self) -> None:
        """Chiamato da ClimateCoordinator ad ogni update (~5 min)."""
        if not self._coordinator.ready:
            return
        # Il debouncer (20 s, immediate=False) rompe il feedback loop:
        # coordinator.update → _decide_and_act → coordinator.update → ...
        # La catena si interrompe perché il debouncer non re-schedula
        # se la chiamata precedente è ancora nel cooldown.
        # self._schedule_decider()

    # ------------------------------------------------------------------
    # Season detection (throttled)
    # ------------------------------------------------------------------

    def _schedule_season_detect(self, reason: str) -> None:
        self._season_job.schedule(
            lambda: self._async_season_detect_guarded(reason), reason=reason
        )

    async def _async_season_detect_guarded(self, reason: str) -> None:
        try:
            await self._async_season_detect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_exception(_LOGGER, "Season detect (%s) fallita: %r", reason, exc)

    async def _async_season_detect(self) -> None:
        today = dt_util.now().date()

        # Snapshot storico come COPIA (HistoricalFetcher.async_dump() garantisce copia).
        # SeasonDetector può fare working_data.update(forecast) senza inquinare la cache.
        historical_snapshot = await self._fetcher.async_dump()

        season_state = await self._detector.async_detect(historical_snapshot, today)

        if season_state is None:
            log_warning(_LOGGER, "Season detect non ha prodotto output per %s.", today)
            return

        self._propagate_season(season_state)

    # ------------------------------------------------------------------
    # Propagazione SeasonState (unico punto di scrittura)
    # ------------------------------------------------------------------

    def _propagate_season(self, season_state: SeasonState) -> None:
        """Scrive SeasonState in hass.data E nel coordinator in modo atomico.

        FIX Bug #4: unico metodo di scrittura — elimina il double-write
        non atomico della versione precedente.

        Ordine: prima hass.data (store globale), poi coordinator.set_season_state
        che può triggerare listener. Così i listener trovano già hass.data aggiornato.
        """
        domain_store = self._hass.data.setdefault(DOMAIN, {})
        entry_store = domain_store.setdefault(self._entry_id, {})
        entry_store[SEASON_STATE] = season_state

        # Questo può triggerare listener sincroni: hass.data è già aggiornato.
        self._coordinator.set_season_state(season_state)

        log_debug(
            _LOGGER,
            "SeasonState propagata: season=%s source=%s as_of=%s healthy=%s",
            season_state.weather.season if season_state.weather else "N/A",
            season_state.detect_model,
            season_state.as_of,
            self._fetcher.is_healthy,
        )

    # ------------------------------------------------------------------
    # Proprietà pubbliche per FSM/DEGRADED
    # ------------------------------------------------------------------

    @property
    def is_healthy(self) -> bool:
        """True se il fetcher storico ha dati validi.

        Da consultare dal layer FSM per decidere se entrare in DEGRADED:
        se False e la stagione è None, la pipeline decisionale non ha
        abbastanza contesto per operare normalmente.
        """
        return self._fetcher.is_healthy

    # ------------------------------------------------------------------
    # HVAC decider bridge (debounced)
    # ------------------------------------------------------------------

    # def _schedule_decider(self) -> None:
    #     self._decider_debouncer.async_schedule_call()

    # async def _decide_and_act(self) -> None:
    #     """Bridge verso ClimateCoordinator.async_decide_and_act().

    #     TODO architetturale: questo bridge va formalizzato come callback/protocol
    #     invece di usare hasattr. ClimateCoordinator dovrebbe iscriversi a un
    #     evento WeatherCoordinator piuttosto che essere chiamato direttamente.
    #     """
    #     decide_fn = getattr(self._coordinator, "async_decide_and_act", None)
    #     if decide_fn is not None:
    #         await decide_fn()
    #     else:
    #         log_warning(
    #             _LOGGER,
    #             "ClimateCoordinator non espone async_decide_and_act — bridge HVAC disabilitato.",
    #         )
