"""Filtro di gating della domanda termica multi-zona.

Responsabilita
--------------
Questo modulo calcola un insieme di soglie e flag booleani - il ``GatingResult``
- a partire dai segnali di domanda aggregati (``PlantDemandSignals``) e dal
profilo operativo utente. Il risultato viene consumato dal ``ModeResolver``
per scegliere la modalita operativa dell'impianto (HEATING / COOLING /
IAQ_ONLY / ...).

Il gating non comanda attuatori ne legge direttamente sensori: e una funzione
pura che traduce "quanto e fuori comfort l'edificio?" in "vale la pena avviare
l'impianto adesso?", tenendo conto di:

  - l'intensita della domanda (peggio zona, media pesata),
  - la sua diffusione (quante zone e con quale peso),
  - il profilo operativo utente (aggressivita),
  - la temperatura esterna (velocita di peggioramento attesa),
  - il regime meteorologico ML (es. non preriscaldare in regime caldo),
  - le richieste autonome della VMC (latente, aria fresca).

Posizione nella pipeline
------------------------
::

    PlantDemandSignals          <- segnali aggregati (deficit, coverage, DP...)
           |
           v
    compute_gating()            <- questo modulo
           |
           v
    GatingResult                <- flag / soglie
           |
           v
    ModeResolver.decide()       <- scelta PlantMode
           |
           v
    PdcCommandBuilder + SupplyCommandBuilder + ZoneValvesCommandBuilder

Contesto termotecnico - soffitto radiante
-----------------------------------------
Un impianto radiante a soffitto ha inerzia termica significativa: la massa
del solaio e dell'aria accumula e rilascia calore su scale temporali di
30-90 minuti. Questo implica due requisiti contrapposti:

1. **Anti short-cycling**: avviare l'impianto ha un costo fisso (rampa PDC,
   transitorio idraulico, stress termomeccanico sul solaio). Non conviene
   per micro-deficit transienti di una singola zona.

2. **Preheat predittivo**: a causa dell'inerzia, e necessario avviare
   *prima* che il deficit si manifesti pienamente, altrimenti la casa non
   raggiunge la comfort band nei tempi attesi.

Il gating bilancia questi due requisiti: il quorum multi-zona previene
gli avvii per micro-deficit, il preheat MPC consente l'anticipo controllato.

Nota sull'impianto specifico (Via Camillo Negro, Roma)
------------------------------------------------------
Prestazioni radiante soffitto (Eurotherm Leonardo 3.5):

  - Riscaldamento: 69 W/m2 a T_man=35C, dt=4C
  - Raffrescamento: 52 W/m2 a T_man=14C, dt=4C

La bassa densita di potenza in raffrescamento rende l'impianto piu sensibile
alle condizioni igrotermiche (rischio condensa); il gating di raffrescamento
e quindi piu conservativo e si coordina con il dew-point guard.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ....domain.enums import HVACOperatingProfile

from ..config import PlantPlannerConfig
from ..contracts import PlantDemandSignals
from ..zone.model import ZonesDecision


@dataclass(slots=True, frozen=True)
class GatingResult:
    """Risultato del filtro di gating: soglie e flag di attivazione impianto.

    Oggetto immutabile (``frozen=True``) consumato da ``ModeResolver.decide()``.
    Tutti i campi sono calcolati da ``compute_gating()`` e non devono essere
    modificati dopo la costruzione.

    Campi di soglia
    ---------------
    ctrl_aggr : float
        Fattore di aggressivita di controllo per il profilo attivo
        (adimensionale, 1.0 = COMFORT nominale). Fonte: ``DemandGatingConfig
        .ctrl_aggr_by_profile``. Usato dal ``ModeResolver`` per diagnostica
        e dal planner MPC zona per scalare i pesi (unica fonte di verita).

    heat_thr_c : float  [C]
        Soglia di attivazione riscaldamento effettiva, dopo modulazione
        per profilo::

            heat_thr = heat_on_deficit_c / ctrl_eff

        Con ctrl_eff=1.35 (BOOST) la soglia si abbassa: impianto piu reattivo.
        Con ctrl_eff=0.50 (AWAY) la soglia sale: impianto molto conservativo.

    cool_thr_c : float  [C]
        Soglia di attivazione raffrescamento effettiva (stessa formula).

    quorum_cov_req : float  [0..1]
        Copertura minima richiesta (frazione di zona pesata fuori banda)
        per autorizzare l'avvio in profili ECO/SLEEP/AWAY.
        Es. 0.25 = almeno il 25% della superficie pesata deve avere deficit > 0.

    Campi diagnostici di gating (COMFORT/BOOST: None; altri profili: bool)
    -----------------------------------------------------------------------
    heat_override : Optional[bool]
        True se il deficit massimo supera la soglia override
        (worst-zone sufficientemente lontana da giustificare l'avvio senza
        quorum, modulata da T_ext).

    heat_quorum_ok : Optional[bool]
        True se la copertura di zone con deficit > 0 supera ``quorum_cov_req``.
        In multi-zona impedisce avvii per una sola piccola stanza fredda.

    heat_mean_ok : Optional[bool]
        True se la media pesata del deficit supera la soglia x ``demand_mean_factor``.
        Complementare al quorum: molte zone con deficit piccolo ma distribuito.

    cool_override, cool_quorum_ok, cool_mean_ok : Optional[bool]
        Analoghi per il raffrescamento.

    Campi di verdetto sensibile
    ---------------------------
    heat_sensible : bool
        True se la domanda di riscaldamento e "sensibile" (giustifica l'avvio
        del circuito idronico). Combina le condizioni sopra per profilo.

    cool_sensible : bool
        Analoga per raffrescamento.

    Richieste VMC (canali indipendenti)
    ------------------------------------
    vmc_req_heat : bool
        La VMC ha richiesto integrazione in caldo (acqua dall'impianto).
        Indipendente da heat_sensible: la VMC puo chiedere acqua calda
        anche quando le zone radianti sono in comfort (es. recupero
        entalpico insufficiente in giorni freddi).

    vmc_req_cool : bool
        La VMC ha richiesto integrazione in freddo.

    vmc_req_dehum : bool
        La VMC ha richiesto deumidifica. In estate e il segnale principale
        che attiva il circuito idronico in DEHUM_ASSIST anche senza surplus
        sensibile (la carica latente supera la capacita del recuperatore).

    Flag finali (contratto verso ModeResolver)
    ------------------------------------------
    any_heat : bool
        Sintesi OR: ``heat_sensible OR vmc_req_heat OR (zones_any_heat AND
        zones_preheat_ok)``. Unico flag che il ModeResolver controlla per
        decidere HEATING in stagione appropriata.

    any_cool : bool
        ``cool_sensible OR vmc_req_cool``.

    any_cool_or_dehum : bool
        ``any_cool OR vmc_req_dehum``. Usato dal resolver per decidere
        COOLING / DEHUM_ASSIST.

    Preheat MPC
    -----------
    zones_any_heat : bool
        Il piano MPC-lite prevede almeno una valvola di zona aperta al prossimo
        passo di pianificazione. Non implica avvio immediato da solo.

    zones_full_on_pct : Optional[float]  [%]
        Percentuale di zone pianificate a "full ON" sull'intero orizzonte MPC
        (0..100). Valore alto indica che il modello RC prevede una dispersione
        rapida: utile per diagnostica e tuning.

    zones_preheat_ok : bool
        True se tutte le condizioni di preheat sono soddisfatte:
        (1) headroom stretta, (2) almeno un segnale quantitativo non-zero,
        (3) regime meteorologico non "hot", (4) profilo non AWAY/VACATION.

    zones_preheat_skipped_reason : Optional[str]
        Motivo di inibizione del preheat (``"headroom_too_large"``,
        ``"demand_zero"``, ``"regime_hot"``, ``"profile_away_vacation"``).
        None se il preheat e attivo o non e stato valutato.

    KPI MPC (diagnostica e tuning)
    --------------------------------
    zones_duty_avg_pct : Optional[float]  [%]
        Duty cycle medio delle zone sull'orizzonte MPC (0..100).
        Basso duty: deficit atteso lieve; alto duty: casa in raffreddamento rapido.

    zones_on_now_pct : Optional[float]  [%]
        Percentuale di zone pianificate ON al passo 0 (adesso).
        Usato da ``PdcCommandBuilder`` per scalare il feedback sul WOT:
        con poche zone aperte il circuito e corto, la potenza satura
        rapidamente e non conviene inseguire il deficit aggressivamente.

    zones_first_on_step : Optional[int]
        Primo passo dell'orizzonte MPC con almeno una zona ON.
        0 = almeno una zona apre subito; >0 = anticipo predittivo;
        None = nessuna zona pianificata ON.
    """

    ctrl_aggr: float
    heat_thr_c: float
    cool_thr_c: float
    quorum_cov_req: float

    heat_override: Optional[bool]
    heat_quorum_ok: Optional[bool]
    heat_mean_ok: Optional[bool]

    cool_override: Optional[bool]
    cool_quorum_ok: Optional[bool]
    cool_mean_ok: Optional[bool]

    heat_sensible: bool
    cool_sensible: bool

    vmc_req_heat: bool
    vmc_req_cool: bool
    vmc_req_dehum: bool

    any_heat: bool
    any_cool: bool
    any_cool_or_dehum: bool
    zones_any_heat: bool
    zones_full_on_pct: Optional[float]
    zones_preheat_ok: bool

    # Motivo di inibizione del preheat (diagnostica/log). None = non inibito.
    zones_preheat_skipped_reason: Optional[str]

    # True se il guard shoulder ha soppresso heat_sensible (diagnostica/log).
    shoulder_heat_suppressed: bool = False

    # MPC-lite KPIs (copiati da ZonesDecision.meta quando disponibili)
    zones_duty_avg_pct: Optional[float] = None
    zones_on_now_pct: Optional[float] = None
    zones_first_on_step: Optional[int] = None

    # Segnale globale PDC (Patch B) - diagnostica
    heat_def_global_c: Optional[float] = None
    """Deficit dalla comfort band globale usato per on/off PDC. None se non disponibile."""
    heat_global_pdc_active: Optional[bool] = None
    """True se il segnale globale ha autorizzato heat_sensible. None se non calcolato."""
    heat_pdc_on_thr_eff_c: float = 0.0
    """Soglia effettiva di accensione PDC usata in questo tick, dopo modulazione
    per t_smooth (RMOT). Diagnostico: permette di vedere a log quando la soglia
    è alzata dal guard stagionale rispetto al valore base."""


def compute_gating(
    *,
    cfg: PlantPlannerConfig,
    demand: PlantDemandSignals,
    profile: HVACOperatingProfile,
    zones_decision: Optional[ZonesDecision],
    t_ext: Optional[float] = None,
    t_smooth: Optional[float] = None,
    regime_hint: str = "mild",
    pdc_currently_on: bool = False,
    pdc_current_mode: Optional[str] = None,
) -> GatingResult:
    """Calcola soglie e flag di gating dipendenti dal profilo operativo.

    Funzione pura: nessun side-effect, nessuna scrittura di stato esterno.
    Tutti gli input sono letti, nessuno viene modificato.

    Parametri
    ---------
    cfg : PlantPlannerConfig
        Configurazione completa del planner. I sotto-blocchi usati sono:
        - ``cfg.comfort``   : soglie base deficit/surplus (C)
        - ``cfg.gating``    : fattori override, quorum, ctrl_aggr per profilo
        - ``cfg.zones_mpc`` : soglia headroom preheat (C)

    demand : PlantDemandSignals
        Segnali di domanda aggregati prodotti da ``DemandSignalsBuilder``
        a partire dallo snapshot corrente. Contiene:
        - ``heat_def_max_c``   : deficit massimo peggio-zona (C)
        - ``cool_sur_max_c``   : surplus massimo peggio-zona (C)
        - ``heat_def_wmean_c`` : media pesata deficit (C)
        - ``cool_sur_wmean_c`` : media pesata surplus (C)
        - ``heat_cov``         : copertura zone con deficit > 0 (0..1)
        - ``cool_cov``         : copertura zone con surplus > 0 (0..1)
        - ``heat_headroom_min_c`` : margine minimo al bordo inferiore banda (C)
        - ``vmc_req_heating/cooling/dehumidif`` : richieste VMC
        - ``free_cool_feasible`` : free cooling ventilativo fattibile

    profile : HVACOperatingProfile
        Profilo operativo attivo (COMFORT / ECO / SLEEP / BOOST / AWAY /
        VACATION). Determina aggressivita, quorum e logica di gating sensibile.

    zones_decision : Optional[ZonesDecision]
        Piano MPC-lite zone (orizzonte binario valvole). Se None, tutti i flag
        preheat sono False e i KPI MPC sono None. Questo e il comportamento
        corretto quando il provider MPC e disabilitato o in fault.

    t_ext : Optional[float]  [C]
        Temperatura esterna corrente. Usata per modulare i fattori override:
        - riscaldamento: a T_ext alta il deficit si recupera spontaneamente
          -> soglia override piu alta (meno avvii inutili in primavera/estate).
        - raffrescamento: a T_ext alta il surplus peggiora rapidamente
          -> soglia override piu bassa (intervento tempestivo in estate).
        Se None, si usa il fallback fisso (comportamento conservativo invernale).

    regime_hint : str
        Classificazione ML del regime meteorologico corrente, prodotta da
        ``MeteoContiguousSeasonModel``: ``"cold"`` / ``"mild"`` / ``"hot"``.
        Soglia "hot": t_smooth >= 22C (media mobile 2 mesi).
        Usato esclusivamente per inibire il preheat MPC in regime caldo:
        un regime "hot" implica che la casa non perdera calore nel breve
        termine, rendendo il preriscaldamento fisicamente controproducente.

    Algoritmo
    ---------
    Il calcolo si svolge in cinque fasi sequenziali:

    **Fase 1 - Aggressivita e soglie effettive**

    ``ctrl_aggr`` scala in modo inverso le soglie di attivazione::

        heat_thr = heat_on_deficit_c / ctrl_eff     (ctrl_eff = max(0.2, ctrl_aggr))

    Con BOOST (ctrl_aggr=1.35): heat_thr=0.22C (base 0.3C): reattivo.
    Con AWAY  (ctrl_aggr=0.50): heat_thr=0.60C: conservativo.

    Il ``max(0.2, ...)`` previene una divisione che produrrebbe soglie
    irraggiungibili: anche con ctrl_aggr teoricamente a 0, la soglia non
    supera 5x la base.

    **Fase 2 - Gating sensibile multi-zona**

    COMFORT/BOOST: il gating e semplice: il deficit/surplus massimo deve
    superare la soglia. Non serve quorum: l'utente ha scelto il comfort pieno.

    ECO/SLEEP/AWAY/VACATION: si aggiungono tre condizioni in OR::

        heat_sensible = heat_override
                        OR (heat_def >= heat_thr AND (heat_quorum_ok OR heat_mean_ok))

    - ``heat_override``: worst-zone molto fuori banda: forza avvio anche senza
      quorum (es. camera che scende a 17C in SLEEP). Il fattore override e
      modulato da T_ext: in primavera (T_ext=15C) richiede un deficit maggiore
      rispetto all'inverno (T_ext=2C), perche in primavera l'edificio tende a
      recuperare autonomamente tramite apporti solari.

    - ``heat_quorum_ok``: copertura >= quorum_cov_req. Impedisce avvii per
      una sola piccola stanza fredda (es. bagno di servizio non riscaldato).
      Per ECO il quorum e 0.25: almeno il 25% della superficie pesata deve
      avere deficit > 0.

    - ``heat_mean_ok``: media pesata deficit >= thr x demand_mean_factor.
      Cattura il caso di molte zone con deficit piccolo ma distribuito
      (es. tutta la casa 0.2C sotto la banda).

    Fisica del quorum multi-zona per soffitto radiante: a differenza di un
    fan-coil che cede calore localmente, il soffitto radiante di un piano
    aperto cede energia in modo diffuso. Se una sola zona e fuori banda
    a causa di un apporto solare transitorio, la migliore risposta e spesso
    aprire piu le finestre o attendere che la distribuzione si uniformi,
    non avviare la PDC.

    Per il raffrescamento, ``cool_override`` ha un gate aggiuntivo::

        cool_override = (cool_sur >= thr * eff_cool_override) AND NOT free_cool_feasible

    Se il free cooling ventilativo e fattibile (T_ext < T_int con finestre
    utilizzabili), il surplus di una zona soleggiata non giustifica l'avvio
    del circuito idronico. Questo evita cycling PDC in giornate invernali
    limpide con forti apporti solari su zona esposta a sud.

    **Fase 3 - KPI MPC zona**

    Lettura passiva dei metadati del piano MPC (se disponibile):
    ``mpc_full_on_pct``, ``mpc_duty_avg_pct``, ``mpc_on_now_pct``,
    ``mpc_first_on_step``. Nessuna logica decisionale in questa fase:
    i valori vengono propagati nel ``GatingResult`` per uso diagnostico
    e per la modulazione del feedback WOT in ``PdcCommandBuilder``.

    **Fase 4 - Preheat MPC (condizioni necessarie E sufficienti)**

    Il preheat consente di avviare l'impianto *prima* che il deficit sia
    misurabile, basandosi sulla previsione del modello RC. E il meccanismo
    che compensa l'inerzia termica del soffitto radiante.

    Condizioni AND (tutte richieste):

    (a) ``zones_any_heat``: il piano MPC prevede almeno una valvola aperta.

    (b) ``heat_headroom_min_c <= preheat_headroom_c (0.4C)``: la zona piu
        vicina al bordo inferiore e a meno di 0.4C dal limite. Traduzione
        fisica: l'edificio e cosi vicino al margine inferiore della comfort
        band che, considerata l'inerzia termica, conviene avviare adesso.

    (c) ``demand_nonzero = heat_cov > 0 OR heat_def_wmean > 0``:
        almeno un segnale quantitativo di domanda e non-zero. Questo guard
        esclude il caso in cui il segnale (b) provenga da una zona a peso=0
        (zona di riferimento, non inclusa nel calcolo del deficit). Senza
        questo check, una zona a peso=0 al limite esatto della soglia
        triggerebbe il preheat con heat_def_max=0, heat_cov=0%, heat_cov=0%
        - un falso positivo strutturale.

    (d) ``regime_allows_preheat = regime_hint != "hot"``:
        in regime meteorologico "hot" (t_smooth >= 22C) il modello RC usa
        una temperatura esterna di riferimento vicina alla T interna. La
        deriva predetta e quasi nulla, ma la casa non perdera calore
        comunque: preriscaldare in queste condizioni e fisicamente errato
        e controproducente per il comfort (si passerebbe da 23.7C a 25C+).

    (e) ``profile not in (AWAY, VACATION)``: in assenza prolungata non si
        preriscalda in anticipo; si aspetta il ritorno (gestito dal timer
        di occupazione a livello superiore).

    Le ragioni di inibizione sono registrate in ``zones_preheat_skipped_reason``
    per commissioning e debug.

    **Fase 5 - Flag finali (contratto verso ModeResolver)**

    ::

        any_heat         = heat_sensible OR vmc_req_heat OR (zones_any_heat AND zones_preheat_ok)
        any_cool         = cool_sensible OR vmc_req_cool
        any_cool_or_dehum = any_cool OR vmc_req_dehum

    Il ``ModeResolver`` non legge le condizioni intermedie: controlla solo
    questi tre flag e li interseca con il contesto stagionale (winter /
    summer / shoulder), la presenza di vacanza, e lo stato delle finestre.

    Ritorni
    -------
    GatingResult
        Oggetto immutabile con tutti i flag e le soglie. Non contiene
        riferimenti mutabili: sicuro da condividere tra coroutine HA.
    """

    # -- Lettura segnali di domanda -----------------------------------------
    # Tutti i valori float per evitare comportamenti inattesi da Decimal/None
    # downstream. I campi di PlantDemandSignals hanno gia default=0.0.
    heat_def = float(demand.heat_def_max_c)  # deficit peggio-zona [C >= 0]
    cool_sur = float(demand.cool_sur_max_c)  # surplus peggio-zona [C >= 0]
    heat_cov = float(demand.heat_cov)  # copertura riscaldamento [0..1]
    cool_cov = float(demand.cool_cov)  # copertura raffrescamento [0..1]
    heat_def_wmean = float(demand.heat_def_wmean_c)  # media pesata deficit [C]
    cool_sur_wmean = float(demand.cool_sur_wmean_c)  # media pesata surplus [C]

    # Segnale globale (Patch B): deficit/surplus dalla comfort band media indoor.
    # None se la banda globale non è stata calcolata (fallback su worst-zone).
    _raw_global_heat = getattr(demand, "heat_def_global_c", None)
    _raw_global_cool = getattr(demand, "cool_sur_global_c", None)
    heat_def_global: Optional[float] = float(_raw_global_heat) if _raw_global_heat is not None else None
    cool_sur_global: Optional[float] = float(_raw_global_cool) if _raw_global_cool is not None else None

    # -- Flag isteresi PDC mode-aware (Patch 0016) ----------------------------
    # pdc_currently_on è True se la PDC è alimentata, indipendentemente dal
    # modo. I flag distinti per modo evitano che l'isteresi "keep cooling running"
    # scatti quando la PDC è ON in heating (e viceversa).
    # Fail-safe: se pdc_current_mode è None (sensore non disponibile) entrambi
    # i flag sono False: si usa solo il ramo "start", mai "keep running".
    pdc_heat_on: bool = pdc_currently_on and (pdc_current_mode == "heating")
    pdc_cool_on: bool = pdc_currently_on and (pdc_current_mode == "cooling")

    # -- Fase 1: Aggressivita e soglie effettive ----------------------------
    # ctrl_aggr: unica fonte di verita in DemandGatingConfig.ctrl_aggr_by_profile.
    # ctrl_eff: clamp a 0.2 per evitare soglie irraggiungibili (heat_thr > 5x base).
    ctrl_aggr = cfg.gating.ctrl_aggr(profile)
    ctrl_eff = max(0.2, ctrl_aggr)

    # Soglie effettive: inversamente proporzionali a ctrl_eff.
    # Piu aggressivo = soglia piu bassa = impianto parte prima.
    heat_thr = float(cfg.comfort.heat_on_deficit_c) / ctrl_eff
    cool_thr = float(cfg.comfort.cool_on_surplus_c) / ctrl_eff

    # Quorum copertura: 0.0 per COMFORT/BOOST (qualsiasi zona basta),
    # crescente per profili di risparmio (ECO: 0.25, AWAY: 0.40, ...).
    quorum = float(cfg.gating.quorum_cov(profile))

    # -- Richieste VMC (canali indipendenti dal comfort radiante) -----------
    vmc_req_heat = bool(demand.vmc_req_heating)
    vmc_req_cool = bool(demand.vmc_req_cooling)
    vmc_req_dehum = bool(demand.vmc_req_dehumidif)

    # -- Fase 2: Gating sensibile multi-zona --------------------------------
    # Inizializzati a None: significativo solo per profili non-COMFORT/BOOST.
    # Il ModeResolver li espone nei diagnostici; None indica "non calcolato".
    heat_override = heat_quorum_ok = heat_mean_ok = None
    cool_override = cool_quorum_ok = cool_mean_ok = None

    # Soglia PDC effettiva (usata nel ramo ECO/SLEEP/AWAY; per COMFORT/BOOST
    # la soglia non viene usata per il gating — si espone il valore base).
    _heat_pdc_on_thr: float = cfg.gating.effective_heat_pdc_on_thr(t_smooth)

    if profile in (HVACOperatingProfile.COMFORT, HVACOperatingProfile.BOOST):
        # Profili comfort pieno: basta superare la soglia con la peggio-zona.
        # Nessun quorum richiesto: l'utente vuole comfort massimo, accetta
        # avvii frequenti pur di non avere zone fuori banda.
        heat_sensible = heat_def >= heat_thr
        cool_sensible = cool_sur >= cool_thr
    else:
        # Profili di risparmio (ECO, SLEEP, AWAY, VACATION):
        # logica a tre livelli per ridurre cycling senza sacrificare il comfort
        # nelle situazioni realmente critiche.

        # Factor override modulato da T_ext:
        # - riscaldamento: factor CRESCE con T_ext. Fisica: in primavera
        #   (T_ext=15C) il deficit tende a recuperarsi da solo grazie agli
        #   apporti solari; serve un deficit maggiore per giustificare l'avvio.
        #   In inverno (T_ext=0C) il deficit peggiora rapidamente: factor basso,
        #   intervento tempestivo.
        # - raffrescamento: factor DECRESCE con T_ext (curva opposta).
        eff_heat_override = cfg.gating.effective_heat_override_factor(t_ext)
        eff_cool_override = cfg.gating.effective_cool_override_factor(t_ext)

        # Livello 1 - On/off PDC basato sulla comfort band globale con isteresi
        # (Patch B). La PDC risponde allo stato medio della casa, non alla
        # singola zona peggiore. Fallback su worst-zone se il segnale globale
        # non è disponibile (banda non calcolata).
        #
        # Isteresi: soglia di accensione più alta (deficit >= on_thr) e margine
        # di spegnimento (mantieni acceso finché deficit > -off_margin, cioè
        # finché T_op non ha superato t_op_min di off_margin). Evita short-cycling
        # da oscillazioni attorno al limite.
        # _heat_pdc_on_thr già calcolato sopra (modulato da t_smooth/RMOT).
        _heat_pdc_off_margin = float(cfg.gating.heat_pdc_off_margin_c)
        _cool_pdc_on_thr = float(cfg.gating.cool_pdc_on_thr_c)
        _cool_pdc_off_margin = float(cfg.gating.cool_pdc_off_margin_c)

        if heat_def_global is not None:
            if pdc_heat_on:
                # PDC accesa in heating: mantieni accesa finché non c'è surplus stabile
                heat_override = heat_def_global > -_heat_pdc_off_margin
            else:
                # PDC spenta o in cooling: accendi solo con deficit globale >= soglia
                heat_override = heat_def_global >= _heat_pdc_on_thr
        else:
            # Fallback worst-zone (segnale globale non disponibile)
            heat_override = heat_def >= heat_thr * eff_heat_override

        # Livello 2 - Quorum: abbastanza zone fuori banda (per peso o conteggio).
        heat_quorum_ok = heat_cov >= quorum
        # Livello 3 - Mean: il deficit medio pesato e significativo.
        heat_mean_ok = heat_def_wmean >= heat_thr * float(cfg.gating.demand_mean_factor)
        # Sintesi: override globale O (soglia base worst-zone E (quorum O mean)).
        heat_sensible = bool(heat_override) or (
            (heat_def >= heat_thr) and (bool(heat_quorum_ok) or bool(heat_mean_ok))
        )

        # Free cooling: se la ventilazione naturale gestisce il surplus termico
        # (T_ext < T_int, finestre apribili) non ha senso avviare il circuito
        # idronico. Tipico scenario: giornata invernale soleggiata con zona
        # esposta a sud che supera la comfort band per apporti solari.
        free_cool_ok = bool(getattr(demand, "free_cool_feasible", False))
        # cool_headroom_min_c: min_z(T_max(z) - T_op(z)) — positivo quando la casa è
        # sotto l'upper bound, negativo quando sopra. NON è clamped a 0, quindi
        # può segnalare "casa lontana dall'upper bound" anche quando cool_sur_global=0.
        _cool_headroom: Optional[float] = (
            float(demand.cool_headroom_min_c)
            if getattr(demand, "cool_headroom_min_c", None) is not None
            else None
        )
        if cool_sur_global is not None:
            if pdc_cool_on:
                # PDC accesa in cooling: mantieni accesa solo se la casa è ancora vicina
                # all'upper bound (headroom ≤ off_margin). (Patch 0017)
                # NON usare cool_sur_global (clamped a 0): 0.0 > -0.3 è sempre True
                # → cooling permanente anche con casa 2°C sotto l'upper bound.
                # cool_headroom_min_c = 1.7°C → 1.7 <= 0.3 = False → STOP ✓
                # cool_headroom_min_c = 0.1°C → 0.1 <= 0.3 = True  → run  ✓
                if _cool_headroom is not None:
                    cool_override = (_cool_headroom <= _cool_pdc_off_margin) and not free_cool_ok
                else:
                    # Fail-safe: headroom non disponibile, usa surplus (comportamento pre-patch)
                    cool_override = (cool_sur_global > -_cool_pdc_off_margin) and not free_cool_ok
            else:
                # PDC spenta o in heating: accendi solo con surplus globale >= soglia
                cool_override = (cool_sur_global >= _cool_pdc_on_thr) and not free_cool_ok
        else:
            cool_override = (cool_sur >= cool_thr * eff_cool_override) and not free_cool_ok
        cool_quorum_ok = cool_cov >= quorum
        cool_mean_ok = cool_sur_wmean >= cool_thr * float(cfg.gating.demand_mean_factor)
        cool_sensible = bool(cool_override) or (
            (cool_sur >= cool_thr) and (bool(cool_quorum_ok) or bool(cool_mean_ok))
        )

    # -- Fase 2b: Guard shoulder (soppressione riscaldamento a T_ext alta) -----
    # Se T_ext e' sopra la soglia "recupero spontaneo" (default 12°C) E la media
    # pesata del deficit e' sotto la soglia minima (default 0.30°C), il deficit e'
    # probabilmente transitorio e si recupera senza avviare la PDC.
    #
    # Fisica: a T_ext >= 12°C in mezza stagione mediterranea, la dispersione termica
    # e' ridotta e gli apporti interni (persone, elettrodomestici) sommati agli
    # apporti solari diffusi bilanciano deficit < 0.30°C in ~20-40 min senza impianto.
    # Il guard NON opera in inverno (T_ext < soglia) e NON blocca il preheat MPC
    # (calcolato nella Fase 4 in modo indipendente).
    #
    # Disabilitazione: shoulder_heat_suppress_t_ext_c = 0.0 (mai soppresso).
    shoulder_heat_suppressed = False
    _suppress_t_ext = float(getattr(cfg.gating, "shoulder_heat_suppress_t_ext_c", 12.0))
    _suppress_wmean = float(getattr(cfg.gating, "shoulder_heat_min_wmean_c", 0.30))
    if bool(heat_sensible) and t_ext is not None and t_ext >= _suppress_t_ext:
        if heat_def_wmean < _suppress_wmean:
            heat_sensible = False
            shoulder_heat_suppressed = True

    # -- Fase 2c: Guard shoulder DINAMICO (soglia wmean progressiva con T_ext) ----
    # A T_ext crescente verso la soglia estiva, anche deficit wmean > 0.30°C non
    # giustificano l'avvio della PDC: la dispersione termica verso l'esterno
    # diminuisce e gli apporti solari compensano spontaneamente il deficit.
    # La soglia wmean scala linearmente tra:
    #   (t_ext_lo, wmean_lo) -> (t_ext_hi, wmean_hi)
    # dove t_ext_lo/wmean_lo sono gli stessi della Fase 2b (base), e
    # t_ext_hi/wmean_hi sono i nuovi parametri di ancoraggio superiore.
    #
    # Esempio Roma, fine maggio (T_ext=21.7°C, t_ext_lo=12°C, t_ext_hi=20°C):
    #   alpha = clamp((21.7-12)/(20-12), 0, 1) = 1.0
    #   soglia_dinamica = 0.30 + 1.0*(1.00-0.30) = 1.00°C
    #   wmean=0.9°C < 1.00°C -> heat_sensible soppresso: PDC non avviata
    # Esempio Roma, inizio primavera (T_ext=14°C):
    #   alpha = clamp((14-12)/(20-12), 0, 1) = 0.25
    #   soglia_dinamica = 0.30 + 0.25*0.70 = 0.475°C
    #   wmean=0.9°C > 0.475°C -> NOT soppresso: il freddo è reale
    # Nota: Fase 2c è no-op se t_ext < t_ext_lo (stesso gate della Fase 2b).
    if bool(heat_sensible) and t_ext is not None and t_ext >= _suppress_t_ext:
        _t_ext_hi = float(getattr(cfg.gating, "shoulder_heat_suppress_t_ext_hi_c", 20.0))
        _wmean_hi = float(getattr(cfg.gating, "shoulder_heat_min_wmean_hi_c", 1.00))
        if _t_ext_hi > _suppress_t_ext:
            alpha = min(1.0, max(0.0, (float(t_ext) - _suppress_t_ext) / (_t_ext_hi - _suppress_t_ext)))
            _dynamic_wmean_thr = _suppress_wmean + alpha * (_wmean_hi - _suppress_wmean)
            if heat_def_wmean < _dynamic_wmean_thr:
                heat_sensible = False
                shoulder_heat_suppressed = True

    # -- Fase 3: KPI MPC zona (lettura passiva) -----------------------------
    # Propagati nel GatingResult per diagnostica e per PdcCommandBuilder
    # (activity_scale sul feedback WOT). Nessuna logica decisionale qui.
    zones_any_heat = bool(zones_decision.any_heat_demand) if zones_decision else False
    zones_full_on_pct = zones_decision.meta.get("mpc_full_on_pct") if zones_decision else None
    zones_duty_avg_pct = zones_decision.meta.get("mpc_duty_avg_pct") if zones_decision else None
    zones_on_now_pct = zones_decision.meta.get("mpc_on_now_pct") if zones_decision else None
    zones_first_on_step = zones_decision.meta.get("mpc_first_on_step") if zones_decision else None

    # -- Fase 4: Preheat MPC -------------------------------------------------
    # Il preheat compensa l'inerzia termica del soffitto radiante (tau ~= 6h):
    # avvia l'impianto quando il modello RC prevede che la banda comfort verra
    # violata nell'orizzonte di pianificazione, anche se il deficit attuale e 0.
    #
    # La headroom stretta e necessaria ma NON sufficiente: servono anche un
    # segnale quantitativo non-zero (guard anti zone-peso-0) e un regime
    # meteorologico che non sia "hot" (guard anti avvio in estate/primavera calda).
    zones_preheat_ok = False
    zones_preheat_skipped_reason: Optional[str] = None

    if zones_any_heat and demand.heat_headroom_min_c is not None:
        # (a) Headroom: zona piu vicina al bordo inferiore entro la soglia preheat.
        # Soglia default 0.4C: meno di mezzo grado di margine prima della violazione.
        headroom_ok = demand.heat_headroom_min_c <= float(
            getattr(cfg.zones_mpc, "preheat_headroom_c", 0.4)
        )

        # (b) Guard anti zone a peso=0 (Fix-A): almeno un segnale quantitativo
        # di domanda deve essere non-zero. Evita il falso positivo in cui una
        # zona di riferimento (weight=0, esclusa da heat_def_max e heat_cov)
        # con headroom esattamente al limite triggera il preheat con
        # heat_def_max=0, heat_cov=0%, heat_def_wmean=0.
        demand_nonzero = (heat_cov > 0.0) or (heat_def_wmean > 0.0)

        # (c) Guard regime caldo (Fix-E): in regime "hot" la T_smooth >= 22C
        # indica che la casa non perdera calore nel breve termine. Il modello
        # RC con t_out~=22C predice quasi zero deriva: il preheat sarebbe
        # fisicamente controproducente (riscaldare a T_int gia 23-24C).
        regime_allows_preheat = (regime_hint != "hot")

        if not headroom_ok:
            zones_preheat_skipped_reason = "headroom_too_large"
        elif not demand_nonzero:
            zones_preheat_skipped_reason = "demand_zero"
        elif not regime_allows_preheat:
            zones_preheat_skipped_reason = "regime_hot"
        else:
            zones_preheat_ok = True

    # (e) Profili assenza: in AWAY/VACATION non si preriscalda in anticipo.
    # La logica di recupero all'arrivo e gestita a livello superiore
    # (timer occupazione / trigger presenza).
    if profile in (HVACOperatingProfile.AWAY, HVACOperatingProfile.VACATION):
        if zones_preheat_ok:
            zones_preheat_skipped_reason = "profile_away_vacation"
        zones_preheat_ok = False

    # -- Fase 5: Flag finali (contratto verso ModeResolver) -----------------
    # Il ModeResolver non vede le condizioni intermedie: usa solo questi tre
    # flag insieme al contesto stagionale, finestre e vacanza.
    any_heat = bool(heat_sensible) or vmc_req_heat or (zones_any_heat and zones_preheat_ok)
    any_cool = bool(cool_sensible) or vmc_req_cool
    any_cool_or_dehum = any_cool or vmc_req_dehum

    return GatingResult(
        ctrl_aggr=ctrl_aggr,
        heat_thr_c=heat_thr,
        cool_thr_c=cool_thr,
        quorum_cov_req=quorum,
        heat_override=heat_override,
        heat_quorum_ok=heat_quorum_ok,
        heat_mean_ok=heat_mean_ok,
        cool_override=cool_override,
        cool_quorum_ok=cool_quorum_ok,
        cool_mean_ok=cool_mean_ok,
        heat_sensible=bool(heat_sensible),
        cool_sensible=bool(cool_sensible),
        zones_any_heat=zones_any_heat,
        zones_full_on_pct=float(zones_full_on_pct) if zones_full_on_pct is not None else None,
        zones_preheat_ok=bool(zones_preheat_ok),
        zones_preheat_skipped_reason=zones_preheat_skipped_reason,
        shoulder_heat_suppressed=bool(shoulder_heat_suppressed),
        zones_duty_avg_pct=float(zones_duty_avg_pct) if zones_duty_avg_pct is not None else None,
        zones_on_now_pct=float(zones_on_now_pct) if zones_on_now_pct is not None else None,
        zones_first_on_step=int(zones_first_on_step) if zones_first_on_step is not None else None,
        vmc_req_heat=vmc_req_heat,
        vmc_req_cool=vmc_req_cool,
        vmc_req_dehum=vmc_req_dehum,
        any_heat=bool(any_heat),
        any_cool=bool(any_cool),
        any_cool_or_dehum=bool(any_cool_or_dehum),
        heat_def_global_c=heat_def_global,
        heat_global_pdc_active=(bool(heat_override) if heat_def_global is not None else None),
        heat_pdc_on_thr_eff_c=float(_heat_pdc_on_thr),
    )
