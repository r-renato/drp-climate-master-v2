## 1 Impianto climatico residenziale

### 1.1 Schema logico dell'impianto

```text
LEGENDA
  ───  flusso fisico (acqua / aria / condensa)
  - -  controllo / telemetria (Modbus, comandi HA, stati, sensori)

┌───────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                               HOME ASSISTANT + DRP CLIMATE MASTER v2                                          │
│  - logiche comfort / stagioni / ottimizzazione inerzia                                                        │
│  - orchestrazione PDC + VMC + elettrovalvole di zona                                                          │
│                                                                                                               │
│  INPUT (sensori)                                                                                              │
│  - T/UR per zona  -> DewPoint_zona (calcolato)                                                                │
│  - T/UR esterno   -> DewPoint (calcolato)                                                                     │
│  - stati PDC/VMC/elettrovalvole (Modbus)                                                                      │
│  - sensori idronici:                                                                                          │
│      * puffer: T_mandata / T_ritorno verso PDC                                                                │
│      * Pompe distribuzione: T_mandata / T_ritorno verso radiante e VMC                                        │
│                                                                                                               │
│  OUTPUT (attuatori)                                                                                           │
│  - PDC: mode / richieste / setpoint acqua                                                                     │
│  - VMC: programma / dewpoint target / ΔT DP / enable dehum                                                    │
│  - Pompe: ON/OFF                                                                                              │
│  - Valvola mix su pompa di distribuzione mix (0-100)                                                          │
│  - Elettrovalvole di zona: OPEN/CLOSE                                                                         │
└───────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                         │
                           - - - - - - - │ - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
                                         ▼
            ┌──────────────────────────────────────────────────────────────────────────────────────────┐
            │                                    DEWPOINT GUARD                                        │
            │  Scopo: evitare condensa su soffitto/impianto in raffrescamento                          │
            │                                                                                          │
            │  Regola (semplificata):                                                                  │
            │    T_superficie_stimata  >  max(DP_zona) + MARGINE                                       │
            │                                                                                          │
            │  Implementazione tipica:                                                                 │
            │    T_mandata_set  = max( T_mandata_min,  max(DP_zona)+MARGINE + Δ_superficie )           │
            │    Se UR alta: priorità VMC deumidifica → poi abilita raffrescamento radiante            │
            └──────────────────────────────────────────────────────────────────────────────────────────┘


                                    ┌──────────────────────────────────────────┐
                                    │         PDC (pompa di calore)            │
                                    │            AERMEC HMI080                 │
                                    │        heating / cooling (acqua)         │
                                    └───────────────────┬──────────────────────┘
                                         - - - - - - -  │  - - - - - - -
                                        telemetria/CTRL │ telemetria/CTRL
                                              acqua calda / fredda
                                                        │
                                    ┌───────────────────┴──────────────────────┐
                                    │             PUFFER / INERZIALE           │
                                    │                CORDIVARI 25L             │
                                    │ sensori: T_mandata / T_ritorno verso PDC │
                                    └───────────────────┬──────────────────────┘
                                         - - - - - - -  │  - - - - - - -
                                                        │
                                    ┌───────────────────┴──────────────────────┐
                                    │     SEPARATORE IDRAULICO-COLLETTORE      │
                                    │         CALEFFI SEPCOL Serie 559         │
                                    │                                          │
                                    └───────────────────┬──────────────────────┘
                                         - - - - - - -  │  - - - - - - -
                                                        │
                                   ┌────────────────────┴──────────────────────┐
                                   │                                           │
                 ┌─────────────────▼─────────────────┐         ┌───────────────▼────────────────┐
                 │ Pompa di distribuzione MIX        │         │ Pompa di distribuzione DIRETTA │
                 │ (motorizzata)                     │         │                                │
                 │ Caleffi serie 167                 │         │ Caleffi serie 165              │
                 │ sensori: T_mandata / T_ritorno    │         │ sensori: T_mandata / T_ritorno │
                 └─────────────────┬─────────────────┘         └──────────────┬─────────────────┘
                      - - - - - -  │  - - - - - - -                - - - - -  │  - - - - - - -
                     CTRL ON/OFF   │                               CTRL ON/OFF│ 
                  CTRL MIX 0-100   │                                          │
                                   │                                          │
       ┌───────────────────────────▼───────────────────────────┐              │
       │ Collettore di distribuzione (radiante)                │              │
       │ + elettrovalvole di zona                              │              │
       │   (Kitchen, Living, ...)                              │              │
       └───────────────────────────┬───────────────────────────┘              │
                - - - - - - - - -  │  - - - - - - - - - - - -                 │
                CTRL valvole zona  │                                          │
                                   │                                          │
                     ┌─────────────▼─────────────┐              ┌─────────────▼──────────────────────────────────────┐
                     │ Soffitto radiante         │              │ VMC (ventilazione meccanica controllata)           │
                     │ Eurotherm Leonardo        │              │ ENEREN RER020I EFHR0 EVO                           │
                     │ (emissione caldo/freddo)  │              │ (batteria idronica + condensa)                     │
                     └─────────────┬─────────────┘              │ ventilazione / deumid. / heating / cooling         │
                                   │                            └─┬──────────────────────────────────────────────────┘
                                   │                 - - - - - -  │  - - - - - -
                                   │                              │ telemetria/CTRL (Modbus)
                                   │                              │
                                ┌──▼──────────────────────────────▼──┐              ┌───────────────────────────────┐
                                │   AMBIENTE / ZONE (comfort)        │              │   SCARICO CONDENSA VMC        │
                                │   - carichi termici                │              │   (tubo di drenaggio)         │
                                │   - umidità / dewpoint             │              └───────────────┬───────────────┘
                                └───────────────┬────────────────────┘                              │
                                                │                                                   │ condensa
                         ┌──────────────────────▼────────────┐                                      │
                         │ Sensori per zona                  │                                      ▼
                         │  - Temperatura (T)                │                             (scarico / drenaggio)
                         │  - Umidità relativa (UR)          │
                         │  -> DewPoint (calcolato in HA)    │
                         └───────────────┬───────────────────┘
                                         │
                               - - - - - ▼ - - - - -
                               telemetria verso HA

```

* **generatore principale (PDC / pompa di calore)** che produce **acqua calda** e **acqua fredda**
* **distribuzione a soffitto radiante** (per riscaldamento e raffrescamento)
* **VMC (ventilazione meccanica controllata)** che fa anche **deumidificazione a condensazione** (quindi con **scarico condensa**) e raffrescamento
* **controllo domotico via Home Assistant** con integrazioni (Modbus) già attive

---

### 1.2 Pompa di calore (AERMEC modello HMI080)

È una PDC reversibile aria/acqua inverter per acqua refrigerata/calda, con limiti operativi estesi (fino a -25 °C esterni in inverno, fino a 48 °C in estate; acqua fino a 55 °C in heating per modalità low-temp).

La macchina gestisce:

* **Riscaldamento** (acqua calda per il radiante e VMC)
* **Raffrescamento** (acqua fredda per il radiante e VMC)

> **Nota:** Non gestisce la **produzione ACS** (acqua calda sanitaria) in questa installazione.

#### Limiti operativi (da datasheet HMI080)

| Modalità  | T_ext min | T_ext max | T_acqua min | T_acqua max |
|-----------|-----------|-----------|-------------|-------------|
| Cooling   | +10 °C    | +48 °C    | +7 °C       | +25 °C      |
| Heating   | -25 °C    | +35 °C    | +25 °C      | +55 °C (low-temp) |

#### Feedback disponibili via Modbus (Read Only)

| Registro Modbus | Descrizione                        | Note                                      |
|----------------|------------------------------------|-------------------------------------------|
| Word 117       | Unit status                        | 1=Cool, 2=Heat, 6=HW, 8=Off              |
| Word 118       | T-outdoor (°C)                     | Signed 16-bit, -30÷150 °C               |
| Word 125       | T-water out PE (T mandata acqua)   | A monte del separatore idraulico ⚠️      |
| Word 127       | T-water in PE (T ritorno acqua)    | A monte del separatore idraulico ⚠️      |
| Bit 80         | Compressor state                   | ON/OFF                                    |
| Bit 86         | Defrosting state                   | ON/OFF                                    |
| Bit 102        | System Recoverable Protection      | Allarme recuperabile                      |
| Bit 103        | System Irrecoverable Protection    | Allarme non recuperabile → FAULT FSM      |
| Bit 108        | Flow Switch Protection             | Portata insufficiente → FAULT FSM ⚠️     |
| Bit 170        | Flow Switch State                  | ON/OFF                                    |

> ⚠️ **T-water out/in PE (Word 125/127)** misurano la temperatura allo scambiatore interno della PDC, **a monte del separatore idraulico e delle pompe di distribuzione**. Non coincidono con T_mandata_radiante (misurata a valle della valvola miscelatrice Caleffi 167) né con T_mandata_VMC (a valle della pompa diretta Caleffi 165). Questi sensori sono fisici separati.

> ⚠️ **Flow Switch Protection (Bit 108)**: se la portata è insufficiente (valvole chiuse, pompa spenta) la macchina entra in protezione. Il driver deve rilevare Bit 108 = 1 e transizionare la FSM in stato FAULT.

#### Output (attuatori) via Modbus

| Registro Modbus | Descrizione                        | Range              |
|----------------|------------------------------------|--------------------|
| Word 2         | Modalità operativa                 | 1=Heat, 2=HW, 3=Cool+HW, 4=Heat+HW, 5=Cool |
| Word 9         | WOT-Cool (setpoint acqua raffresc.)| 7–25 °C            |
| Word 10        | WOT-Heat (setpoint acqua risc.)    | 20–55 °C           |
| Word 29        | ΔT-Cool                            | 2–10 °C (default 5)|
| Word 30        | ΔT-Heat                            | 2–10 °C (default 10)|
| Word 33        | Cool run time (anti-short-cycling) | 1–10 min (default 3)|
| Word 34        | Heat run time (anti-short-cycling) | 1–10 min (default 5)|
| Word 42        | ON/OFF                             | **0xAA=On, 0x55=Off** ⚠️ |

#### Vincoli di attuazione Modbus (CRITICI)

> ⚠️ **Cambio modalità richiede unità OFF**: la scrittura su Word 2 (Mode) viene ignorata silenziosamente se l'unità è accesa (Word 117 ≠ 8). Il driver deve verificare `Unit status == 8 (Off)` prima di inviare il cambio di modalità.

> ⚠️ **Encoding ON/OFF non standard**: Word 42 utilizza `0xAA` per ON e `0x55` per OFF. Non è un booleano 0/1. Scrivere 1 o 0 non ha effetto.

> ⚠️ **Anti-short-cycling interno**: Word 33/34 configurano il min runtime interno della macchina (3 min cooling, 5 min heating di default). Questi sono indipendenti dal `MIN_ON_TIME_MINUTES` gestito da HA, che deve essere più conservativo (10–15 min) per protezione inverter.

> ⚠️ **Funzione Weather Depend**: la PDC dispone di una curva climatica interna (Bit 22 + Word 17–28) che regola automaticamente il setpoint acqua in funzione della T esterna. **Se abilitata, sovrascrive i comandi HA su Word 9/10**. Deve essere disabilitata (Bit 22 = 0). Tutta la logica di setpoint acqua è gestita da HA.

---

### 1.3 Puffer (CORDIVARI modello 3070160920001 25L)

Utilizzato come buffer idraulico per la PDC.

* **Sensori disponibili**: T_mandata e T_ritorno verso PDC (corrispondono a T_boiler_ingresso e T_boiler_uscita nel modello dati di HA).
* Capacità: 25 L. Funzione: smorzare i transitori termici e proteggere la PDC da cicli eccessivamente brevi.

> **Nota**: i sensori T_mandata/T_ritorno del puffer sono fisici dedicati — verificare tipo (Pt1000, NTC o sonda digitale) e modalità di acquisizione in HA.

---

### 1.4 Separatore idraulico-collettore SEPCOL (CALEFFI serie 559)

Utilizzato come separatore idraulico tra il puffer e le due pompe di distribuzione. Disaccoppia idraulicamente il circuito primario (PDC + puffer) dai circuiti secondari (pompa MIX e pompa DIRETTA), evitando interferenze di portata.

---

### 1.5 Pompe di distribuzione idronica

#### Gruppo di regolazione termica motorizzato — CALEFFI Serie 167 (pompa MIX)

Completo di valvola miscelatrice a tre vie motorizzata, termometri di mandata e ritorno, valvole di intercettazione circuito secondario e coibentazione a guscio preformata. Alimenta il **circuito radiante a soffitto**.

**Feedback disponibili (sensori fisici):**
* Temperatura mandata (T_mandata_radiante)
* Temperatura ritorno (T_ritorno_radiante)

> ⚠️ Verificare se T_mandata_radiante e T_ritorno_radiante sono acquisiti come sensori digitali in HA (e con quale tecnologia: Pt1000, NTC, sonda bus) oppure sono solo indicatori meccanici locali non integrati.

**Output (attuatori):**
* Power pompa (ON/OFF)
* Valvola motorizzata miscelatrice: **controllo a 3 punti** (apri / ferma / chiudi) ⚠️

> ⚠️ **Controllo valvola a 3 punti, non analogico**: il servomotore Caleffi 637042 (230V) riceve segnale a 3 punti (open/stop/close), non un segnale analogico 0–100%. La posizione percentuale si ottiene **temporizzando il comando** in relazione al tempo di manovra. Il driver HA deve implementare logica temporizzata (switch + timer), non un'entity `number` analogica diretta.

> ⚠️ **Tempo di manovra: 150 secondi per 90°** (Caleffi 637042, 230V, da datasheet). Il sequenziamento deve attendere almeno **160 s** dopo il comando di apertura prima di avviare la pompa.

> ⚠️ **Nessun feedback di posizione**: il modello 637042 (230V) non fornisce segnale di retroazione. Il feedback 0–10V è disponibile solo sul modello 637044 (24V), non installato in questa configurazione.

#### Gruppo di distribuzione diretta — CALEFFI Serie 165 (pompa DIRETTA)

Completo di pompa elettronica ad alta efficienza. Alimenta il **circuito VMC**.

**Feedback disponibili (sensori fisici):**
* Temperatura mandata (T_mandata_VMC)
* Temperatura ritorno (T_ritorno_VMC)

> ⚠️ Verificare se T_mandata_VMC e T_ritorno_VMC sono acquisiti come sensori digitali in HA oppure sono solo indicatori meccanici locali non integrati.

**Output (attuatori):**
* Power pompa (ON/OFF)

#### Nota termotecnica — modalità pompa raccomandata

Per i pannelli radianti a soffitto, Wilo raccomanda la modalità a **pressione differenziale costante (Δp-c)**:
* Mantiene una prevalenza costante al variare della portata → distribuzione stabile ai circuiti del collettore.
* Coerente con impianti a portate prevedibili (tipico dei pannelli radianti).

**Taratura consigliata per Δp-c:**

| Situazione | Livello |
|------------|---------|
| Punto di partenza consigliato | Δp-c / II (medio) |
| Zone lontane non raggiungono comfort, ΔT mandata-ritorno alto, sensazione di strozzatura | Δp-c / III |
| Rumori di flusso/valvole, portate eccessive, ΔT troppo basso | Δp-c / I |

---

### 1.6 Collettore di distribuzione di zona con elettrovalvole di controllo

Il soffitto radiante è diviso in **7 zone idrauliche indipendenti**. Il collettore permette l'alimentazione del radiante a soffitto nelle seguenti zone:

* Kitchen
* Living
* Foyer
* Master Bedroom
* Guest Bedroom
* Master Bathroom
* Main Bathroom

Le elettrovalvole del collettore sono comandate attraverso 7 attuatori (testine elettrotermiche).

**Output (attuatori):**
* Power zona (ON/OFF)

**Nessun feedback di posizione reale**: il feedback disponibile è esclusivamente lo stato del comando inviato. Non assumere conferma di apertura/chiusura fisica.

#### Caratteristiche testine elettrotermiche (ST5150020201)

* Tensione: 220V, 4 fili
* Tipo: **NC (normalmente chiuso)** — la valvola è chiusa senza alimentazione, si apre quando alimentata
* Tempo di apertura: **~2–3 minuti** (tipico per testine elettrotermiche NC 220V) ⚠️

> ⚠️ **Tempo di apertura ~2–3 min**: il sequenziamento deve attendere almeno **120 s** dopo il comando ON prima di avviare la pompa del circuito. Un'attesa di 30 s è insufficiente e causa l'avvio della pompa con valvole ancora chiuse (rischio cavitazione / trip del flow switch della PDC). Verificare il valore esatto sul datasheet completo ST5150020201.

> ⚠️ **Comportamento fail-safe NC**: in caso di interruzione di alimentazione, tutte le testine si chiudono. Se la pompa è ancora attiva (alimentazione separata), il circuito si trova chiuso con pompa in funzione. La sequenza di arresto deve gestire questo scenario: stop pompa prima della perdita di alimentazione, oppure verificare che la PDC rilevii il trip del flow switch (Bit 108) e transizioni in FAULT in modo controllato.

#### Nota termotecnica

Con il soffitto radiante in raffrescamento, il rischio principale è la **condensa sul soffitto**. È indispensabile un **dew point guard** ben calibrato: la temperatura di mandata al circuito radiante deve sempre mantenersi al di sopra del punto di rugiada dell'ambiente maggiorato di un margine di sicurezza (v. §1.1 schema Dewpoint Guard).

---

### 1.7 Ventilazione Meccanica Controllata (VMC ENEREN RER020I EFHR0 EVO)

Deumidificatore canalizzabile da controsoffitto con recuperatore di calore. Installato in abbinamento al sistema radiante a soffitto. Permette di deumidificare, raffrescare e riscaldare, effettuando un ricambio dell'aria esausta con aria pulita proveniente dall'esterno.

La VMC **deumidifica per condensazione**: dispone internamente di uno scambiatore/batteria fredda che porta l'aria sotto il punto di rugiada, producendo condensa espulsa tramite scarico dedicato.

#### Prerequisiti di commissioning RS485

> ⚠️ **RS485 disabilitato di default**: per il controllo da HA è necessario configurare il parametro di abilitazione seriale da display VMC (menu installatore, codice `0010`): `RS485-MODBUS → Abilitazione seriale = SLAVE`. Senza questo step, nessun comando Modbus ha effetto.

> ⚠️ **Modalità SL-STAGIONE**: in questa modalità parziale sono disponibili via Modbus solo i comandi di stagione e force-off. Per il controllo completo è richiesta la modalità SLAVE completa.

#### Feedback analogici — Input Register FC04 (Read Only)

| Registro | Descrizione              | Scala      | Tipo        |
|----------|--------------------------|------------|-------------|
| 1        | Temperatura ambiente     | °C × 10    | Signed int16 |
| 2        | Umidità relativa         | % × 10     | Signed int16 |
| 17       | Temperatura acqua        | °C × 10    | Signed int16 |
| 18       | Temperatura esterna      | °C × 10    | Signed int16 |
| 19       | Stato ventilatore mandata| %          | Signed int16 |
| 20       | Stato ventilatore ripresa| %          | Signed int16 |
| 26       | Temperatura aria mandata | °C × 10    | Signed int16 |

> ⚠️ **Scala Modbus /10**: tutti i valori di temperatura e umidità sono trasmessi con scala `/10` (es. 215 = 21.5 °C; 550 = 55.0%). I valori sono **signed integer a 16 bit** (complemento a 2 per temperature negative, es. -5 °C = -50 = `0xFFCE`). Il driver deve usare `struct.unpack('>h', ...)` (signed), non `'>H'` (unsigned).

> **Nota**: T_ambiente e UR_ambiente (Reg 1, 2) misurano le condizioni dell'aria processata dalla VMC, **non le condizioni per singola zona**. Per il calcolo del dew point per zona sono necessari sensori ambientali indipendenti per ciascuna zona. I valori VMC sono utili come check di coerenza globale.

#### Segnali di stato e richiesta — Discrete Input FC02 (Read Only)

| Coil | Descrizione                  | Impatto progetto                        |
|------|------------------------------|-----------------------------------------|
| 1    | Allarme presente             | → FAULT / DEGRADED nella FSM           |
| 17   | **Allarme dew point**        | → Lockout raffrescamento ⚠️            |
| 18   | Stato compressore            | ON/OFF                                  |
| 20   | **Richiesta acqua dall'impianto** | → attivazione pompa DIRETTA ⚠️   |
| 21   | Richiesta deumidifica        | Feedback stato VMC                      |
| 22   | Richiesta raffreddamento     | Feedback stato VMC                      |
| 23   | Richiesta riscaldamento      | Feedback stato VMC                      |

> ⚠️ **Coil 20 — Richiesta acqua**: questo segnale è un **output della VMC verso HA** (non bidirezionale). HA deve **interrogare (poll)** questo bit ad ogni ciclo del coordinator per rilevare quando la VMC richiede acqua e attivare la pompa DIRETTA. Non è un segnale push/interrupt.

> ⚠️ **Allarme dew point (Coil 17)**: la VMC dispone di un proprio controllo interno del dew point (configurabile via Reg 15/16). L'allarme Coil 17 deve essere trattato come **fail-safe di escalation**: la logica primaria di dew point guard è gestita da HA; l'allarme VMC è il secondo livello di protezione che forza il lockout del raffrescamento.

#### Output (attuatori) — Coil FC01/FC15 (Read/Write)

| Coil | Descrizione                           | Note                                   |
|------|---------------------------------------|----------------------------------------|
| 1    | ON/OFF unità                          | Attuatore principale                   |
| 2    | Stagione: 0=estate, 1=inverno         | Commutazione stagionale                |
| 3    | Forza OFF trattamento                 | Ventilazione senza trattamento termico |
| 9    | Abilita free-cooling                  | Mid-season                             |
| 10   | Forza free-cooling                    | Mid-season                             |
| 12   | **Abilita forzatura heat/cool** ⚠️   | Prerequisito per Coil 13/14            |
| 13   | Forza riscaldamento                   | Richiede Coil 12 = 1 prima             |
| 14   | Forza raffreddamento                  | Richiede Coil 12 = 1 prima             |
| 15   | **Abilita forzatura deumidifica** ⚠️ | Prerequisito per Coil 16               |
| 16   | Forza deumidifica                     | Richiede Coil 15 = 1 prima             |
| 17   | Forza ricircolo                       | Solo se processing mode = OFF          |
| 23   | Gestione dew point: 0=variabile, 1=fisso | Config interna dew point VMC       |

> ⚠️ **Doppio step obbligatorio per forzature**: per comandare heating/cooling/dehumid via Modbus sono necessarie **due scritture distinte in sequenza**:
> 1. Scrivi Coil 12 = 1 (abilita forzatura heat/cool) → poi scrivi Coil 13 o 14
> 2. Scrivi Coil 15 = 1 (abilita forzatura deumidifica) → poi scrivi Coil 16
>
> Una scrittura singola sul comando senza la preventiva abilitazione viene **ignorata silenziosamente** dalla VMC. Nessun errore Modbus viene restituito.

#### Setpoint configurabili — Holding Register FC03/FC16 (Read/Write)

| Registro | Descrizione                        | Range              | Scala    |
|----------|------------------------------------|--------------------|----------|
| 1        | Setpoint temperatura ambiente      | 15.0–30.0 °C       | °C × 10  |
| 2        | Setpoint umidità relativa          | 40.0–90.0 %        | % × 10   |
| 3        | Set ricambio (velocità aria)       | 0–5 (step interi)  | —        |
| 12       | T min invernale senza riscaldamento| 0.0–90.0 °C        | °C × 10  |
| 13       | T max estiva senza raffreddamento  | 0.0–90.0 °C        | °C × 10  |
| 15       | Differenziale dew point            | -10.0 ÷ +10.0 °C   | °C × 10  |
| 16       | T superficie fissa dew point       | 10.0–40.0 °C       | °C × 10  |

> **Set ricambio (Reg 3)**: valori interi 0–5, corrispondenti a fasce di portata configurate in fase di taratura. Non è una percentuale continua né una velocità in RPM.

#### Nota termotecnica

La VMC non è solo "ventilazione": è un attore attivo che può:
* Aiutare il comfort estivo riducendo l'umidità interna
* Rendere possibile il raffrescamento radiante abbassando il dew point degli ambienti (riducendo il rischio condensa sul soffitto)
* Operare in ventilazione pura (Coil 3 = 1, trattamento disabilitato) nelle stagioni intermedie