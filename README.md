# DRP Climate Master v3

Integrazione Home Assistant (custom component) per l'orchestrazione di un impianto HVAC residenziale con
PDC, radiante a soffitto, VMC e valvole di zona. Riscrittura da zero della v2, sviluppata a step rilasciabili.

> **Stato:** in sviluppo — vedi [`docs/roadmap.md`](docs/roadmap.md). Dominio `drp_climate_master_v3`: convive con la v2.

## Sviluppo

```bash
scripts/setup          # venv + dipendenze
source .venv/bin/activate
scripts/test           # ruff + format + pyright + pytest
```

Il codice della v2 da consultare va copiato in `reference/v2/` (cartella ignorata da git, sola lettura).

## Documentazione
- [`docs/roadmap.md`](docs/roadmap.md) — step S0–S10 e stato
- [`docs/architecture.md`](docs/architecture.md) — pipeline a strati e regole di dipendenza
- [`docs/hardware.md`](docs/hardware.md) — inventario HW e vincoli
- [`docs/steps/`](docs/steps) — brief e criteri di uscita di ogni step
- [`CLAUDE.md`](CLAUDE.md) — istruzioni per lavorare con Claude Code (rules, skills e agenti in `.claude/`)
