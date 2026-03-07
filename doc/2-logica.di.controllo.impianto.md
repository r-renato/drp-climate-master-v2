## 2 Logica di controllo impianto

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
