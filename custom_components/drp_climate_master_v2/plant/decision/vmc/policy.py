from __future__ import annotations

import math
from typing import Optional

from ....domain.enums import HVACOperatingProfile
from ....plant.monitor.plant import PlantSnapshot
from ....helpers.psychrometric import dew_point_celsius
from ....helpers.utils import as_float, clamp

from ..config import VmcConfig
from ..contracts import VmcDemand

from .state import VmcState

class VmcPolicy:
    """Politica di controllo per la VMC (Ventilazione Meccanica Controllata).

    Classe di policy pura per la VMC Eneren RER020i: riceve i segnali di domanda
    già calcolati (deficit/surplus termici, DP indoor/outdoor, copertura cooling)
    e produce un ``VmcDemand`` con tutte le richieste da inviare al dispositivo.

    Non conosce i dettagli di attuazione Modbus né la struttura del ``PlantDecision``;
    il suo unico stato persistente è ``VmcState.dehum_on`` (isteresi deumidifica).
    Viene invocata dal ``PlantDecisionPlanner`` nella fase di VMC enrichment della
    pipeline a sette fasi.

    Responsibilities
    - resolve operative season bucket (winter/summer/shoulder)
    - resolve RH target from config (season + profile)
    - compute DP setpoint (psychrometric or fixed)
    - compute DP hysteresis thresholds
    - evaluate dehumidification feasibility (water coil vs vent-only)
    - apply hysteresis memory to decide dehumidification request
    - decide whether heat/cool boost requests are allowed
    """

    def __init__(self, cfg: VmcConfig, *, state: VmcState) -> None:
        """Inizializza la policy con la configurazione e lo stato persistente.

        Args:
            cfg: Configurazione VMC (soglie DP, RH target, parametri boost e
                 free cooling).
            state: Oggetto di stato condiviso con il ciclo precedente; contiene
                   ``dehum_on`` per l'isteresi anti-flapping della deumidifica.
        """
        self.cfg = cfg
        self._state = state

    # -----------------
    # High-level API
    # -----------------

    def compute(
        self,
        *,
        snapshot: PlantSnapshot,
        profile: HVACOperatingProfile,
        heat_def_max_c: float,
        heat_def_wmean_c: float,
        heat_cov: float,
        cool_sur_max_c: float,
        cool_sur_wmean_c: float,
        cool_cov: float,
        dp_dehum_c: Optional[float],
        dp_max_c: Optional[float],
        outdoor_dp_c: Optional[float],
        free_cool_feasible: bool = False,
        free_heat_feasible: bool = False,
        radiant_cooling_active: bool = False,
    ) -> VmcDemand:
        """Calcola la domanda VMC completa per il ciclo corrente.

        Flusso interno (in ordine):

        1. **Stagione operativa** — classifica il tick in winter/summer/shoulder.
        2. **T_ref e RH target** — temperatura interna di riferimento (global zone)
           e target di umidità relativa da configurazione stagionale.
        3. **Setpoint DP psicrometrico** — ``dp_sp_raw_c`` è il punto di rugiada
           corrispondente a (T_ref, RH_target): rappresenta il massimo DP ammissibile
           affinché l'aria interna non condensa sulle superfici fredde del radiante.
        4. **Quantizzazione ΔDP** — il dispositivo accetta ΔDP solo a step interi
           (1°C). Per non anticipare la soglia ON, il ΔDP comandato viene arrotondato
           *verso l'alto* (``math.ceil``), e il setpoint DP comandato viene corretto
           di conseguenza: ``dp_sp_cmd = dp_sp_raw + ddp_policy - ddp_cmd``.
           In questo modo ``on_thr = dp_sp_cmd + ddp_cmd`` rimane uguale all'intento
           originale indipendentemente dalla granularità del dispositivo.
        5. **Profilo contestuale radiante** — se il radiante è attivo o in domanda
           (``radiant_cooling_active=True``), viene usato il setpoint psicrometrico
           protettivo; altrimenti si usa ``dp_sp_max_c`` (profilo passivo/permissivo)
           per evitare falsi allarmi nei periodi di spalla in cui non vi sono superfici
           fredde su cui possa formarsi condensa.
        6. **Soglie ON/OFF deumidifica** — ``on_thr = dp_sp_cmd + ddp_cmd``,
           ``off_thr = on_thr - hysteresis``.
        7. **Fattibilità deumidifica** — acqua disponibile (batteria idraulica VMC)
           oppure DP outdoor sufficientemente basso da permettere ventilazione pura.
        8. **Isteresi deumidifica** — ``need_dehumidification`` applica la memoria
           del ciclo precedente per evitare flapping on/off.
        9. **Gate fisico deumidifica** — ``_dehum_condition_met`` blocca la richiesta
           se non vi è né cooling attivo né disagio igienico né rischio condensa critico.
        10. **Boost riscaldamento/raffreddamento VMC** — autorizzati solo se
            abilitati in configurazione e le soglie di deficit/surplus sono superate.
        11. **Free cooling/heating** — bypass recuperatore; mutuamente esclusivo con
            i boost idronici; disabilitato se è già attiva una richiesta di trattamento.

        Args:
            snapshot: Osservazione istantanea coerente dell'impianto.
            profile: Profilo operativo attivo (Eco, Comfort, ...).
            heat_def_max_c: Deficit termico massimo tra le zone [°C].
            heat_def_wmean_c: Deficit termico medio pesato [°C].
            heat_cov: Copertura riscaldamento (frazione zone in deficit, 0..1).
            cool_sur_max_c: Surplus termico massimo tra le zone [°C].
            cool_sur_wmean_c: Surplus termico medio pesato [°C].
            cool_cov: Copertura raffrescamento (frazione zone in surplus, 0..1).
            dp_dehum_c: Punto di rugiada robusto (percentile zone) per controllo
                        latente [°C]; preferito a dp_max per la stabilità numerica.
            dp_max_c: Punto di rugiada massimo tra le zone [°C]; usato come
                      fallback se dp_dehum_c è None.
            outdoor_dp_c: Punto di rugiada esterno [°C]; usato per valutare la
                          fattibilità della deumidifica per ventilazione.
            free_cool_feasible: Flag prodotto dal DemandSignalsBuilder: True se il
                                free cooling ventilativo è fattibile (delta T e DP
                                outdoor idonei, finestre chiuse).
            free_heat_feasible: Analogo per il free heating ventilativo.
            radiant_cooling_active: True se la pompa miscelatrice è attiva oppure
                                    se esiste domanda di cooling (cool_sur_max > 0
                                    o cool_cov > 0). Determina il profilo DP VMC.

        Returns:
            ``VmcDemand`` con tutti i setpoint e le richieste per il tick corrente.
        """
        operative = self.infer_operative_bucket(snapshot)

        # reference indoor temperature + RH target
        t_ref_c = float(self.get_indoor_reference_temp_c(snapshot))
        rh_target_pct = float(self.rh_target_pct(operative, profile))

        # dp setpoint + hysteresis thresholds
        # NOTE: the device may support ΔDP only in coarse steps (e.g. 1°C).
        # We preserve the intended ON threshold by adjusting the *commanded*
        # DP setpoint when quantizing ΔDP for the device.
        dp_sp_raw_c = float(self.compute_dp_setpoint_c_from(t_ref_c, rh_target_pct))
        ddp_policy = float(self.cfg.dehum.setpoint_ddp_c)
        step = max(1e-9, float(getattr(self.cfg.dehum, "ddp_device_step_c", 1.0)))
        if ddp_policy <= 0.0:
            ddp_cmd = 0.0
        else:
            # Quantize UP to avoid triggering dehumidification earlier than intended.
            ddp_cmd = step * math.ceil(ddp_policy / step - 1e-12)

        # Setpoint DP contestuale al radiante.
        #
        # Quando il radiante è ATTIVO (cooling fisico o in domanda): usa il target
        # psicrometrico (da RH% comfort), calibrato per proteggere le superfici fredde.
        #
        # Quando il radiante è INATTIVO: non esistono superfici fredde, quindi il rischio
        # di condensa è nullo. Il setpoint passivo (dp_sp_max_c = 15.0°C) porta la soglia
        # ON a ~16.0°C, ben sopra il DP primaverile romano tipico (14-15°C).
        # Questo elimina l'allarme falso su dashboard e device senza alterare la logica
        # di protezione quando il cooling è davvero in corso.
        # Il valore psicrometrico rimane in dp_sp_raw_c per diagnostica.
        if radiant_cooling_active:
            # Caso attivo: protezione condensazione -> soglia da RH target
            dp_sp_cmd_c = float(dp_sp_raw_c) + float(ddp_policy) - float(ddp_cmd)
            dp_sp_cmd_c = float(clamp(dp_sp_cmd_c, float(self.cfg.dehum.dp_sp_min_c), float(self.cfg.dehum.dp_sp_max_c)))
        else:
            # Caso passivo: nessuna superficie fredda -> soglia permissiva
            dp_sp_cmd_c = float(self.cfg.dehum.dp_sp_max_c)

        hyst = max(0.0, float(self.cfg.dehum.hysteresis_c))
        on_thr = float(dp_sp_cmd_c) + float(ddp_cmd)
        off_thr = float(on_thr) - hyst

        # Raw device request (best effort)
        vmc = getattr(snapshot, "vmc", None)
        raw_req_dehum = bool(getattr(vmc, "request_dehumidification", False)) if vmc else False

        # Feasibility
        dehum_feasible: Optional[bool] = None
        if bool(self.cfg.dehum.water_on_for_dehumid):
            dehum_feasible = True
        elif outdoor_dp_c is not None and dp_dehum_c is not None:
            headroom = float(self.cfg.dehum.outdoor_dp_headroom_c)
            dehum_feasible = float(outdoor_dp_c) <= (float(dp_dehum_c) - headroom)

        # DP control uses robust dp_dehum, fallback to dp_max
        dp_current = dp_dehum_c if dp_dehum_c is not None else dp_max_c
        need_dehum = self.need_dehumidification(dp_current, on_thr, off_thr)
        # Gate fisico: need_dehum è necessario ma non sufficiente.
        # La deumidifica è autorizzata solo se almeno una condizione fisica è vera.
        # raw_req_dehum è intenzionalmente escluso dal gate: il dispositivo
        # ha priorità di sicurezza autonoma e bypassa questo filtro.
        need_dehum = need_dehum and self._dehum_condition_met(
            snapshot=snapshot,
            dp_max_c=dp_max_c,
            cool_sur_max_c=cool_sur_max_c,
            cool_cov=cool_cov,
        )

        # Boost eligibility
        allow_heat = self.allow_heat_boost(snapshot, heat_def_max_c, heat_def_wmean_c, heat_cov)
        allow_cool = self.allow_cool_boost(snapshot, cool_sur_max_c, cool_sur_wmean_c, cool_cov)

        req_heat = bool(allow_heat)
        req_cool = bool(allow_cool)
        req_dehum = bool(need_dehum or raw_req_dehum) and (dehum_feasible is not False)
        req_water = bool(req_heat or req_cool or (req_dehum and bool(self.cfg.dehum.water_on_for_dehumid)))

        # Free cooling/heating ventilativo: bypass recuperatore.
        # Mutuamente esclusivo con req_heating/req_cooling (nessuna batteria idraulica).
        req_free_cool = bool(free_cool_feasible) and self.allow_free_cooling(snapshot, outdoor_dp_c, dp_max_c)
        req_free_heat = bool(free_heat_feasible) and self.allow_free_heating(snapshot)
        # Se il trattamento termico idronico è già richiesto, il free vent non si attiva.
        if req_heat or req_cool or req_dehum:
            req_free_cool = False
            req_free_heat = False

        return VmcDemand(
            dp_sp_c=float(dp_sp_cmd_c),
            ddp_cmd_c=float(ddp_cmd),
            dehum_on_thr_c=float(on_thr),
            dehum_off_thr_c=float(off_thr),
            dehum_feasible=dehum_feasible,
            dp_sp_raw_c=float(dp_sp_raw_c),
            req_heating=req_heat,
            req_cooling=req_cool,
            req_dehumidif=req_dehum,
            req_water=req_water,
            req_free_cooling=req_free_cool,
            req_free_heating=req_free_heat,
            operative_season=str(operative),
            rh_target_pct=float(rh_target_pct),
            t_ref_c=float(t_ref_c),
            raw_req_dehumidif=bool(raw_req_dehum),
            radiant_cooling_active=bool(radiant_cooling_active),
        )

    # -----------------
    # Pure-ish helpers
    # -----------------

    def infer_operative_bucket(self, snapshot: PlantSnapshot) -> str:
        """Classifica il tick corrente in uno dei tre bucket stagionali operativi.

        Mappa la stagione di runtime (da ``PlantSnapshot.season``) nei tre bucket
        usati dalla VMC policy: ``"winter"`` / ``"summer"`` / ``"shoulder"``.
        Il bucket ``shoulder`` copre primavera e autunno e si comporta in modo
        conservativo: non attiva trattamenti aggressivi né in caldo né in freddo.

        Returns:
            Stringa ``"winter"``, ``"summer"`` o ``"shoulder"``.
        """
        season = getattr(getattr(snapshot, "season", None), "season", None)
        season_val = getattr(season, "value", None)
        if season_val == "winter":
            return "winter"
        if season_val == "summer":
            return "summer"
        return "shoulder"

    def rh_target_pct(self, operative: Optional[str], profile: HVACOperatingProfile) -> float:
        """Restituisce il target di umidità relativa [%] per stagione e profilo.

        Delega interamente a ``VmcConfig.dehum.rh_target_pct``; non contiene logica
        propria per separare la configurazione dalla policy.

        Args:
            operative: Bucket stagionale (``"winter"`` / ``"summer"`` / ``"shoulder"``).
            profile: Profilo operativo attivo.

        Returns:
            Target RH in percentuale (es. 50.0).
        """
        return float(self.cfg.dehum.rh_target_pct(operative, profile))

    def get_indoor_reference_temp_c(self, snapshot: PlantSnapshot) -> float:
        """Restituisce la temperatura operativa indoor di riferimento [°C].

        Utilizza ``t_op`` (temperatura operativa ISO 7730 = media pesata tra T_aria e
        MRT) della zona globale aggregata come riferimento psicrometrico per il calcolo
        del setpoint DP. In assenza del dato, ricade su ``cfg.setpoint_t_c``.

        La temperatura operativa è preferita alla sola T_aria perché incorpora
        l'effetto radiante: con soffitto caldo o freddo la T_op è più rappresentativa
        della condizione percepita e della temperatura superficiale stimata.

        Returns:
            Temperatura operativa media indoor [°C], o setpoint fisso di fallback.
        """
        z = snapshot.global_indoor_zone
        if z is not None:
            for av in (getattr(z, "t_op", None), getattr(z, "temperature", None)):
                v = as_float(getattr(av, "value", None))
                if v is not None:
                    return float(v)
        return float(self.cfg.setpoint_t_c)

    def compute_dp_setpoint_c_from(self, t_c: float, rh_pct: float) -> float:
        """Calcola il setpoint di dew point [°C] per la protezione anti-condensa.

        Se ``cfg.dehum.dp_setpoint_from_psychrometrics`` è True, inverte la formula
        psicrometrica di Magnus-Tetens per ricavare il DP corrispondente alla coppia
        (T_ref, RH_target) tramite ``dew_point_celsius(T, RH)``.

        Il valore prodotto rappresenta il **massimo DP ammissibile** nell'aria indoor
        affinché le superfici a T_mandata_radiante non vadano in condensa: se
        DP_aria <= DP_sp, la superficie rimane asciutta a qualunque T_mandata
        superiore al DP_sp stesso.

        Se il calcolo psicrometrico non è abilitato (o lancia eccezione), viene usato
        il valore fisso ``cfg.dehum.setpoint_dp_c`` come fallback.

        Il risultato è clampato nell'intervallo [dp_sp_min_c, dp_sp_max_c] definito
        in configurazione per evitare setpoint fisicamente impossibili.

        Args:
            t_c: Temperatura operativa indoor di riferimento [°C].
            rh_pct: Target di umidità relativa [%].

        Returns:
            Setpoint DP [°C] clampato nei limiti di configurazione.
        """
        if bool(self.cfg.dehum.dp_setpoint_from_psychrometrics):
            try:
                dp = float(dew_point_celsius(float(t_c), float(rh_pct)))
            except Exception:
                dp = float(self.cfg.dehum.setpoint_dp_c)
        else:
            dp = float(self.cfg.dehum.setpoint_dp_c)
        return float(clamp(dp, float(self.cfg.dehum.dp_sp_min_c), float(self.cfg.dehum.dp_sp_max_c)))

    def need_dehumidification(self, dp_current_c: Optional[float], on_thr_c: float, off_thr_c: float) -> bool:
        """Valuta se la deumidifica è necessaria applicando isteresi anti-flapping.

        Implementa un trigger con soglie asimmetriche (Schmitt trigger):

        - **Attivazione**: DP_corrente > on_thr  ->  ``dehum_on = True``
        - **Mantenimento**: se già attiva, rimane True finché DP_corrente > off_thr
        - **Spegnimento**: DP_corrente <= off_thr  ->  ``dehum_on = False``

        L'isteresi (on_thr - off_thr) evita oscillazioni rapide della deumidifica
        quando il DP è vicino alla soglia, riducendo i cicli compressore VMC e il
        consumo energetico.

        Lo stato ``_state.dehum_on`` è l'unica memoria persistente della classe;
        viene aggiornato ad ogni chiamata.

        Se ``dp_current_c`` è None (sensore non disponibile), la deumidifica viene
        disattivata per sicurezza e lo stato resettato a False.

        Args:
            dp_current_c: Punto di rugiada indoor corrente [°C]; None se indisponibile.
            on_thr_c: Soglia di attivazione DP [°C] (= dp_sp_cmd + ddp_cmd).
            off_thr_c: Soglia di disattivazione DP [°C] (= on_thr - hysteresis).

        Returns:
            True se la deumidifica è richiesta in questo tick.
        """
        if dp_current_c is None:
            self._state.dehum_on = False
            return False
        cur = float(dp_current_c)
        prev = self._state.dehum_on
        if prev is True:
            keep = cur > float(off_thr_c)
            self._state.dehum_on = bool(keep)
            return bool(keep)
        turn_on = cur > float(on_thr_c)
        self._state.dehum_on = bool(turn_on)
        return bool(turn_on)

    def _dehum_condition_met(
        self,
        *,
        snapshot: PlantSnapshot,
        dp_max_c: Optional[float],
        cool_sur_max_c: float,
        cool_cov: float,
    ) -> bool:
        """Gate fisico: verifica se almeno una condizione giustifica la deumidifica.

        Questo gate viene applicato *dopo* l'isteresi DP (``need_dehumidification``):
        anche se il DP indoor supera la soglia, la deumidifica non viene autorizzata
        a meno che una delle tre condizioni fisiche sotto sia verificata.

        La logica evita di attivare il compressore VMC in primavera/autunno quando
        il DP è leggermente sopra soglia ma non esiste alcuna superficie fredda su
        cui possa formarsi condensa (soffitto a temperatura ambiente, nessun cooling).

        **Condizione A — Cooling attivo o imminente** (``cool_sur_max > 0`` o
        ``cool_cov > 0``): con il soffitto radiante a T_mandata < T_dp esiste una
        superficie fredda reale. Deumidificare preventivamente è corretto.

        **Condizione B — Disagio igienico assoluto** (UR_max > soglia, tipicamente
        67%): indipendente dal cooling. Oltre questa soglia l'aria è percepita come
        soffocante (ISO 7730) e aumenta il rischio biologico (muffe) su superfici
        parzialmente fredde (vetri, davanzali).
        Soglia: ``VmcDehumConfig.rh_dehum_absolute_threshold_pct`` (default 67%).

        **Condizione C — Rischio condensa su superfici passive** (DP_max > soglia
        critica, tipicamente 16.5°C): superfici che possono trovarsi sotto i 16°C
        anche senza cooling attivo (vetri notturni, superfici non isolate) sono a
        rischio. Funziona da guardrail pre-avvio estivo.
        Soglia: ``VmcDehumConfig.dp_dehum_critical_threshold_c`` (default 16.5°C).

        Args:
            snapshot: Osservazione istantanea usata per leggere UR per zona.
            dp_max_c: DP massimo indoor tra le zone [°C]; None se indisponibile.
            cool_sur_max_c: Surplus termico massimo tra le zone [°C].
            cool_cov: Copertura raffrescamento (frazione zone in surplus, 0..1).

        Returns:
            True se almeno una condizione è soddisfatta; False blocca la deumidifica.
        """
        # --- Condizione A: cooling attivo o imminente ---
        if float(cool_sur_max_c) > 0.0 or float(cool_cov) > 0.0:
            return True

        # --- Condizione B: disagio igienico assoluto ---
        rh_thr = float(getattr(self.cfg.dehum, "rh_dehum_absolute_threshold_pct", 67.0))
        rh_max: Optional[float] = None
        zones = getattr(snapshot, "indoor_zones", None) or {}
        for z in zones.values():
            rh_val = as_float(getattr(getattr(z, "humidity", None), "value", None))
            if rh_val is not None:
                rh_max = rh_val if rh_max is None else max(rh_max, rh_val)
        if rh_max is not None and rh_max > rh_thr:
            return True

        # --- Condizione C: rischio condensa su superfici passive ---
        dp_crit = float(getattr(self.cfg.dehum, "dp_dehum_critical_threshold_c", 16.5))
        if dp_max_c is not None and float(dp_max_c) > dp_crit:
            return True

        return False

    def allow_free_cooling(
        self,
        snapshot: PlantSnapshot,
        outdoor_dp_c: Optional[float],
        dp_max_c: Optional[float],
    ) -> bool:
        """Verifica le precondizioni locali per il free cooling ventilativo.

        Il free cooling (bypass recuperatore con aria esterna fredda) è una modalità
        di raffrescamento gratuito: la VMC immette aria esterna più fredda di quella
        interna senza attivare compressori.

        Questo metodo verifica solo le condizioni di *sicurezza e contesto* che la
        policy può valutare autonomamente: stato finestre, occupazione, e controllo
        DP dell'aria esterna (l'aria esterna non deve aggiungere umidità ->
        DP_outdoor < DP_indoor_max - margine).

        Il delta T (T_indoor - T_outdoor > soglia minima) non viene ricalcolato qui:
        è già valutato dal ``DemandSignalsBuilder`` nel flag ``free_cool_feasible``
        che il caller usa come condizione necessaria aggiuntiva, evitando
        duplicazione di logica tra strati.

        Fail-safe: se DP_outdoor è sconosciuto, la funzione restituisce False per
        non rischiare di peggiorare l'umidità interna.

        Args:
            snapshot: Usato per leggere stato finestre, vacanza, assenza prolungata.
            outdoor_dp_c: DP esterno [°C]; None -> fail-safe False.
            dp_max_c: DP indoor massimo [°C]; usato per il margine di sicurezza DP.

        Returns:
            True se le precondizioni locali sono soddisfatte.
        """
        # Guardie identiche ai boost esistenti
        if not bool(getattr(snapshot, "windows_close_state", True)):
            return False
        if bool(snapshot.presence_vacation):
            return False
        if bool(snapshot.presence_nobodysin):
            return False
        # Controllo DP: aria esterna non deve aggiungere umidità
        if outdoor_dp_c is not None and dp_max_c is not None:
            margin = float(getattr(self.cfg.dehum, "outdoor_dp_headroom_c", 2.0))
            if float(outdoor_dp_c) >= (float(dp_max_c) - margin):
                return False
        elif outdoor_dp_c is None:
            # DP esterno sconosciuto: fail-safe, non attivare
            return False
        # Il delta T è valutato dal caller tramite free_cool_feasible del demand
        return True

    def allow_free_heating(self, snapshot: PlantSnapshot) -> bool:
        """Verifica le precondizioni locali per il free heating ventilativo.

        Il free heating (bypass recuperatore con aria esterna calda) è utile in
        primavera/autunno quando l'esterno è più caldo dell'interno: la VMC
        immette calore gratuito senza attivare la batteria idraulica.

        Condizioni necessarie: finestre chiuse (aria controllata), assenza di
        vacanza, stagione non estiva (in estate l'aria esterna calda peggiorerebbe
        il comfort anziché migliorarlo).

        Come per ``allow_free_cooling``, il delta T minimo non viene ricalcolato
        qui: è responsabilità del caller tramite il flag ``free_heat_feasible``.

        Args:
            snapshot: Usato per leggere stato finestre, vacanza, stagione.

        Returns:
            True se le precondizioni locali sono soddisfatte.
        """
        if not bool(getattr(snapshot, "windows_close_state", True)):
            return False
        if bool(snapshot.presence_vacation):
            return False
        season_val = getattr(getattr(getattr(snapshot, "season", None), "season", None), "value", None)
        if season_val == "summer":
            return False
        return True

    def allow_heat_boost(self, snapshot: PlantSnapshot, heat_def_max_c: float, heat_def_wmean_c: float, heat_cov: float) -> bool:
        """Autorizza il boost di riscaldamento tramite batteria idraulica VMC.

        Il boost VMC integra il riscaldamento principale nei periodi di spalla:
        la VMC scalda l'aria di mandata con acqua calda dell'impianto radiante,
        aggiungendo capacità termica senza avviare un ciclo separato della PDC.

        Condizioni di attivazione (OR tra le due):

        - Deficit massimo zona >= ``cfg.boost.heat_def_max_thr_c``: zona singola
          molto fuori banda -> intervento immediato.
        - Deficit medio pesato >= ``cfg.boost.heat_def_wmean_thr_c`` E copertura
          >= 60%: disagio diffuso ma moderato -> intervento distribuito.

        Gate di sicurezza: boost non abilitato se ``cfg.boost.enabled=False``,
        finestre aperte (il ricambio naturale compensa) o casa vuota/in vacanza.

        Returns:
            True se il boost di riscaldamento VMC è autorizzato.
        """
        if not bool(self.cfg.boost.enabled):
            return False
        if not bool(snapshot.windows_close_state):
            return False
        if bool(snapshot.presence_vacation):
            return False
        if heat_def_max_c >= float(self.cfg.boost.heat_def_max_thr_c):
            return True
        if heat_def_wmean_c >= float(self.cfg.boost.heat_def_wmean_thr_c) and float(heat_cov) >= 0.6:
            return True
        return False

    def allow_cool_boost(self, snapshot: PlantSnapshot, cool_sur_max_c: float, cool_sur_wmean_c: float, cool_cov: float) -> bool:
        """Autorizza il boost di raffrescamento tramite batteria idraulica VMC.

        Simmetrico ad ``allow_heat_boost``: la VMC raffredda l'aria di mandata con
        acqua fredda dell'impianto, integrando il raffreddamento radiante o
        sostituendolo nei periodi di spalla in cui il radiante non è ancora attivo
        ma il surplus termico indoor è già significativo.

        Condizioni di attivazione (OR tra le due):

        - Surplus massimo zona >= ``cfg.boost.cool_sur_max_thr_c``: zona singola
          molto sopra banda -> intervento immediato.
        - Surplus medio pesato >= ``cfg.boost.cool_sur_wmean_thr_c`` E copertura
          >= 60%: disagio diffuso ma moderato -> intervento distribuito.

        Gate di sicurezza: boost non abilitato se ``cfg.boost.enabled=False``,
        finestre aperte o casa vuota/in vacanza.

        Returns:
            True se il boost di raffrescamento VMC è autorizzato.
        """
        if not bool(self.cfg.boost.enabled):
            return False
        if not bool(snapshot.windows_close_state):
            return False
        if bool(snapshot.presence_vacation):
            return False
        if cool_sur_max_c >= float(self.cfg.boost.cool_sur_max_thr_c):
            return True
        if cool_sur_wmean_c >= float(self.cfg.boost.cool_sur_wmean_thr_c) and float(cool_cov) >= 0.6:
            return True
        return False
