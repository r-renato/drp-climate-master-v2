"""Costanti di configurazione del modulo comfort-band (ISO 7730 PMV/PPD).

Questo file raccoglie **tutte** le costanti numeriche usate dal modulo
``comfort_band``: tabelle di lookup, valori seed dei parametri termoigrometrici,
soglie di policy e limiti difensivi fisici.

Struttura
---------
- Sezione A: modello velocità aria (turbulenza VMC su radiante a soffitto)
- Sezione B: draft robustness (protezione dal discomfort da corrente)
- Sezione C: abbigliamento (CLO) — valori base, correzioni per profilo/stagione
- Sezione D: metabolismo (MET) — overrides per profilo operativo
- Sezione E: target PMV e aggressività controllo per profilo operativo
- Sezione F: zona climatica italiana → CLO invernale

Cosa NON è in questo file
-------------------------
- Coefficienti della norma ISO 7730 di Fanger (immutabili per definizione fisica):
  sono embedded in ``calculator.pmv_ppd()`` e non devono essere estratti.
- Parametri numerici del solver bisection (``t_min``, ``t_max``, ``max_iter``):
  sono parametri algoritmici del metodo numerico, non configurazione HVAC.
- Costanti di conversione termodinamica (273.15 K↔°C, 58.15 met→W/m², …):
  fanno parte dell'implementazione fisica della norma.
- ``ConfortPolicyConfig`` (dataclass runtime): resta in ``policy_layer.py``
  perché trasporta la configurazione a runtime; i suoi default fanno riferimento
  alle costanti di questo file.

Riferimenti normativi
---------------------
- ISO 7730:2005 — Ergonomics of the thermal environment.
- ASHRAE 55-2020 — Thermal Environmental Conditions for Human Occupancy.
- DPR 412/1993 — Regolamento italiano zone climatiche A..F (gradi-giorno).
- UNI EN 15251:2008 / EN 16798-1:2019 — Criteri di qualità dell'aria interna.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Usato solo per type hint; a runtime le dict sono costruite da _build_*().
    from ....domain.enums import HVACOperatingProfile
    from .model import ClimateZoneIT


# =============================================================================
# SEZIONE A — Modello velocità aria (m/s) per step VMC 0..5
# =============================================================================
#
# Razionale termotecnico (radiante a soffitto + VMC)
# ---------------------------------------------------
# In un impianto radiante a soffitto abbinato a VMC, la velocità dell'aria in
# ambiente è quasi interamente determinata dalla portata della VMC: non esiste
# convezione forzata dal terminale radiante.
#
# I valori "BEST" rappresentano la velocità media attesa al centro zona per
# ogni step VMC; i valori "HI" rappresentano il 95° percentile (worst-case
# per il draft discomfort, ISO 7730 Annex A).
#
# Living vs altri ambienti
# ------------------------
# Il soggiorno ha volumi più grandi e aperture verso corridoio/cucina che
# aumentano la dispersione del getto VMC → velocità medie più alte a parità
# di step (tabella LIVING). Camere e bagni hanno volumi ridotti, spesso senza
# linea visiva diretta con la bocchetta VMC → curve più basse (tabella OTHER).
#
# Le tuple sono indicizzate per step (0=off/minimo … 5=massimo).
# Calibrate sull'impianto Eneren RER020i in abitazione di media metratura
# romana (zona climatica D, altitudine ~30 m).

V_AIR_BEST_LIVING: tuple[float, ...] = (0.05, 0.07, 0.09, 0.11, 0.13, 0.15)
"""Velocità aria media (m/s) in zona living per step VMC 0..5.

A step 0 (VMC spento / minimo) si assume 0.05 m/s per convezione naturale
residua (moto browniano termico degli strati d'aria). A step 5 si raggiungono
0.15 m/s, ancora ben al di sotto della soglia ISO 7730 Cat. A (0.16 m/s per
temperatura operante estiva di 26 °C con abbigliamento 0.5 clo).
"""

V_AIR_BEST_OTHER: tuple[float, ...] = (0.05, 0.06, 0.07, 0.08, 0.09, 0.10)
"""Velocità aria media (m/s) in zone non-living (camere, bagni) per step 0..5.

Valori più bassi rispetto a LIVING: volumi ridotti e assenza di percorsi
d'aria diretti dalla bocchetta VMC. A step 5 si rimane a 0.10 m/s, compatibile
con ISO 7730 Cat. A in riscaldamento (limite 0.12 m/s a 20 °C).
"""

V_AIR_HI_LIVING: tuple[float, ...] = (0.05, 0.092, 0.134, 0.176, 0.218, 0.260)
"""Velocità aria al 95° percentile (m/s) in zona living per step 0..5.

Usata per il calcolo del bound freddo della comfort band (t_op_min) in inverno:
con velocità alta il rischio draft è maggiore, quindi la temperatura minima di
comfort sale (la band si restringe verso il basso).
A step 5 si arriva a 0.26 m/s: al limite della soglia ISO 7730 Cat. B in
riscaldamento (0.21 m/s a 20 °C), giustificato dalla presenza di occupanti
in zona soggiorno che accettano ventilazione più vivace in estate.
"""

V_AIR_HI_OTHER: tuple[float, ...] = (0.05, 0.074, 0.098, 0.122, 0.146, 0.170)
"""Velocità aria al 95° percentile (m/s) in zone non-living per step 0..5.

Curva più conservativa: nelle camere da letto i residenti sono fermi o
sdraiati (met < 1.0), quindi la sensitività al draft è maggiore. A step 5
si raggiungono 0.17 m/s, entro il limite Cat. B in raffrescamento.
"""

V_AIR_LO_DEFAULT: float = 0.05
"""Velocità aria lower-bound di default (m/s) — convezione naturale residua.

Rappresenta il minimo fisico plausibile in un ambiente chiuso senza ventilazione
forzata attiva. ISO 7730 suggerisce 0.05 m/s come valore di progetto minimo
per calcoli PMV in ambienti statici.
"""

# Clamp difensivi indoor (limiti fisici assoluti per qualsiasi configurazione)
V_AIR_BEST_CLAMP: float = 0.60
"""Velocità aria 'best' massima ammessa (m/s) — limite difensivo assoluto.

0.60 m/s è il limite superiore della categoria ISO 7730 per uso residenziale
(velocità oltre le quali il PMV non può essere usato come indice affidabile
senza modelli di turbolenza avanzati). In nessuno scenario si dovrebbero
raggiungere queste velocità con una VMC residenziale.
"""

V_AIR_HI_CLAMP: float = 0.80
"""Velocità aria 'hi' (95° pct.) massima ammessa (m/s) — limite difensivo assoluto.

Oltre 0.8 m/s si entra nel regime di turbolenza intensa: i termini convettivi
del bilancio Fanger perdono di accuratezza e il rischio draft diventa
predominante sull'intero comfort. Clamp di sicurezza del solver.
"""

V_AIR_LO_CLAMP: float = 0.30
"""Velocità aria lower-bound massima ammessa (m/s) — limite difensivo assoluto.

Impone che il lower-bound non superi 0.3 m/s: oltre questa soglia anche il
bound 'best' rischia di essere dominato dal draft piuttosto che dalla
temperatura operante, rendendo il PMV instabile.
"""


# =============================================================================
# SEZIONE B — Draft robustness (α): blending v_best → v_hi per t_op_min
# =============================================================================
#
# Razionale termotecnico
# ----------------------
# Il bound freddo della comfort band (t_op_min) dipende dalla velocità aria
# assunta: con aria più mossa il corpo perde calore per convezione → la
# temperatura di comfort minima sale.
# Il parametro α (0..1) interpola tra v_best (conservativo, bassa turbolenza)
# e v_hi (caso peggiore, alta turbolenza):
#
#     v_draft = v_best + α * (v_hi - v_best)
#
# α=0 → si usa v_best per calcolare t_op_min (massima larghezza banda).
# α=1 → si usa v_hi per calcolare t_op_min (minima larghezza banda, più
#         conservativo verso il freddo).
#
# Valori di default calibrati per impianto radiante a soffitto con VMC:
# - Living: α=0.55. Ambienti aperti con maggiore moto d'aria naturale;
#   si protegge di più dal draft perché gli occupanti sono seduti o fermi
#   per periodi prolungati (sensibilità alta al discomfort da corrente).
# - Altri ambienti (camere, bagni): α=0.35. Volumi ridotti, moto d'aria
#   più uniforme; si accetta un bound freddo leggermente più basso.

T_OP_MIN_SLEEP_FLOOR_C: float = 17.0
"""Floor assoluto di T_op minima in modalità SLEEP in stagione invernale (°C).

Il modello PMV con CLO=2.0 (pigiama + piumino) calcola comfort già a 15–16°C,
ma lasciare la stanza sotto 17°C in inverno è inaccettabile per sicurezza
e comfort reale (persone vulnerabili, bambini, variabilità individuale).
Questo floor è indipendente dal PMV: si applica sempre in SLEEP invernale
come hard cap sulla banda calcolata.
"""

T_OP_MIN_SLEEP_CAP_SHOULDER_C: float = 20.0
"""Cap superiore di t_op_min per profilo SLEEP in stagione di mezza stagione (°C).

Radice del problema: ISO 7730 è validato per met >= 0.8. Usando met=0.70 (sonno)
fuori dal dominio della norma, la bisection PMV produce t_op_min artificiosa
(~23.7°C in shoulder) che porta il sistema a pianificare riscaldamento notturno
inutile quando la stanza è a 23°C in aprile/ottobre.

Fisicamente: 20°C è il limite superiore dell'intervallo ottimale per il sonno
(Muzet et al. 1984; ASHRAE 55). Una t_op_min > 20°C in shoulder season significa
che il sistema riscalda per mantenere temperature notturne che la letteratura
scientifica considera già calde per dormire.

Implementazione: cap applicato come post-processing in compute_many() sul
ComfortBandResult restituito da compute_single(), preservando PMV/PPD originali
(utili per commissioning) e ricalcolando solo il campo ok.
Inattivo se t_out < T_OP_MIN_SLEEP_CAP_T_OUT_C (protezione autunno freddo).
Inattivo in winter (coperto dal floor T_OP_MIN_SLEEP_FLOOR_C) e summer.
"""

T_OP_MIN_SLEEP_CAP_T_OUT_C: float = 12.0
"""Temperatura esterna minima per attivare il cap shoulder SLEEP (°C).

Sotto 12°C la dispersione termica è abbastanza alta da giustificare un target
notturno più conservativo. Questo guardrail esclude automaticamente le notti
di autunno inoltrato/novembre freddo in cui la stagione operativa potrebbe
ancora essere classificata come shoulder ma il comfort notturno richiede
un target più alto di 20°C.
Sopra 12°C (aprile mite, ottobre mite): il cap è attivo.
Sotto 12°C (novembre freddo): cap inattivo, sistema usa t_op_min da PMV.
"""

DRAFT_ALPHA_LIVING: float = 0.55
"""Alpha di draft robustness per zone living (soggiorno, salotto).

Vedi docstring di sezione B. Valore più alto = bound freddo più conservativo
= banda comfort più stretta verso il basso.
"""

DRAFT_ALPHA_OTHER: float = 0.35
"""Alpha di draft robustness per zone non-living (camere, bagni).

Valore più basso rispetto a LIVING perché il moto d'aria è più uniforme e
gli occupanti sono spesso in movimento (bagno) o coperti (camera da letto).
"""

DRAFT_ALPHA_SLEEP_DELTA: float = -0.10
"""Riduzione dell'alpha di draft in modalità SLEEP.

Razionale: di notte la VMC è tipicamente a step basso (1..2); il rischio
draft reale è minimo. Ridurre l'alpha abbassa il bound freddo di t_op_min,
prevenendo l'attivazione prematura del riscaldamento durante le ore notturne
(riduzione cicli brevi, risparmio energetico).
Delta applicato come:  α = max(DRAFT_ALPHA_SLEEP_MIN, α - |DELTA|)
"""

DRAFT_ALPHA_SLEEP_MIN: float = 0.05
"""Valore minimo di alpha in modalità SLEEP (floor di sicurezza).

Assicura che il draft non venga completamente ignorato anche di notte,
mantenendo una protezione minima residua dal discomfort da corrente.
"""

DRAFT_ALPHA_HIGH_SPEED_DELTA: float = -0.05
"""Riduzione alpha per VMC a passo alto (≥ VMC_SPEED_THR_DRAFT) in ambienti non-living.

A velocità alte la VMC domina già il campo di velocità; applicare l'alpha
pieno sovrastimarebbe il contributo della turbolenza naturale. Riduzione
conservativa per evitare che t_op_min risulti troppo alta (over-heating).
"""

VMC_SPEED_THR_DRAFT: int = 4
"""Soglia step VMC per attivare DRAFT_ALPHA_HIGH_SPEED_DELTA in ambienti non-living.

Al di sopra di questa soglia la VMC è in funzionamento ad alta portata;
la turbolenza indotta è già rappresentata nelle tabelle V_AIR_HI_OTHER,
quindi l'ulteriore correzione sull'alpha evita doppio conteggio.
"""

VMC_SPEED_THR_LIVING_HI_SCALE: int = 3
"""Soglia step VMC per attivare lo scaling v_hi in zona living.

In modalità living con step ≥ 3 la VMC contribuisce sensibilmente alla
ventilazione percepita; si applica il fattore living_high_speed_hi_scale
(> 1.0) per riflettere la maggiore dispersione in ambiente aperto.
"""

# --- Fattori di scaling v_hi per alta portata VMC ---
V_AIR_HI_SCALE_NON_LIVING_HIGH_SPEED: float = 0.90
"""Scaling v_hi per ambienti non-living a step VMC ≥ VMC_SPEED_THR_DRAFT.

Valore < 1.0: riduce la velocità percepita al 95° pct. per evitare che
il modello sovrastimi il draft in ambienti piccoli (camere, bagni) dove
la bocchetta VMC è lontana dalla zona di permanenza.
0.90 = riduzione del 10% rispetto alla tabella V_AIR_HI_OTHER.
"""

V_AIR_HI_SCALE_LIVING_HIGH_SPEED: float = 1.05
"""Scaling v_hi per zona living a step VMC ≥ VMC_SPEED_THR_LIVING_HI_SCALE.

Valore > 1.0: amplifica leggermente la velocità al 95° pct. in ambienti
aperti (soggiorno) dove la dispersione del getto VMC è maggiore per via
dei volumi più grandi e delle aperture su altri ambienti.
1.05 = aumento del 5% rispetto alla tabella V_AIR_HI_LIVING.
"""


# =============================================================================
# SEZIONE C — Abbigliamento (CLO): valori base e correzioni
# =============================================================================
#
# Razionale termotecnico
# ----------------------
# Il valore CLO (1 clo = 0.155 m²·K/W) rappresenta la resistenza termica
# complessiva dell'abbigliamento. È il parametro che più influenza il PMV
# invernale: un errore di 0.20 clo si traduce in ~0.3 PMV units, equivalente
# a spostare il setpoint di ~1 °C.
#
# Valori base stagionali (riferimento ISO 7730 / ASHRAE 55):
# - Estate (summer): 0.50 clo — abbigliamento leggero (T-shirt, pantaloni corti)
# - Mezza stagione (shoulder): 0.70 clo — abbigliamento misto
# - Inverno (winter): 1.00 clo — abbigliamento da interno invernale (maglione, pantaloni)
#
# Le correzioni per zona climatica (sezione F) raffinano il valore invernale
# base tenendo conto delle abitudini di abbigliamento nelle diverse aree d'Italia
# (Zone A/B meridionali → clo più basso; Zone E/F alpine → clo più alto).

# --- Valori CLO base ---
CLO_BASE_SUMMER: float = 0.50
"""CLO base stagione estiva (abbigliamento leggero — ISO 7730, ASHRAE 55 Tab. 5.2.2)."""

CLO_BASE_SHOULDER: float = 0.82
"""CLO base mezza stagione (mix primavera/autunno — interpolazione empirica)."""

CLO_BASE_WINTER: float = 1.00
"""CLO base stagione invernale (abbigliamento da interno — ISO 7730 Tab. B.1)."""

# --- MET base ---
MET_BASE: float = 1.10
"""MET base per occupante standard in abitazione (attività sedentaria leggera).

1.1 met = attività sedentaria con occasionali spostamenti (ISO 7730 Tab. A.1:
seduto tranquillo = 1.0 met; in piedi tranquillo = 1.2 met; si usa 1.1 come
compromesso per abitazione residenziale con mix di attività).
"""

MET_WORK_DEFAULT: float = 0.0
"""Componente di lavoro meccanico esterno (wme) — zero per uso residenziale.

In Fanger, wme rappresenta il lavoro meccanico compiuto dall'occupante
sull'ambiente (es. pedalare). Per uso residenziale vale sempre 0.
"""

# --- Correzioni CLO per modalità operativa ---

CLO_SLEEP_WINTER_DELTA: float = 0.95
"""Delta CLO aggiunto in modalità SLEEP durante la stagione invernale.

Razionale: di notte gli occupanti sono a letto con coperte pesanti. Il CLO
complessivo (abbigliamento da notte + coperte) raggiunge facilmente 1.8..2.0 clo
(pigiama leggero 0.20 + coperta media 0.80 + piumino 0.75 = ~1.75 clo).
Il delta +0.95 porta il CLO invernale base (1.05 per zona D) a ~2.00, coerente
con le indicazioni di EN 16798-1 per ambienti di riposo notturno.
Limite superiore garantito da CLO_CAP_SLEEP.
"""

CLO_SLEEP_SHOULDER_DELTA: float = 0.70
"""Delta CLO aggiunto in modalità SLEEP durante la stagione di mezza stagione (shoulder).

Razionale: in aprile/ottobre a Roma gli occupanti usano comunque una coperta
o un piumino leggero. Senza questo delta, il modello tratta la persona dormiente
come seduta in abbigliamento da casa (clo~0.82), producendo PMV~-0.4 a 23 gradi
e un deficit spurio che porta il planner a pianificare riscaldamento notturno.

Composizione: pigiama leggero (0.20) + coperta primaverile (0.60-0.75)
-> clo shoulder totale ~ 0.82 + 0.70 = 1.52, PMV a 23 gradi ~ +0.1.
Limite superiore garantito da CLO_CAP_SLEEP.
"""

CLO_AWAY_VACATION_WINTER_DELTA: float = -0.10
"""Delta CLO sottratto in modalità AWAY/VACATION durante l'inverno.

Razionale: in assenza di occupanti non ha senso mantenere il riscaldamento
calibrato per abbigliamento pesante. Il clo ridotto sposta la comfort band
verso temperature più basse, consentendo al sistema di risparmiare energia
pur mantenendo una soglia di protezione dell'impianto e dell'immobile.
"""

CLO_AWAY_VACATION_WINTER_FLOOR: float = 0.70
"""Valore CLO minimo in modalità AWAY/VACATION in inverno.

Impedisce che la riduzione porti il clo a valori fisicamente non plausibili
per un ambiente riscaldato (non si scende sotto l'abbigliamento da mezza
stagione, che è il minimo sensato per calcolo PMV su impianto attivo).
"""

CLO_CAP_SLEEP: float = 2.40
"""Cap CLO massimo in modalità SLEEP.

2.4 clo rappresenta il valore massimo plausibile per abbigliamento da notte
con piumone pesante (pigiama + coperta invernale spessa). Oltre questo valore
il calcolo PMV perde di accuratezza perché il modello Fanger è stato validato
per CLO < 2.0 clo (ISO 7730 §A.5).
"""

CLO_CAP_DEFAULT: float = 1.60
"""Cap CLO massimo per tutti i profili non-SLEEP.

1.6 clo è il massimo plausibile per abbigliamento da interno in pieno inverno
(cappotto leggero indoor); valori superiori indicherebbero errori di
configurazione o stagionalità mal classificata.
"""

# --- Cold snap: interpolazione CLO shoulder → winter ---
COLD_SNAP_CLO_FRACTION: float = 0.40
"""Frazione di interpolazione CLO in caso di cold_snap (stagione shoulder).

Quando il classificatore ML segnala ``cold_snap=True`` in mezza stagione
(giornata insolitamente fredda rispetto al prototipo stagionale), gli
occupanti si vestono più pesante. Il CLO viene interpolato tra il valore
base shoulder e quello invernale:

    clo_cold_snap = clo_shoulder + FRACTION * (clo_winter - clo_shoulder)

Con FRACTION=0.40 e clo_shoulder=0.70, clo_winter=1.05 (zona D):
    clo_cold_snap = 0.70 + 0.40 * (1.05 - 0.70) = 0.84 clo

Valori utili: 0.25 (variazione debole) … 0.60 (variazione forte).
0.40 rappresenta un compromesso: le persone si vestono un po' più pesante
ma non raggiungono il livello pieno invernale in una giornata transitoria.
"""

# --- S4: interpolazione CLO shoulder -> summer/winter con progresso stagionale ---
CLO_SHOULDER_RAMP_ENABLED: bool = False
"""Abilita l'interpolazione progressiva del CLO base in stagione shoulder (S4).

Se True, il CLO shoulder non è più fisso a ``CLO_BASE_SHOULDER`` per tutta la
stagione: viene interpolato linearmente verso il valore target (estate o inverno)
man mano che il progresso stagionale supera ``CLO_SHOULDER_RAMP_START``.

Disabilitare per tornare al comportamento pre-S4 (CLO fisso per tutta la shoulder).
"""

CLO_SHOULDER_RAMP_START: float = 50.0
"""Progresso stagionale (%) oltre il quale inizia la rampa CLO shoulder (S4).

Sotto questa soglia il CLO rimane al valore base shoulder invariato.
Sopra questa soglia il CLO scala linearmente verso il target stagionale:
  - spring shoulder -> CLO_BASE_SUMMER  (le persone si vestono sempre più leggero)
  - autumn shoulder -> CLO invernale per zona climatica (sempre più pesante)

La rampa è lineare tra (ramp_start, CLO_base) e (100%, CLO_target):

    blend = max(0, (progress - ramp_start) / (100 - ramp_start))
    clo_eff = CLO_base + blend * (CLO_target - CLO_base)

Esempio spring, progress=77.2%, ramp_start=50%:
    blend = (77.2-50)/(100-50) = 0.544
    clo_eff = 0.82 + 0.544*(0.50-0.82) = 0.646 clo

Fisica: a metà stagione (progress=50%) l'abbigliamento è ancora da mezza stagione.
Oltre il 50% la transizione verso l'estate/inverno è percepibile nel vestiario.
Default 50.0%: calibrato su clima mediterraneo (Roma, zona D).
"""


# =============================================================================
# SEZIONE D — Metabolismo (MET): overrides per profilo operativo
# =============================================================================
#
# Razionale termotecnico
# ----------------------
# Il MET influenza l'equazione di Fanger attraverso la produzione metabolica M
# (M = met × 58.15 W/m²). Un errore di 0.1 met causa uno scostamento PMV di
# circa 0.10..0.15 unità, equivalente a ~0.5 °C di setpoint.
#
# Gli override per profilo riflettono il livello di attività atteso:
# - Base (1.10 met): normale attività residenziale — seduto, in piedi, breve
#   deambulazione (media ISO 7730).
# - SLEEP (0.70 met): persona dormiente. ISO 8996 Tab. A.1 indica 0.70 met
#   per sonno in posizione orizzontale.
# - AWAY/VACATION (1.00 met): casa vuota ma si usa met=1.00 per mantenere un
#   riferimento neutro; la vera riduzione dell'energia avviene tramite PMV
#   center/band spostati (sezione E) e CLO ridotto (sezione C).

MET_SLEEP: float = 0.70
"""MET in modalità SLEEP — persona dormiente.

ISO 8996 Tab. A.1: sonno = 0.70 met (posizione orizzontale, metabolismo basale).
Valore precedente (0.90, "attività sedentaria") era termofisicamente errato:
produceva un deficit sistematico di ~3°C su tutte le zone notturne, portando
il planner a pianificare riscaldamento non necessario durante la notte.

Correzione: 0.70 met -> con coperte (clo shoulder/winter), PMV a 23°C ~= +0.1,
nessun deficit spurio.
"""

MET_AWAY_VACATION: float = 1.00
"""MET in modalità AWAY/VACATION — abitazione non occupata.

Non ha significato fisico diretto (non ci sono occupanti), ma il valore
viene usato come riferimento neutro per calcolare la comfort band del
controllo di protezione (evitare temperature estreme dell'impianto).
"""


# =============================================================================
# SEZIONE E — Target PMV e aggressività controllo per profilo operativo
# =============================================================================
#
# Razionale termotecnico (ISO 7730 + radiante a soffitto)
# -------------------------------------------------------
# Il PMV (Predicted Mean Vote, scala -3..+3) rappresenta il voto medio di
# comfort termico di un grande gruppo di occupanti:
#   -3 = molto freddo, 0 = neutro, +3 = molto caldo.
#
# ISO 7730 definisce tre categorie di qualità:
#   Cat. A: -0.2 ≤ PMV ≤ +0.2 (PPD < 6%)
#   Cat. B: -0.5 ≤ PMV ≤ +0.5 (PPD < 10%)
#   Cat. C: -0.7 ≤ PMV ≤ +0.7 (PPD < 15%)
#
# Per impianti radiantiа soffitto occorre considerare:
# 1) Alta inerzia termica: oscillazioni lente → si può usare una banda più
#    stretta (Cat. A/B) senza rischio di over-cycling.
# 2) Asimmetria MRT: il soffitto caldo/freddo crea una radiant temperature
#    asymmetry che PMV non cattura completamente → centro PMV leggermente
#    negativo (-0.05) per compensare la percezione di calore proveniente
#    dall'alto in riscaldamento.
# 3) Estate: con soffitto freddo e alta umidità, centrare PMV a 0 tende a
#    causare over-cooling → centro ECO spostato verso +0.10 in estate.
#
# Le tabelle sono separate (MODE_PMV_CENTER, MODE_PMV_BAND) per consentire
# override selettivi nel commissioning (es. allargare solo la banda AWAY senza
# toccare il centro).

# Importazione lazy per evitare circular import a livello di modulo.
# Le dict vengono costruite a runtime (prima importazione del modulo).
def _build_pmv_tables() -> tuple[
    "dict[HVACOperatingProfile, float]",
    "dict[HVACOperatingProfile, float]",
    "dict[HVACOperatingProfile, float]",
]:
    """Costruisce le tabelle PMV e aggressività alla prima importazione del modulo.

    Separata in funzione per evitare import circolari a livello di modulo:
    ``HVACOperatingProfile`` è in ``domain.enums`` che non importa da qui.
    """
    from ....domain.enums import HVACOperatingProfile  # noqa: PLC0415

    pmv_center: dict[HVACOperatingProfile, float] = {
        # Radiante a soffitto: centro -0.05 (leggermente fresco).
        # Compensa l'asimmetria MRT da soffitto caldo che aggiunge calore
        # radiante percepito senza aumentare T_aria → PMV effettivo più alto
        # del calcolato se si usa centro=0. Un bias di -0.05 riduce
        # l'overshooting sui cicli lunghi tipici dei sistemi radianti.
        HVACOperatingProfile.COMFORT: -0.05,

        # BOOST: recupero rapido dopo assenza / prefissato mattutino.
        # Centro neutro: si vuole raggiungere il comfort ISO Cat. B
        # nel minor tempo possibile, senza penalizzare né caldo né freddo.
        HVACOperatingProfile.BOOST: 0.00,

        # ECO: leggero bias verso il fresco (-0.10) per ridurre consumi.
        # In inverno la banda è appena sotto il neutro (più freddo tollerato);
        # in estate il nudge summer_eco (PMV_SUMMER_ECO_NUDGE_DELTA) sposta
        # il centro verso +0.10 per ridurre il raffreddamento.
        HVACOperatingProfile.ECO: -0.10,

        # SLEEP: spostato verso il fresco (-0.40). Durante il sonno la
        # produzione metabolica cala (MET=0.90) e il corpo preferisce
        # temperature più basse: la letteratura indica PMV ottimale notturno
        # tra -0.5 e -0.3 (Fanger & Langkilde, 1975). -0.40 è un compromesso
        # che include la variabilità individuale.
        HVACOperatingProfile.SLEEP: -0.40,

        # AWAY/VACATION: centro molto spostato (-0.60) con banda larga.
        # L'impianto è in modalità "protezione": non si punta al comfort
        # degli occupanti (assenti) ma a evitare temperature estreme che
        # danneggino impianti o generino condensa. La banda larga (1.20)
        # minimizza i cicli di avvio.
        HVACOperatingProfile.AWAY: -0.60,
        HVACOperatingProfile.VACATION: -0.60,
    }

    pmv_band: dict[HVACOperatingProfile, float] = {
        # COMFORT/ECO/SLEEP: bande ISO 7730 Cat. A/B (±0.2/±0.5).
        # Radiante a soffitto tollera bande strette grazie all'inerzia.
        HVACOperatingProfile.COMFORT: 0.30,   # ≈ Cat. A/B ibrida (PPD < 7%)
        HVACOperatingProfile.BOOST: 0.50,     # Cat. B (PPD < 10%) — recupero
        HVACOperatingProfile.ECO: 0.35,       # leggermente più larga di COMFORT
        HVACOperatingProfile.SLEEP: 0.45,     # Cat. B+ — adatta alla variabilità notturna
        HVACOperatingProfile.AWAY: 1.20,      # banda larga — protezione impianto
        HVACOperatingProfile.VACATION: 1.20,  # come AWAY
    }

    ctrl_aggressiveness: dict[HVACOperatingProfile, float] = {
        # Fattore moltiplicativo sull'azione di controllo (setpoint WOT, portata pompa).
        # Decoupled dal PMV: cambia la velocità di risposta dell'impianto senza
        # modificare l'obiettivo di comfort (PMV center/band).
        # 1.0 = comportamento nominale della curva climatica.
        HVACOperatingProfile.COMFORT: 1.00,
        HVACOperatingProfile.BOOST: 1.35,     # +35% → recupero rapido
        HVACOperatingProfile.ECO: 0.85,       # -15% → risparmio energetico
        HVACOperatingProfile.SLEEP: 0.75,     # -25% → azione lenta, cicli rari
        HVACOperatingProfile.AWAY: 0.50,      # -50% → protezione minima
        HVACOperatingProfile.VACATION: 0.50,
    }

    return pmv_center, pmv_band, ctrl_aggressiveness


# Istanziazione modulo-level (eseguita una sola volta all'import)
_pmv_center, _pmv_band, _ctrl_aggressiveness = _build_pmv_tables()

MODE_PMV_CENTER: "dict[HVACOperatingProfile, float]" = _pmv_center
"""Centro della banda PMV per profilo operativo (vedi docstring di sezione E)."""

MODE_PMV_BAND: "dict[HVACOperatingProfile, float]" = _pmv_band
"""Semiampiezza della banda PMV (±) per profilo operativo."""

MODE_CTRL_AGGRESSIVENESS: "dict[HVACOperatingProfile, float]" = _ctrl_aggressiveness
"""Fattore di aggressività attuazione per profilo (adimensionale, 0..2)."""

# --- PMV summer/ECO nudge ---
PMV_SUMMER_ECO_NUDGE_DELTA: float = 0.10
"""Delta di aggiustamento del centro PMV in ECO estate.

In estate con profilo ECO si alza leggermente il centro PMV per ridurre il
raffreddamento (gli occupanti tollerano un PMV leggermente più caldo in ECO).
Delta applicato come: pmv_center = min(PMV_SUMMER_ECO_CENTER_MAX, center + DELTA)
"""

PMV_SUMMER_ECO_CENTER_MAX: float = 0.20
"""Cap superiore del centro PMV dopo il nudge ECO estate.

Limita il centro a +0.20 (Cat. A boundary) per evitare che ECO in estate
imponga un discomfort eccessivo anche in giornate molto calde.
"""

# --- Limite v_air_hi in SLEEP/WINTER ---
V_AIR_HI_SLEEP_WINTER_MAX: float = 1.00
"""Fattore di scaling v_hi massimo in modalità SLEEP durante l'inverno.

In inverno notturno la VMC è a step basso; si annulla lo scaling living/speed
per non allargare artificialmente la curva alta di velocità quando il draft
non è un rischio reale. Valore 1.00 = nessuna amplificazione rispetto alle
tabelle di default (cap, non riduzione).
"""


# =============================================================================
# SEZIONE F — Zona climatica italiana (DPR 412/1993) → CLO invernale
# =============================================================================
#
# Razionale termotecnico
# ----------------------
# Le zone climatiche italiane (A..F) sono definite per gradi-giorno (GG):
#   A: GG < 600  (es. Lampedusa, Pantelleria)
#   B: 600 ≤ GG < 900  (es. Palermo, Reggio Calabria)
#   C: 900 ≤ GG < 1400 (es. Napoli, Bari, Roma litorale)
#   D: 1400 ≤ GG < 2100 (es. Roma, Firenze, Pescara)   ← impianto di riferimento
#   E: 2100 ≤ GG < 3000 (es. Milano, Torino, Bologna)
#   F: GG ≥ 3000 (es. Bolzano, Aosta, Cortina)
#
# La correlazione GG → CLO riflette le abitudini di abbigliamento invernale:
# nelle zone più fredde gli occupanti tendono a vestirsi più pesante anche in
# casa perché sono abituati a climi rigidi e a temperature radianti esterne più
# basse (finestre fredde, pareti più disperdenti).
#
# Fonte: calibrazione empirica su database residenziale italiano (Enea/CNR),
# coerente con i valori ISO 7730 Tab. B.1 e le indicazioni ASHRAE 55-2020.
#
# Roma (zona D): 1,415 GG → CLO=1.05 (leggermente sopra il valore base 1.00
# per tenere conto del mix residenziale romano con interni spesso non
# ristrutturati e ponti termici).

def _build_zone_clo_table() -> "dict[ClimateZoneIT, float]":
    """Costruisce la tabella CLO invernale per zona climatica."""
    from .model import ClimateZoneIT  # noqa: PLC0415

    return {
        ClimateZoneIT.A: 0.90,  # Clima mite: abbigliamento leggero indoor
        ClimateZoneIT.B: 0.95,  # Clima sub-tropicale: leggero+
        ClimateZoneIT.C: 1.00,  # Mediterraneo: valore base ISO 7730
        ClimateZoneIT.D: 1.05,  # Temperato caldo (Roma): leggero supplemento
        ClimateZoneIT.E: 1.15,  # Temperato continentale: maglione pesante
        ClimateZoneIT.F: 1.25,  # Alpino/prealpino: abbigliamento da montagna indoor
    }


_zone_clo_winter = _build_zone_clo_table()

CLO_WINTER_BY_ZONE: "dict[ClimateZoneIT, float]" = _zone_clo_winter
"""CLO invernale per zona climatica italiana (DPR 412/1993).

Chiave: ``ClimateZoneIT`` enum (A..F).
Valore: CLO in unità ISO (1 clo = 0.155 m²·K/W).
"""


# =============================================================================
# SEZIONE G — Adaptive CLO: running mean T_op interna (Strategia S3)
# =============================================================================
#
# Razionale termotecnico
# ----------------------
# Gli occupanti adattano l'abbigliamento alla temperatura percepita nel tempo:
# una settimana più fredda del solito porta a vestirsi più pesante (CLO sale),
# una settimana mite porta a vestirsi più leggero (CLO scende).
# Questo adattamento non è catturato dal CLO stagionale fisso.
#
# Metodo: EWMA (Exponentially Weighted Moving Average) della T_op interna
# per zona, con costante di tempo T_RM_TAU_HOURS.  Il delta CLO è proporzionale
# alla deviazione della running mean rispetto alla "temperatura neutra attesa"
# per la stagione corrente (T_RM_NEUTRAL_BY_SEASON).
#
# La correzione è soppressa durante il warm-up (T_RM_WARMUP_TICKS tick) e
# per i profili SLEEP/AWAY/VACATION (dove il CLO è già dominato da altre
# correzioni fisicamente ben definite).
#
# Riferimento: ASHRAE 55-2017 §5.4.1 (prevailing mean outdoor temperature)
# applicato alla T_op interna per zona.

T_RM_TAU_HOURS: float = 168.0
"""Costante di tempo dell'EWMA per la running mean T_op (ore).

168h = 7 giorni: un picco di temperatura si attenua a ~37% dopo 7 giorni.
Un'ondata di freddo di 3 giorni produce un delta T_rm di ~35% del suo valore
massimo → correzione CLO moderata, non eccessiva.

Valori alternativi:
- 72h (3gg): risposta più rapida, adatta a impianti con alta inerzia e
  occupanti molto sensibili alle variazioni di breve periodo.
- 336h (14gg): risposta lenta, adatta a climi stabili dove l'adattamento
  comportamentale è principalmente stagionale.
"""

T_RM_WARMUP_TICKS: int = 144
"""Numero minimo di tick validi prima di applicare la correzione adattiva.

Con ciclo a 30s: 144 tick = 72 minuti (≈ 1.2 ore).
Durante il warm-up la policy usa il CLO stagionale base (fail-safe).
Un tick è "valido" se ZoneSnapshot.t_op era disponibile (non None/unavailable).

Motivazione: al riavvio HA la EWMA non ha storia → il primo valore potrebbe
essere anomalo (es. casa vuota dopo vacanza); il warm-up previene che una
singola lettura fredda/calda sposti bruscamente la banda comfort.
"""

T_RM_NEUTRAL_BY_SEASON: "dict[str, float]" = {
    # Temperatura operante "neutra attesa" per stagione (°C).
    # Rappresenta la T_rm attorno alla quale il CLO stagionale base è calibrato.
    # Se T_rm è sotto questo valore → delta_clo > 0 (più vestiti).
    # Se T_rm è sopra questo valore → delta_clo < 0 (meno vestiti).
    #
    # Valori calibrati per abitazione residenziale zona D (Roma):
    # - Winter: 21.5°C — T_op media invernale tipica con impianto radiante
    #   (più alta di 20°C perché il soffitto caldo alza la T_mrt percepita)
    # - Shoulder: 22.5°C — transizione: T_op più alta per compensare
    #   la maggiore variabilità stagionale
    # - Summer: 25.0°C — obiettivo raffrescamento con profilo COMFORT
    "winter":   21.5,
    "shoulder": 22.5,
    "summer":   25.0,
}
"""T_op neutra attesa per stagione (°C) — pivot per il calcolo delta_clo."""

T_RM_SENSITIVITY_CLO: float = 0.05
"""Sensitività CLO alla deviazione T_rm (clo/°C).

Ogni °C di deviazione dalla T_rm neutra produce una variazione di 0.05 clo.
Esempi (prima del cap):
  T_rm = 17°C in inverno (−4.5°C sotto neutro): delta = +0.225 clo
  T_rm = 14°C in inverno (−7.5°C sotto neutro): delta = +0.375 → cap a +0.25

Calibrazione: 0.05 clo/°C ≈ sensibilità media da letteratura ASHRAE
sull'adattamento vestiario in ambienti residenziali controllati.
"""

T_RM_CAP_DELTA_CLO: float = 0.25
"""Cap della correzione CLO adattiva (clo) — valore assoluto simmetrico.

Limita il delta_clo nell'intervallo [−0.25, +0.25].
+0.25 clo corrisponde circa a indossare una maglia in più (ISO 7730 Tab. B.1:
pullover leggero ≈ 0.25 clo); è il massimo adattamento plausibile in risposta
alle variazioni settimanali senza cambiare la natura del profilo stagionale.
Oltre questo valore l'adattamento è più strutturale (cambio stagione) e deve
essere gestito dalla logica di classificazione stagionale ML, non dal CLO adattivo.
"""

T_RM_CLAMP_MIN: float = 10.0
"""Temperatura minima plausibile per T_op interna (°C) — clamp difensivo.

Letture sotto 10°C indicano quasi certamente un sensore guasto o uno scenario
non residenziale (casa disabitata in inverno senza riscaldamento minimo).
Il clamp impedisce che una lettura anomala inquini la EWMA.
"""

T_RM_CLAMP_MAX: float = 35.0
"""Temperatura massima plausibile per T_op interna (°C) — clamp difensivo.

Oltre 35°C il modello PMV perde di accuratezza e il sensore è probabilmente
guasto o in condizioni estreme non residenziali.
"""

T_RM_MAX_DEVIATION_SUPPRESS_C: float = 4.0
"""Deviazione massima |T_op_current − T_rm| oltre la quale S3 viene soppressa (°C).

Se la running mean è troppo distante dalla temperatura corrente, lo scenario
è un rientro da casa fredda/calda (assenza prolungata), non un adattamento
comportamentale graduale. In questo caso S3 produrrebbe un CLO anomalo:
con T_rm=15°C e T_op=21°C il CLO salirebbe a 1.300 (cap) anche se gli
occupanti si sono già riscaldati.
Sopra questa soglia si usa il CLO stagionale baseline (fail-safe neutro).
Sotto questa soglia S3 opera normalmente per l'adattamento quotidiano.

Valore 4.0°C: corrisponde a T_rm < 17.5°C con T_op=21.5°C (neutro invernale).
Scenari normali (occupante adattato): |deviazione| < 3°C.
Scenari rientro (casa vuota): |deviazione| > 5°C → soppressa con ampio margine.
"""

# Profili per cui la correzione S3 viene soppressa
T_RM_SKIP_PROFILES: "frozenset[str]" = frozenset({
    "sleep",      # CLO già corretto da CLO_SLEEP_WINTER_DELTA (coperte)
    "away",       # nessun occupante → CLO privo di significato fisico
    "vacation",   # come AWAY
})
"""Profili operativi per cui la correzione S3 è soppressa.

In SLEEP il CLO è dominato dal delta fisso delle coperte (+0.95 in inverno):
aggiungere una correzione adattiva produrrebbe una doppia correzione incoerente.
In AWAY/VACATION non ci sono occupanti: il CLO serve solo come riferimento per
la protezione dell'impianto, non deve variare con l'adattamento comportamentale.
"""
