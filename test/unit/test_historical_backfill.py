"""Test unitari del backfill storico PirateWeather (patch #21).

Problema risolto
----------------
Il backfill chiedeva ~2250 giorni in un colpo (6 richieste in parallelo): il time machine
rispondeva 503/timeout e ogni errore transitorio incrementava ``failed`` fino a 3, dopo
di che il giorno non veniva più ritentato (1505 giorni bloccati).

Copertura
---------
A. Errori transitori non contano; definitivi sì (fino a ``max_fetch_failures``).
B. Entry legacy (``failed`` senza ``failed_kind``) ritentate da zero (reset senza migrazione).
C. Lotti: dimensione, budget per ciclo, ordine dal più recente, salvataggio a ogni lotto.
D. Interruttore: lotti consecutivi senza successi fermano il ciclo.
E. Fase recente senza budget; successo rimuove i metadati di errore.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from custom_components.drp_climate_master_v2.controller.weather.historical_fetcher import (
    FAILED_KIND_PERMANENT,
    HistoricalFetcher,
    HistoricalFetcherConfig,
)
from custom_components.drp_climate_master_v2.helpers.weather.pirateweather_client import (
    FAILED_TRANSIENT_KEY,
)


class FakeCache:
    def __init__(self, items: dict[date, dict[str, Any]] | None = None) -> None:
        self.items: dict[date, dict[str, Any]] = dict(items or {})
        self.saves = 0

    async def async_get_many(self, days):
        return {d: dict(self.items[d]) for d in days if d in self.items}

    async def async_put_many(self, items):
        self.saves += 1
        self.items.update({d: dict(v) for d, v in items.items()})


class FakePW:
    """Esito per giorno: 'ok' | 'transient' | 'permanent' | 'exc'; registra i lotti."""

    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.batches: list[list[date]] = []
        self.conc: list[int | None] = []

    async def async_fetch_days(self, days, *, max_concurrency=None):
        self.batches.append(list(days))
        self.conc.append(max_concurrency)
        out: dict[date, Any] = {}
        for d in days:
            kind = self.outcome(d) if callable(self.outcome) else self.outcome
            if kind == "ok":
                out[d] = {"datetime": d.isoformat(), "native_temperature": 20.0}
            elif kind == "transient":
                out[d] = {"datetime": d.isoformat(), "failed": 1, FAILED_TRANSIENT_KEY: True}
            elif kind == "permanent":
                out[d] = {"datetime": d.isoformat(), "failed": 1}
            # 'exc' → assente
        return out


def _fetcher(pw: FakePW, cache: FakeCache, **cfg) -> HistoricalFetcher:
    f = object.__new__(HistoricalFetcher)
    f._pw_client = pw
    f._cache = cache
    f._cfg = HistoricalFetcherConfig(
        days_back=cfg.pop("days_back", 100),
        backfill_batch_pause_s=0.0,
        **cfg,
    )
    return f


def _run(f: HistoricalFetcher) -> dict[date, Any]:
    return asyncio.run(f._fetch_and_cache(reason="test"))


def _today() -> date:
    return dt_util.now().date()


# ---------------------------------------------------------------- A
def test_transitorio_non_incrementa_failed_e_non_scrive() -> None:
    cache = FakeCache()
    _run(_fetcher(FakePW("transient"), cache, days_back=5, priority_days=10))
    assert cache.items == {}  # nessuna entry "failed": i giorni restano da scaricare


def test_transitorio_lascia_invariata_entry_legacy() -> None:
    d = _today() - timedelta(days=3)
    cache = FakeCache({d: {"datetime": d.isoformat(), "failed": 3}})
    _run(_fetcher(FakePW("transient"), cache, days_back=5, priority_days=10))
    assert cache.items[d] == {"datetime": d.isoformat(), "failed": 3}


def test_definitivo_incrementa_fino_al_massimo_e_poi_salta() -> None:
    d = _today() - timedelta(days=2)
    cache = FakeCache()
    pw = FakePW(lambda x: "permanent" if x == d else "ok")
    f = _fetcher(pw, cache, days_back=4, priority_days=10)
    for expected in (1, 2, 3):
        _run(f)
        assert cache.items[d]["failed"] == expected
        assert cache.items[d]["failed_kind"] == FAILED_KIND_PERMANENT
    pw.batches.clear()
    _run(f)
    assert all(d not in b for b in pw.batches)  # a 3: non più richiesto


# ---------------------------------------------------------------- B
def test_entry_legacy_failed_3_ritentata_e_poi_risolta() -> None:
    d = _today() - timedelta(days=70)
    cache = FakeCache({d: {"datetime": d.isoformat(), "failed": 3}})
    pw = FakePW("ok")
    _run(_fetcher(pw, cache, days_back=80))
    assert any(d in b for b in pw.batches)
    assert "failed" not in cache.items[d] and cache.items[d]["native_temperature"] == 20.0


def test_legacy_con_fallimento_definitivo_riparte_da_uno() -> None:
    d = _today() - timedelta(days=70)
    cache = FakeCache({d: {"datetime": d.isoformat(), "failed": 3}})
    _run(_fetcher(FakePW(lambda x: "permanent" if x == d else "ok"), cache, days_back=80))
    assert cache.items[d]["failed"] == 1 and cache.items[d]["failed_kind"] == FAILED_KIND_PERMANENT


# ---------------------------------------------------------------- C
def test_budget_lotti_e_ordine_dal_piu_recente() -> None:
    pw = FakePW("ok")
    cache = FakeCache()
    _run(_fetcher(pw, cache, days_back=100, priority_days=10,
                  backfill_budget_days=30, backfill_batch_days=10))
    flat = [d for b in pw.batches for d in b]
    t = _today()
    assert len(flat) == 10 + 30  # recenti + budget arretrati
    assert flat == [t - timedelta(days=i) for i in range(40)]  # recente → vecchio
    assert all(len(b) <= 10 for b in pw.batches)
    assert cache.saves == len(pw.batches)  # cache salvata a ogni lotto
    assert set(pw.conc) == {2}  # concorrenza di backfill


def test_cicli_successivi_proseguono_dal_punto_raggiunto() -> None:
    pw = FakePW("ok")
    cache = FakeCache()
    f = _fetcher(pw, cache, days_back=100, priority_days=10, backfill_budget_days=30,
                 backfill_batch_days=10)
    _run(f)
    pw.batches.clear()
    _run(f)
    flat = {d for b in pw.batches for d in b}
    t = _today()
    assert t - timedelta(days=45) in flat and t - timedelta(days=5) not in flat
    assert max(flat) <= t  # nessun giorno già in cache viene ririchiesto (salvo oggi)
    assert all((t - d).days >= 40 or d == t for d in flat)


# ---------------------------------------------------------------- D
def test_interruttore_dopo_lotti_consecutivi_senza_successi() -> None:
    pw = FakePW("transient")
    _run(_fetcher(pw, FakeCache(), days_back=100, priority_days=0,
                  backfill_budget_days=100, backfill_batch_days=10, backfill_max_bad_batches=2))
    assert len(pw.batches) == 2


def test_interruttore_si_azzera_con_un_successo() -> None:
    n = {"i": 0}

    def outcome(d):
        n["i"] += 1
        return "ok" if 11 <= n["i"] <= 20 else "transient"

    pw = FakePW(outcome)
    _run(_fetcher(pw, FakeCache(), days_back=100, priority_days=0,
                  backfill_budget_days=100, backfill_batch_days=10, backfill_max_bad_batches=2))
    assert len(pw.batches) == 4  # KO, OK, KO, KO → stop


# ---------------------------------------------------------------- E
def test_fase_recente_non_ha_budget() -> None:
    pw = FakePW("ok")
    _run(_fetcher(pw, FakeCache(), days_back=100, priority_days=50,
                  backfill_budget_days=0, backfill_batch_days=20))
    assert sum(len(b) for b in pw.batches) == 50


def test_eccezione_task_trattata_come_transitoria() -> None:
    cache = FakeCache()
    _run(_fetcher(FakePW("exc"), cache, days_back=5, priority_days=10))
    assert cache.items == {}


def test_successo_rimuove_metadati_errore() -> None:
    d = _today() - timedelta(days=1)
    cache = FakeCache({d: {"datetime": d.isoformat(), "failed": 2, "failed_kind": FAILED_KIND_PERMANENT}})
    _run(_fetcher(FakePW("ok"), cache, days_back=3, priority_days=10))
    assert "failed" not in cache.items[d] and "failed_kind" not in cache.items[d]


# ---------------------------------------------------------------- F (client)
class _Resp:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status, self._p, self.headers = status, payload or {}, {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._p


class _Session:
    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour

    def get(self, url, params=None, headers=None):
        r = self.behaviour()
        if isinstance(r, BaseException):
            raise r
        return r


def _client(behaviour):
    from custom_components.drp_climate_master_v2.helpers.weather.pirateweather_client import (
        PirateWeatherConfig,
        PirateWeatherDayClient,
        ProviderOptions,
    )

    cfg = PirateWeatherConfig(lat=41.9, lon=12.4, api_key="k")
    return PirateWeatherDayClient(cfg, ProviderOptions(retries=0), _Session(behaviour))


def _fetch_one(client):
    fc, _ = asyncio.run(client.async_fetch_day(date(2024, 1, 1)))
    return fc


def test_client_503_e_timeout_sono_transitori() -> None:
    assert _fetch_one(_client(lambda: _Resp(503))).get(FAILED_TRANSIENT_KEY) is True
    assert _fetch_one(_client(lambda: _Resp(429))).get(FAILED_TRANSIENT_KEY) is True
    assert _fetch_one(_client(lambda: asyncio.TimeoutError())).get(FAILED_TRANSIENT_KEY) is True


def test_client_400_e_payload_invalido_sono_definitivi() -> None:
    for resp in (_Resp(400), _Resp(404), _Resp(200, {"nodaily": 1})):
        fc = _fetch_one(_client(lambda r=resp: r))
        assert fc["failed"] == 1 and FAILED_TRANSIENT_KEY not in fc


def test_async_fetch_days_rispetta_max_concurrency() -> None:
    from custom_components.drp_climate_master_v2.helpers.weather.pirateweather_client import (
        PirateWeatherConfig,
        PirateWeatherTimeMachineClient,
        ProviderOptions,
    )

    cur = {"n": 0, "max": 0}

    class Slow(PirateWeatherTimeMachineClient):
        async def async_fetch_day(self, d):  # type: ignore[override]
            cur["n"] += 1
            cur["max"] = max(cur["max"], cur["n"])
            await asyncio.sleep(0.01)
            cur["n"] -= 1
            return {"datetime": d.isoformat()}

    c = Slow(PirateWeatherConfig(lat=1, lon=1, api_key="k"), ProviderOptions(), _Session(lambda: _Resp(200)))
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(10)]
    asyncio.run(c.async_fetch_days(days, max_concurrency=2))
    assert cur["max"] == 2
