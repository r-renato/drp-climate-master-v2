# DRP Climate Master
Home Assistant Integration for Home Climate Plant

## 1 Impianto climatico residenziale

### 1.1 Schema logico dell’impianto

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

### 1.2 Pompa di calore (AERMEC modello HMI080)

E' una PDC reversibile aria/acqua inverter per acqua refrigerata/calda, con limiti operativi estesi (fino a -25 °C esterni in inverno, fino a 48 °C in estate; acqua fino a 60 °C in heating).

* La macchina gestisce:

  * **Riscaldamento** (acqua calda per il radiante e VMC)
  * **Raffrescamento** (acqua fredda per il radiante e VMC)


Nota
    Non gestisce la **produzione ACS** (acqua calda sanitaria)

* Input (sensori)
    * Temperatura esterna
    * Compressore (ON/OFF)

* Output (attuatori)
    * Power (ON/OFF)
    * Mode (Heating/Cooling)
    * Air removal (ON/OFF)
    * Heat wather outlet Temperature (20/55 °C)
    * Heat wather outlet Delta Temperature (2/10 °C)
    * Cool wather outlet Temperature (7/25 °C)
    * Cool wather outlet Delta Temperature (2/10 °C)

### 1.3 Puffer (CORDIVARI modello 3070160920001 25L)

E' utilizzato come buffer per la PDC

### 1.4 Separatore idraulico-collettore SEPCOL (CALEFFI serie 559)

E' utilizzato come separatore tra il puffer e le due pompe di distribuzione

### 1.5 Pompe di distribuzione idronica

**Gruppo di regolazione termica motorizzato per impianti di riscaldamento e raffrescamento** (CALEFFI Serie 167)

Completo  di  valvola  miscelatrice  a  tre  vie  motorizzata,  termometri  di  mandata  e  ritorno,  valvole  di  intercettazione  circuito  secondario  e  coibentazione a guscio preformata.

* Input (sensori)
    * Temperatura mandata
    * Temperatura ritorno

* Output (attuatori)
    * Power (ON/OFF)
    * Valvola motorizzata miscelatrice (0/100)


**Gruppo di distribuzione direttaper impianti di riscaldamento e condizionamento** (CALEFFI Serie 165)

Gruppo di distribuzione direttaper impianti di riscaldamento e condizionamentoserie 165FunzioneIl  gruppo  di  distribuzione  diretta  svolge  la  funzione  di  alimentare  i  circuiti  degli  impianti  di  riscaldamento  ad  alta  temperatura  o  degli  impianti di condizionamento. Completo  di  pompa  elettronica  ad  alta  efficienza.

* Input (sensori)
    * Temperatura mandata
    * Temperatura ritorno

* Output (attuatori)
    * Power (ON/OFF)

Note
Per i pannelli radianti, quindi anche il radiante a soffitto, Wilo raccomanda la modalità a pressione differenziale costante (Δp-c), questo perchè Δp-c:
* Mantiene una prevalenza costante al variare della portata, quindi aiuta ad avere una distribuzione stabile ai circuiti del collettore.
* È coerente con impianti dove vuoi portate prevedibili e non “ballerine” (tipico dei pannelli radianti). 

Quale “curva” (I / II / III) usare in Δp-c
    
    In Δp-c hai tre livelli (I, II, III). Il manuale non può dirti “II” o “III” senza conoscere perdite di carico e portate, quindi il modo pratico è una taratura per passi:

    Settaggio iniziale consigliato

        * Δp-c / II (medio): spesso è il miglior punto di start.

    Quando salire (Δp-c / III)

        * alcune zone lontane non scaldano/raffrescano abbastanza,
        * ΔT mandata-ritorno troppo alto perché manca portata,
        * percepisci “strozzature” (poca resa) con più zone aperte.

    Quando scendere (Δp-c / I)

        * senti rumori di flusso/valvole,
        * hai portate eccessive (ΔT troppo basso e risposta troppo aggressiva),
        * vuoi minimizzare consumi mantenendo comfort.

    Nota: nel troubleshooting il manuale cita proprio che, se la resa dei pannelli radianti è bassa, può essere opportuno impostare Δp-c anziché Δp-v.

### 1.6 Collettore di distribuzione di zona con elettrovalvole di controllo 

Il soffitto radiante è diviso in **zone idrauliche**. E' possibile aprire/chiudere la singola zona per far circolare acqua quando serve.In particolare, il collettore permette l'alimetazione del radiante a soffito in 7 diverse zone:
* Kitchen
* Living
* Foyer
* Master Bedroom
* Guest Bedroom
* Master Bathroom
* Main Bathroom

Le elettrovalvole del collettore sono comandate attraverso 7 attuatori.
* Output (attuatori)
    * Power zona (ON/OFF)

Nota termotecnica
    con il soffitto radiante in raffrescamento, il problema principale è la **condensa sul soffitto**. Serve sempre un **dew point guard** ben fatto per evitare l'umidità.

### 1.7 Ventilazione Meccanica Controllata (VMC ENEREN RER020I EFHR0 EVO)

Deumidificatore canalizzabile da controsoffitto con recuperatore di calore, installato in abbinamento al sistema radiante a soffitto.  La VMC permette di deumidificare, raffrescaree e riscaldare, effettuando un ricambio dell’aria esausta con aria pulita proveniente dall’esterno.

La VMC “deumidifica condensando”, ha quindi internamente ha uno scambiatore/batteria fredda che porta l’aria sotto punto di rugiada, produce condensa che viene espulsa da uno **scarico**.

* Input (sensori)
    * Compressore (ON/OFF)
    * Temperatura ambiente
    * Umidità ambiente
    * Temperatura acqua (supporto per raffrescamento, riscaldamento, deumidificazione)
    * Request Plant Water (ON/OFF)
    * Request cooling (ON/OFF)
    * Request heating (ON/OFF)
    * Request dehumidifier (ON/OFF)
    * Free cooling (ON/OFF)
    * Dew Point alarm (ON/OFF)

* Output (attuatori)
    * Power (ON/OFF)
    * Processing mode (winter, summer, off)
    * Air speed (0/5)
    * Recirculation (ON/OFF solo se processing mode OFF)
    * Ambient temperature target
    * Ambient humidity target
    * Dew Point target
    * Delta Dew Point Target
    * Winter Temperature min
    * Summer Temperature max

Nota termotecnica: la VMC non è solo “ventilazione”, è un attore attivo che può:
* aiutare il comfort estivo (togliendo umidità)
* rendere possibile il raffrescamento radiante (riducendo il rischio condensa)

## 2 Logica di controllo termotecnico

### 2.1 Obiettivi e vincoli

L’impianto è un sistema ibrido idronico + aria (soffitto radiante + VMC con batteria idronica e deumidifica a condensazione). La logica di controllo termotecnica persegue, in ordine di priorità:

**Sicurezza impiantistica e igrometrica**

* prevenzione condensa su superfici radianti e in parti critiche dell’edificio;
* protezioni di minima/massima temperatura acqua, anti-ciclo compressore, anticongelamento.
* nei sistemi radianti in raffrescamento è necessario un controllo anticondensa basato su dew point e un limite sulla temperatura minima dell’acqua di mandata (approccio normativo/di buona pratica).

**Comfort termoigrometrico**

* mantenimento di temperatura operante/ambiente entro banda di comfort;
* controllo dell’umidità (in estate il carico latente è gestito principalmente dalla VMC).

**Efficienza energetica**

* massimizzazione COP/EER della PDC tramite temperature acqua il più possibile “dolci” compatibilmente con comfort e vincoli;
* riduzione cicli e oscillazioni (inerzia + isteresi).

### 2.2 Struttura di controllo a livelli (impianto)

La regolazione è organizzata in tre livelli logici:

**Livello A – Modalità impianto (regime)**

* Impianto in HEATING, COOLING, DEHUM-ASSIST (estate con priorità latente), oppure VENTILATION ONLY (mezza stagione/free cooling).
* Il cambio regime è volutamente lento (anti “ping-pong” tra caldo/freddo): si applicano bande morte e tempi minimi di permanenza.

**Livello B – Produzione primaria (PDC + puffer)**

* La PDC mantiene una temperatura acqua “primaria” nel puffer/separatore coerente con il fabbisogno più esigente tra:
    * batteria VMC (diretta),
    * circuito radiante (ma quest’ultimo è protetto/ottimizzato dalla miscelazione).
* Il puffer e la separazione idraulica hanno lo scopo di:
    * stabilizzare la PDC (ridurre cicli),
    * disaccoppiare portate tra primario e secondari.

**Livello C – Distribuzione secondaria**

* Secondario VMC (diretto): quando la VMC richiede acqua (heating/cooling/dehum), la pompa diretta abilita lo scambio; la capacità lato aria è gestita dalla VMC (portata aria, modalità di trattamento, setpoint).
* Secondario radiante (mix): la pompa mix garantisce circolazione quando esiste richiesta in almeno una zona; la valvola 3-vie modula la temperatura di mandata al soffitto radiante.

### 2.3 Logica di controllo in inverno (HEATING)

**Obiettivo**
* coprire il carico sensibile con il radiante (base load), usando la VMC come:
    * supporto per omogeneità/IAQ,
    * eventuale “boost” transitorio se necessario.

**Setpoint acqua primaria (PDC)**
* preferibilmente guidato da una curva climatica in funzione della temperatura esterna (compensazione), limitata da:
    * massima T ammessa dal radiante,
    * richiesta VMC (se attiva in heating).
* principio
    * acqua più bassa possibile compatibilmente col carico, per mantenere alta efficienza.

**Mandata radiante (tramite miscelazione)**
* regolata su un setpoint dedicato del circuito radiante (tipicamente più basso del primario).
* strategia consigliata:
    * controllo lento (inerzia del sistema) con isteresi e limiti di variazione;
    * stabilizzazione su ΔT mandata/ritorno “ragionevole” (indicatore di portata adeguata).

**Gestione zone**
* richiesta termica per zona con banda di isteresi (on/off elettrovalvola);
* aggregazione delle richieste per decidere abilitazione pompa mix e produzione primaria.

### 2.4 Logica di controllo in estate (COOLING + DEHUM)

In estate il sistema è vincolato dalla condensa, il radiante può gestire bene il carico sensibile, ma il carico latente deve essere gestito dalla VMC (deumidifica a condensazione). È una regola generale nei sistemi radianti, la prevenzione della condensa richiede che temperatura di superficie (e quindi indirettamente la mandata acqua) non scenda sotto il dew point dell’aria ambiente.

#### 2.4.1 Dew Point Guard (anticondensa)
* Si misura/deriva il dew point per zone rappresentative e si assume come vincolo quello della zona più critica (dew point più alto), come raccomandato per il controllo dei sistemi radianti in raffrescamento.
* Condizione di sicurezza (forma pratica):
    * T_superficie_min > DP_max + margine
* Poiché la superficie non è sempre misurata, si traduce in un vincolo su T_mandata_radiante_min, includendo:
    * un margine igrometrico (tipicamente ≥ 1 °C; in pratica spesso 1.5–3 °C in funzione di rischio, precisione sensori e dinamica).
    * un delta tecnico superficie–acqua (dipendente dal pannello/portata/carico), trattato come parametro conservativo.

Quindi:
* T_mandata_radiante_set ≥ DP_max + MARGINE + Δ(superficie↔acqua)

Se il vincolo non è rispettabile (DP troppo alto), il controllo deve:
1. ridurre/annullare raffrescamento radiante,
2. attivare priorità deumidifica VMC,
3. riprendere raffrescamento radiante solo quando DP scende sotto soglia (con isteresi temporale).

#### 2.4.2 Coordinamento PDC ↔ VMC ↔ Radiante
* PDC (primario) guidata dalla VMC quando serve latente: per condensare e deumidificare, la batteria deve poter lavorare a temperatura sufficientemente bassa.
* Radiante protetto dalla miscelazione: anche se il primario è freddo per consentire deumidifica, la 3-vie miscela per mantenere la mandata radiante sopra il limite anticondensa.

Questa architettura consente:
* VMC = gestione latente + parte sensibile rapida,
* radiante = gestione sensibile efficiente e uniforme,
* evitando condensa.

#### 2.4.3 Sequenza operativa estate (raccomandata)
1. Valutazione DP_max (zona peggiore).
2. Se DP_max alto → fase DEHUM-ASSIST:
    * VMC in deumidifica fino a riduzione dew point,
    * radiante in cooling limitato o sospeso.
3. Quando DP_max rientra → fase COOLING STABLE:
    * abilita radiante con T_mandata protetta dal dew point guard,
    * VMC continua ventilazione e, se necessario, deumidifica “di mantenimento”.

### 2.5 Mezze stagioni e Free Cooling

Quando condizioni esterne lo permettono:
* si privilegia ventilazione (eventuale free cooling VMC) per smaltire carichi senza attivare la PDC;
* il radiante resta in stand-by (o solo mantenimento) per evitare continui transitori.

### 2.6 Logiche di robustezza (anti-ciclo, transitori, fail-safe)

Anti short-cycling PDC
* tempi minimi ON/OFF e isteresi sul puffer (particolarmente importante con puffer di piccolo volume).
Pump overrun
* dopo stop richiesta, mantenere circolazione per smaltire energia residua (riduce picchi e stress).
Fail-safe condensa
* se allarme dew point (o rischio elevato): stop cooling radiante + priorità deumidifica.
* ripartenza con ramp-up e margini conservativi.

Limitazioni di comfort vs sicurezza
* in estate, la sicurezza anticondensa prevale sempre sul raggiungimento “aggressivo” della temperatura.

### 2.7 Parametri da tarare (commissioning)
* curve/clamp di temperatura primaria (heating/cooling);
* MARGINE dew point e Δ(superficie↔acqua);
* isteresi e tempi minimi per cambi regime;
* taratura portate/pompe (Δp-c consigliata per circuiti radianti) e bilanciamento collettori;
* priorità tra VMC e radiante in estate in base a condizioni tipiche dell’edificio.

### 2.8 Grandezze di stato (termo-igrometriche)
```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                       GRANDEZZE DI STATO (per ZONA z)                        │
├──────────────────────────────────────────────────────────────────────────────┤
│  Misure/derivate base                                                        │
│  - T_air,z     [°C]  = temperatura aria (sensore zona)                       │
│  - RH_z        [%]   = umidità relativa (sensore zona)                       │
│  - DP_z        [°C]  = dew point zona (da T_air,z + RH_z)                    │
│  - T_out, RH_out, DP_out (esterno)                                           │
│                                                                              │
│  Comfort “fisico” (quello che conta davvero per percezione)                  │
│  - MRT_z       [°C]  = Mean Radiant Temperature (stima)                      │
│      • ideale: da sensore MRT o “globe”                                      │
│      • pratico: modello/stima da: T_air,z + contributo radiante (soffitto    │
│  - T_op,z      [°C]  = temperatura operante                                  │
│      • per v_aria bassa tipica residenziale: T_op ≈ (T_air + MRT)/2          │
│                                                                              │
│  Stato impiantistico idronico (per controllo e diagnostica)                  │
│  - T_sup_rad   [°C]  = mandata circuito radiante (sensore su pompa MIX)      │
│  - T_ret_rad   [°C]  = ritorno circuito radiante                             │
│  - T_sup_vmc   [°C]  = mandata batteria idronica VMC (sensore su pompa DIR)  │
│  - T_ret_vmc   [°C]  = ritorno batteria VMC                                  │
│  - T_pdc_out/in[°C]  = mandata/ritorno lato PDC (puffer)                     │
│                                                                              │
│  Carichi (non “verità assolute”, ma stime utili a decidere lo stato)         │
│  - Q_sens,z    [W]   = carico sensibile stimato                              │
│      • proporzionale a (T_op,set − T_op) e inerzia (radiante lento)          │
│  - Q_lat,z     [W]   = carico latente stimato                                │
│      • proporzionale a (w_set − w) o (DP_set − DP) (deumidifica)             │
│                                                                              │
│  Indici di rischio (fondamentali in raffrescamento radiante)                 │
│  - DP_max      [°C]  = max_z(DP_z)  (la zona “peggiore” governa il rischio)  │
│  - CondRisk    [-]   = rischio condensa se T_superficie ≤ DP_max + margine   │
└──────────────────────────────────────────────────────────────────────────────┘
```

    Nota termotecnica critica (importante): su soffitto radiante in cooling la variabile governante non è “quanto manca alla temperatura”, ma quanto sei vicino alla condensa. Quindi in estate la gerarchia corretta è: sicurezza condensa → controllo latente (VMC) → controllo sensibile (radiante). La tua sezione “DewPoint Guard” è coerente con questa impostazione.


| Stato | Condizione di ingresso (logica) | PDC (Aermec HMI080) | Miscelatrice (pompa MIX) | Pompe | VMC (ENEREN) |
|---|---|---|---|---|---|
| **HEATING** | Richiesta comfort: `T_op,z < T_op,set − hyst`<br>Condensa irrilevante | **ON**, mode **HEATING**.<br>Setpoint acqua “primario” orientato al radiante (bassa T): tipicamente **30–40°C** (poi fine-tune). | Regola per ottenere `T_sup_rad,target` (bassa T, stabile).<br>Obiettivo: ΔT radiante ragionevole (es. **3–6 K**) e stabilità con valvole che aprono/chiudono. | `P_mix` **ON** se ≥1 zona aperta.<br>`P_dir` **ON** solo se VMC richiede heating aria (o post-trattamento). | Programma **WINTER**.<br>Se serve: heating assist (aria) ma senza inseguire troppo (radiante già copre il sensibile). |
| **COOLING** | Richiesta comfort: `T_op,z > T_op,set + hyst`<br>**e** `CondRisk = OK` (cioè `DP_max` sufficientemente basso) | **ON**, mode **COOLING**.<br>Setpoint acqua “primario” per freddo compatibile con VMC + radiante (tipicamente **7–18°C** a seconda delle logiche impianto; il radiante però va “protetto” dalla miscelatrice). | Elemento chiave: imposta `T_sup_rad,target = max(T_min_rad, DP_max + margine + Δsurf)`.<br>In pratica: se `DP` sale, **alzi** la mandata radiante (meno potenza, più sicurezza). | `P_mix` **ON** se richiesta sensibile e condensa OK, con ≥1 zona aperta.<br>`P_dir` **ON** se VMC in raffrescamento/deumidifica o free-cooling assist. | Programma **SUMMER**.<br>Deumidifica solo se necessario (vedi **DEHUM-ASSIST**). |
| **DEHUM-ASSIST** | Richiesta raffrescamento ma `CondRisk = HIGH` (`DP_max` alto)<br>oppure umidità/DP oltre target | PDC può restare **COOLING** ma con setpoint “utile” alla VMC (batteria fredda) + limiti impianto.<br>Obiettivo: fornire acqua fredda alla VMC per togliere latente. | Porta la miscelatrice verso “sicurezza”: `T_sup_rad,target` elevata (fino a neutralizzare il radiante).<br>Il radiante può restare **inibito** se `DP` troppo alto. | `P_dir` **ON** (priorità) per alimentare batteria VMC.<br>`P_mix` **OFF** oppure **ON** solo se `T_sup_rad` è safe e serve un minimo sensibile. | Deumidifica **ON** (condensazione) con `DP target` e `ΔDP` coerenti col tuo DewPoint Guard.<br>Velocità aria: modulata per capacità latente (non per rumore). |
| **VENT ONLY** | Nessuna richiesta sensibile/latente, oppure “free-cooling” favorevole (esterno migliore) | **OFF** (o ON ma idle se serve solo circolazione minima impianto: di norma eviterei). | Neutra (mantieni posizione o metti in posizione “park” che eviti ricircoli inutili). | `P_mix` **OFF**, `P_dir` **OFF** (salvo anticondensa/antistallo programmati). | Programma **OFF** o solo ventilazione/ricambio.<br>Free-cooling se disponibile e conveniente (T/UR esterne). |



## 3 Logica di controllo in Home Assistant come orchestratore

Home Assistant, attraverso l'integrazione **DRP Climate Master V2** funge di fatto da **BMS residenziale**.

### 3.1 Architettura di riferimento dell'integrazione DRP Climate Mater V2

L’integrazione **DRP Climate Master v2** trasforma Home Assistant in un orchestratore/BMS residenziale per un impianto ibrido idronico + aria (PDC + puffer + separatore + doppia distribuzione + soffitto radiante a zone + VMC con batteria idronica e deumidifica a condensazione).
L’obiettivo architetturale non è “controllare un singolo device”, ma coordinare più sottosistemi con priorità termotecniche chiare:
* Sicurezza igrometrica e impiantistica (dew point guard, limiti acqua, anti-ciclo)
* Comfort termo-igrometrico (T operante/aria, umidità/DP)
* Efficienza (temperature acqua “dolci”, riduzione oscillazioni e cicli)

#### 3.1.1 Vista a blocchi (layering)
TBD

#### 3.1.2 Mappatura ai livelli di controllo impianto (A/B/C)
La struttura software ricalca la struttura termotecnica descritta nel capitolo 2:

**Livello A – Regime (Modalità impianto)**

Implementato dal Regime Manager, che decide tra HEATING, COOLING, DEHUM-ASSIST, VENT ONLY applicando:
* bande morte e tempi minimi di permanenza (anti ping-pong caldo/freddo),
* condizioni di sicurezza (CondRisk, allarmi DP, limiti acqua).

**Livello B – Produzione primaria (PDC + puffer)**

Implementato dal Production Controller: calcola e invia setpoint alla PDC (mode + setpoint acqua primario) orientati a:
* “dolcezza” (efficienza) compatibile col fabbisogno più esigente,
* stabilità (puffer piccolo → anti-ciclo rigoroso).

**Livello C – Distribuzione secondaria (MIX radiante + DIR VMC)**

Implementato dai Distribution Controllers:
* MIX (radiante)
    * pompa ON/OFF + posizione valvola 3-vie per ottenere T_sup_rad,target vincolata dal dew point guard e dalle richieste di zona.
* DIR (VMC)
    * pompa ON/OFF quando la VMC richiede acqua (heating/cooling/dehum); la capacità aria resta gestita dalla logica della VMC (programma, dew point target, ΔDP, velocità).

#### 3.1.3 Dati, “grandezze di stato” e modello interno

L’integrazione mantiene un modello interno coerente con le grandezze di stato già definite (cap. 2.8), tipicamente organizzato in “snapshot” atomici per evitare decisioni su dati incoerenti:
* Stato per zona: T_air,z, RH_z, DP_z, (stima MRT_z, T_op,z)
* Stato esterno: T_out, RH_out, DP_out
* Stato idronico: T_sup_rad, T_ret_rad, T_sup_vmc, T_ret_vmc, T_pdc_out/in
* Indici: DP_max = max(DP_z), CondRisk (derivato)

Stato attuatori: 
* pompa MIX/DIR, valvola 3-vie (0–100), valvole di zona (open/close), stati PDC e VMC

Nota critica (coerenza con il testo): in raffrescamento radiante la variabile governante è la distanza dalla condensa, non l’errore di temperatura. Quindi il control core tratta DP_max/CondRisk come vincolo primario che può “schiacciare” la richiesta sensibile.

#### 3.1.4 Eventi, trigger e ciclo di controllo

L’orchestrazione è efficace solo se il ciclo di controllo è deterministico e non “nervoso”. In pratica l’integrazione combina:

Trigger evento (reattivi)
* variazioni significative di DP_max / RH (rischio condensa),
* richiesta VMC (heating/cooling/dehum),
* apertura/chiusura zone (domanda sensibile),
+ fault/allarmi (DP alarm, anomalie sensori).

Trigger temporali (periodici)
* controllo lento per sistemi inerziali (radiante): aggiornamenti a passo “largo” e con rate limit,
* refresh di robustezza: anti-stallo pompe/valvole, pump overrun, riconciliazione stati.

Il ciclo tipico è:
1. Acquisisci tutti gli stati necessari (zona, esterno, idronico, device)
2. Deriva DP, DP_max, CondRisk, e altre metriche
3. Decidi il regime (A) e i setpoint (B/C) applicando vincoli e priorità
4. Applica comandi in modo idempotente (solo se cambia davvero qualcosa)
5. Registra esito, motivazioni e timestamp (diagnostica/persistenza)

#### 3.1.5 Interfacce Home Assistant: entità e comandi

Input (lettura):
* sensori T/UR per zona → calcolo DP_z
* sensori esterni T/UR → DP_out
* sensori idronici su puffer e sulle due pompe (mandata/ritorno)
* stati/flag Modbus: richieste VMC (plant water, cooling/heating/dehum), allarmi dew point, stato compressore PDC

Output (scrittura):
* PDC: ON/OFF, mode (HEAT/COOL), setpoint acqua e (se disponibile) delta T
* VMC: programma (winter/summer/off), dew point target, ΔDP, enable dehum, setpoint ambiente/UR (se usati)
* Pompe: ON/OFF
* Valvola 3-vie MIX: 0–100%
* Valvole di zona: OPEN/CLOSE (in base a richiesta e strategia)

Architetturalmente, la parte “actuation” deve essere difensiva:
* clamp limiti (es. T_sup_rad,target ≥ DP_max + margine + Δsurf↔acqua)
* isteresi e tempi minimi (anti-ciclo compressore e anti-rimbalzo valvole)
* priorità comandi (in DEHUM-ASSIST la VMC “vince” sul radiante)

#### 3.1.6 Sicurezza, degradazione e fail-safe

Dato che i sensori possono essere rumorosi o fallire, la logica include modalità conservative:

* Se DP_max non è affidabile (sensore mancante/outlier):
    * innalza T_sup_rad,target (approccio “safe”) oppure inibisci il raffrescamento radiante e usa VMC.

* Se allarme dew point VMC o CondRisk HIGH:
    * stop/inibizione cooling radiante + priorità deumidifica (DEHUM-ASSIST) + ripartenza con isteresi temporale.

* Anti short-cycling PDC:
    * vincoli minimi ON/OFF + controllo su puffer (fondamentale con 25 L).

* Reconciliation:
    * se un comando fallisce o lo stato reale non converge, l’integrazione rientra in uno stato “safe” e ritenta con backoff.

#### 3.1.7 Principi progettuali (per evitare un BMS “fragile”)

* Separation of concerns: acquisizione/derivazioni ≠ decisione ≠ attuazione
* Idempotenza: stesso input ⇒ stessi comandi; evita “martellamento” Modbus
* Rate limiting: il radiante è lento, non va inseguito come uno split
* Osservabilità: ogni decisione deve avere un reason code (es. “CondRisk HIGH → DEHUM-ASSIST”)
* Safety-first: in estate la logica non “negozia” con la condensa


















TBD
---

