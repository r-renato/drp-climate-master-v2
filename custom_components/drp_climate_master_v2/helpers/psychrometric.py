"""Funzioni psicrometriche per il controllo HVAC (soffitto radiante + VMC).

Dipendenza: psychroLib==2.5.0 (dichiarata in manifest.json["requirements"]).

Nota sul sistema di unità (SetUnitSystem)
------------------------------------------
psychroLib mantiene il sistema di unità come variabile globale di modulo.
In Home Assistant, più entry dello stesso custom component condividono lo
stesso processo Python: chiamare SetUnitSystem() in __init__ del coordinator
significherebbe che l'ultima entry a inizializzarsi vince per tutte.

Soluzione adottata: SetUnitSystem(SI) viene chiamato UNA VOLTA a livello di
modulo (import time). Questo è safe perché:
  - Il progetto usa esclusivamente unità SI.
  - L'impostazione è idempotente (chiamarla N volte con SI è equivalente a
    chiamarla una volta).
  - I consumer di questo modulo non devono preoccuparsi dello stato globale.

Conseguenza: rimuovere l'import psychrolib e la chiamata SetUnitSystem()
dal coordinator — quello è il punto d'ingresso sbagliato.

Funzioni esposte
----------------
  Dew point e stato igrometrico:
    dew_point_celsius(t_c, rh_pct)            -> °C  [CANONICA — usa psychroLib]
    humidity_ratio_from_rh(t_c, rh_pct)       -> kg_vap/kg_aria_secca
    delta_humidity_ratio(t_c, rh_now, rh_tgt) -> DeltaW kg/kg  (eccesso vapore)

  Carico latente VMC (controllo proporzionale step 0-5):
    latent_load_kw(delta_w, flow_m3h)         -> kW latente da rimuovere
    vmc_step_from_delta_w(delta_w, thresholds) -> int step 0-5
    rer_rev_flow_m3h(size, step)              -> m3/h portata stimata per step

  Utilità:
    celsius_to_fahrenheit(celsius)            -> °F
    heat_index_celsius(t_c, rh_pct)           -> °C  (NWS / Rothfusz)
"""

from __future__ import annotations

import logging
from typing import Sequence

import psychrolib

# ---------------------------------------------------------------------------
# Inizializzazione sistema di unità — UNA VOLTA, a livello di modulo.
# Vedere docstring del modulo per la motivazione.
# ---------------------------------------------------------------------------
psychrolib.SetUnitSystem(psychrolib.SI)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Costanti fisiche nominate
# ---------------------------------------------------------------------------

# Calore latente di vaporizzazione dell'acqua a 20 °C (J/kg).
# Usato per convertire DeltaW in potenza latente.
# Valore a 0 °C: 2_501_000 J/kg; a 20 °C: ~2_453_500 J/kg.
# Si usa il valore a 20 °C come riferimento per condizioni indoor tipiche.
H_FG_J_PER_KG: float = 2_453_500.0

# Densità dell'aria secca a 20 °C, 101 325 Pa (kg/m³).
# Utilizzata per convertire portata volumetrica in portata massica.
RHO_AIR_KG_M3: float = 1.204

# Step VMC massimo (Eneren RER/REV: "Set ricambio" registro Modbus HR3, range 0-5).
VMC_STEP_MAX: int = 5


# ---------------------------------------------------------------------------
# Dew point (funzione CANONICA del progetto)
# ---------------------------------------------------------------------------

def dew_point_celsius(t_c: float, rh_pct: float) -> float:
    """Punto di rugiada (°C) da temperatura (°C) e UR (%) — via psychroLib.

    Questa è la funzione CANONICA per il calcolo del dew point nel progetto.
    La funzione dew_point_c presente in helpers/sensor_aggregator.py
    (formula di Magnus) è un duplicato storico; i nuovi consumer devono
    importare questa.

    Precisione: psychroLib usa l'equazione di Buck, errore < 0.05 °C nel
    range 5-40 °C. La Magnus in sensor_aggregator ha errore < 0.3 °C nello
    stesso range — entrambe accettabili per HVAC, ma questa è più accurata.

    Args:
        t_c:    Temperatura aria bulbo-secco (°C). Range utile: -10 a 60 °C.
        rh_pct: Umidità relativa (%). Deve essere in [0, 100].

    Returns:
        Punto di rugiada in °C.

    Raises:
        ValueError: se rh_pct è fuori dall'intervallo [0, 100].
    """
    rh_pct = float(rh_pct)
    if not 0.0 <= rh_pct <= 100.0:
        raise ValueError(f"rh_pct deve essere in [0, 100], ricevuto: {rh_pct}")
    return float(psychrolib.GetTDewPointFromRelHum(float(t_c), rh_pct / 100.0))


# ---------------------------------------------------------------------------
# Rapporto di umidità — base per il controllo proporzionale VMC
# ---------------------------------------------------------------------------

def humidity_ratio_from_rh(t_c: float, rh_pct: float, pressure_pa: float=100539) -> float:
    """Rapporto di umidità W (kg_vapore / kg_aria_secca) da T e UR.

    W è la grandezza fisica che descrive il contenuto assoluto di vapore
    acqueo nell'aria. A differenza dell'UR (relativa), W non cambia se
    cambia la temperatura a parità di vapore presente: è la grandezza
    corretta per calcolare il carico latente da rimuovere con la VMC.

    Relazione con UR:
        W = 0.622 * p_v / (p_atm - p_v)
    dove p_v è la pressione parziale del vapore (funzione di T e UR).

    Args:
        t_c:    Temperatura aria (°C).
        rh_pct: Umidità relativa (%). Deve essere in [0, 100].

    Returns:
        Rapporto di umidità W in kg_vap/kg_aria_secca.
        Valori tipici indoor: 0.007-0.014 kg/kg.

    Raises:
        ValueError: se rh_pct è fuori dall'intervallo [0, 100].
    """
    rh_pct = float(rh_pct)
    if not 0.0 <= rh_pct <= 100.0:
        raise ValueError(f"rh_pct deve essere in [0, 100], ricevuto: {rh_pct}")
    return float(psychrolib.GetHumRatioFromRelHum(float(t_c), rh_pct / 100.0, pressure_pa))


def delta_humidity_ratio(
    t_c: float,
    rh_now_pct: float,
    rh_target_pct: float,
) -> float:
    """Eccesso di vapore da rimuovere DeltaW (kg_vap/kg_aria_secca).

    Calcola la differenza di rapporto di umidità tra stato corrente e target.
    Ritorna 0.0 se l'aria è già al di sotto del target (non serve deumidifica).

    Questa è la grandezza di domanda per il controllo proporzionale VMC:
    DeltaW grande -> step alto; DeltaW piccolo -> step basso.

    Rispetto al DeltaDP (usato nell'attuale logica on/off in VmcSpeedConfig),
    DeltaW è più accurato perché il rapporto di umidità è lineare con la
    massa di vapore da estrarre, mentre il DP ha scala logaritmica.

    Args:
        t_c:           Temperatura aria (°C) — usata per entrambi i calcoli W.
        rh_now_pct:    UR corrente (%). Deve essere in [0, 100].
        rh_target_pct: UR target (%). Deve essere in [0, 100].

    Returns:
        DeltaW >= 0 in kg_vap/kg_aria_secca.
        Ritorna 0.0 se rh_now <= rh_target (condizione già soddisfatta).
    """
    w_now = humidity_ratio_from_rh(float(t_c), float(rh_now_pct))
    w_tgt = humidity_ratio_from_rh(float(t_c), float(rh_target_pct))
    return float(max(0.0, w_now - w_tgt))


# ---------------------------------------------------------------------------
# Carico latente e controllo proporzionale step VMC
# ---------------------------------------------------------------------------

def latent_load_kw(
    delta_w: float,
    flow_m3h: float,
    rho_air_kg_m3: float = RHO_AIR_KG_M3,
    h_fg_j_per_kg: float = H_FG_J_PER_KG,
) -> float:
    """Potenza latente da rimuovere (kW) per deumidificazione VMC.

    Formula:
        Q_lat = DeltaW * (flow_m3h / 3600) * rho_aria * h_fg   [W]
              / 1000                                             -> [kW]

    dove:
        DeltaW = delta_humidity_ratio()   [kg_vap/kg_aria_secca]
        flow   = portata aria trattata    [m³/h]
        rho    = densità aria secca       [kg/m³]
        h_fg   = calore latente vaporizz. [J/kg]

    Nota sulla portata VMC (Eneren RER/REV):
        Il datasheet pubblica la portata NOMINALE (step 5) per taglia:
            015->160, 020->260, 035->380, 050->520, 100->1000 m³/h.
        Per gli step intermedi usare rer_rev_flow_m3h(size, step).
        La taglia si ricava dal registro Modbus IR16 "Taglia macchina".

    Args:
        delta_w:        DeltaW eccesso vapore [kg_vap/kg_aria_secca].
        flow_m3h:       Portata aria trattata [m³/h].
        rho_air_kg_m3:  Densità aria secca [kg/m³]. Default RHO_AIR_KG_M3.
        h_fg_j_per_kg:  Calore latente vaporizzazione [J/kg]. Default H_FG_J_PER_KG.

    Returns:
        Potenza latente in kW. Ritorna 0.0 se delta_w <= 0 o flow <= 0.
    """
    dw = float(delta_w)
    q = float(flow_m3h)
    if dw <= 0.0 or q <= 0.0:
        return 0.0
    flow_kg_s = (q / 3600.0) * float(rho_air_kg_m3)
    return float(dw * flow_kg_s * float(h_fg_j_per_kg) / 1000.0)


def vmc_step_from_delta_w(
    delta_w: float,
    thresholds: Sequence[float] | None = None,
    step_max: int = VMC_STEP_MAX,
) -> int:
    """Mappa DeltaW (eccesso vapore) allo step VMC proporzionale (0-step_max).

    Sostituisce la logica on/off binaria con un controllo a gradini
    proporzionale alla domanda di deumidificazione.

    Struttura thresholds:
        Lista di N soglie DeltaW crescenti [kg/kg] che corrispondono agli
        step 1, 2, ..., N. Lo step 0 è implicito per DeltaW < thresholds[0].
        Se len(thresholds) < step_max, gli step superiori non sono
        raggiungibili (utile per limitare rumore acustico in modalità normale).

    Valori default (thresholds=None):
        Calibrati per ambiente residenziale con UR target ~55%, T ~ 26 °C.
        Corrispondono approssimativamente a:
            step 1: DeltaUR ~  2%  -> DeltaW ~ 0.0008 kg/kg
            step 2: DeltaUR ~  4%  -> DeltaW ~ 0.0016 kg/kg
            step 3: DeltaUR ~  7%  -> DeltaW ~ 0.0028 kg/kg
            step 4: DeltaUR ~ 11%  -> DeltaW ~ 0.0045 kg/kg
            step 5: DeltaUR ~ 16%  -> DeltaW ~ 0.0065 kg/kg
        Questi sono seed conservativi — calibrare dopo commissioning con
        misure anemometriche (procedura taratura §9.6 manuale RER/REV).

    Coerenza con VmcSpeedConfig.dp_boost_step1/2/3:
        I threshold esistenti in VmcSpeedConfig sono espressi in DeltaDP (°C).
        Questo approccio li supera lavorando su DeltaW, grandezza fisicamente
        corretta. Unificare il config in un refactor futuro dedicato.

    Args:
        delta_w:    DeltaW eccesso vapore [kg_vap/kg_aria_secca].
        thresholds: Soglie DeltaW crescenti per step 1, 2, ... N. Vedi sopra.
        step_max:   Step massimo consentito (default VMC_STEP_MAX = 5).

    Returns:
        Step intero in [0, step_max].
    """
    _DEFAULT_THRESHOLDS: tuple[float, ...] = (
        0.0008,   # step 1: inizio deumidifica leggera
        0.0016,   # step 2
        0.0028,   # step 3: domanda moderata
        0.0045,   # step 4
        0.0065,   # step 5: domanda intensa
    )

    thr = list(thresholds) if thresholds is not None else list(_DEFAULT_THRESHOLDS)
    dw = float(delta_w)

    if dw <= 0.0:
        return 0

    step = 0
    for i, t in enumerate(thr):
        if dw >= float(t):
            step = i + 1
        else:
            break

    return min(int(step), int(step_max))


# ---------------------------------------------------------------------------
# Portata aria per step (helper commissioning — Eneren RER/REV)
# ---------------------------------------------------------------------------

# Mappa taglia RER/REV -> portata nominale m³/h (step 5, bocca libera).
# Fonte: datasheet Eneren RER/REV Rev 15, §6.1, colonna "Portata d'aria nominale".
# La taglia dell'unità installata si legge dal registro Modbus IR16 all'avvio.
_RER_REV_FLOW_NOMINAL_M3H: dict[int, float] = {
    15:  160.0,
    20:  260.0,
    35:  380.0,
    50:  520.0,
    100: 1000.0,
}


def rer_rev_flow_m3h(size: int, step: int, step_max: int = VMC_STEP_MAX) -> float:
    """Stima portata aria (m³/h) per taglia e step VMC (Eneren RER/REV).

    Utilizza la portata nominale (step massimo) dal datasheet e assume scala
    lineare step -> portata. L'approssimazione è valida per ventilatori
    brushless inverter nel range 20-100% velocità nominale.

    La stima è conservativa: step 1 = 20% portata nominale.
    Verificare con misura anemometrica durante taratura (procedura §9.6
    del manuale, registri Modbus HR101-105).

    Nota: il registro Modbus IR19 "Stato ventilatore mandata %" restituisce
    la velocità effettiva del motore in tempo reale; in futuro si può usarlo
    con le curve Pfa/V (§6.2 del manuale) per una stima più accurata.

    Args:
        size:     Taglia unità (15, 20, 35, 50, 100). Dal registro IR16.
        step:     Step ricambio corrente (0-step_max). Dal registro IR23.
        step_max: Step massimo configurato (default VMC_STEP_MAX = 5).

    Returns:
        Portata stimata in m³/h. Ritorna 0.0 per step=0 o taglia sconosciuta.
    """
    if step <= 0:
        return 0.0
    nominal = _RER_REV_FLOW_NOMINAL_M3H.get(int(size))
    if nominal is None:
        _LOGGER.warning(
            "rer_rev_flow_m3h: taglia %s non in tabella datasheet %s",
            size,
            sorted(_RER_REV_FLOW_NOMINAL_M3H),
        )
        return 0.0
    fraction = min(float(step), float(step_max)) / float(step_max)
    return float(nominal * fraction)


# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------

def celsius_to_fahrenheit(celsius: float) -> float:
    """Conversione temperatura da °C a °F."""
    return float(celsius) * 9.0 / 5.0 + 32.0


def heat_index_celsius(t_c: float, rh_pct: float) -> float:
    """Heat Index (°C) da temperatura (°C) e UR (%) — algoritmo NWS/Rothfusz.

    Stima il "caldo percepito" combinando temperatura e umidità relativa.
    Valido solo per condizioni calde e umide (T > 26.7 °C e UR > 40 %);
    al di fuori di questo range restituisce t_c senza correzione.

    Algoritmo:
        1. Stima rapida (Steadman semplificato) in dominio °F.
        2. Se HI >= 80 °F: regressione di Rothfusz.
        3. Aggiustamenti NWS per UR molto bassa (<13%) o alta (>85%).

    Nota: heat_index è metrica di comfort per climi caldi afosi. Nel progetto
    è usato come sensore diagnostico di disagio estivo, non come input del
    controllo attivo. La versione in sensor_aggregator.py::heat_index_c()
    usa la stessa regressione Rothfusz senza lo step 1 (applicata sempre
    se T > 26.7 °C); mantenere coerenza se si modifica questa funzione.

    Args:
        t_c:    Temperatura aria (°C).
        rh_pct: Umidità relativa (%).

    Returns:
        Heat Index in °C. Uguale a t_c se fuori dall'intervallo di validità.
    """
    t_c = float(t_c)
    rh = float(rh_pct)

    if t_c < 26.7 or rh < 40.0:
        return t_c

    tf = t_c * 9.0 / 5.0 + 32.0

    # Step 1: stima rapida (Steadman)
    hi_f = 0.5 * (tf + 61.0 + (tf - 68.0) * 1.2 + rh * 0.094)
    hi_f = (hi_f + tf) / 2.0

    # Step 2: regressione Rothfusz per HI >= 80 °F
    if hi_f >= 80.0:
        hi_f = (
            -42.379
            + 2.04901523 * tf
            + 10.14333127 * rh
            - 0.22475541 * tf * rh
            - 0.00683783 * tf * tf
            - 0.05481717 * rh * rh
            + 0.00122874 * tf * tf * rh
            + 0.00085282 * tf * rh * rh
            - 0.00000199 * tf * tf * rh * rh
        )
        # Aggiustamento NWS: aria secca calda
        if rh < 13.0 and 80.0 <= tf <= 112.0:
            hi_f -= ((13.0 - rh) / 4.0) * ((17.0 - abs(tf - 95.0)) / 17.0) ** 0.5
        # Aggiustamento NWS: aria molto umida ma tiepida
        elif rh > 85.0 and 80.0 <= tf <= 87.0:
            hi_f += ((rh - 85.0) / 10.0) * ((87.0 - tf) / 5.0)

    return float((hi_f - 32.0) * 5.0 / 9.0)
