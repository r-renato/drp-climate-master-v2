from .plant_regime_pipeline import pipeline_from_env


def main() -> int:
    """
    Test entrypoint.

    Requisiti:
      - env: INFLUX_URL, INFLUX_ORG, INFLUX_TOKEN, INFLUX_BUCKET
        (se non li metti, userà i default url/org/bucket ma token vuoto -> fallirà)
    """
    pipeline = pipeline_from_env(
        start="-730d",
        stop="now()",
        debug=True,                 # log dettagliati
        power_threshold_w=250.0,     # come nei tuoi test
        duty_min=0.20,
        cool_vmc_duty_min=None,      # usa duty_min
    )

    try:
        # Se vuoi anche i dettagli (obs/duty/frame), puoi chiamare i pezzi singoli:
        raw = pipeline.load_raw()
        norm = pipeline.normalize(raw)
        obs_daily, duty_daily, frame = pipeline.build_observed_daily_regime(norm)

        # Outdoor daily mean
        T_out_daily = norm["T_out"].resample("1D").mean()

        # Fit regime model
        result = pipeline.grid_search_regime(
            T_out_daily=T_out_daily,
            obs_regime_daily=obs_daily,
        )

        # --- stampa risultati ---
        print("\n=== SUMMARY ===")
        print("Observed regimes counts (plant-aware):")
        print(result.obs_counts)

        best = result.best
        print("\nBest config:")
        print(best.cfg)

        print("\nScore:")
        print(f"  loss={best.loss:.3f}  err={best.err:.3f}  pen={best.pen:.3f}")
        print(f"  switches={best.switches}/{best.days}  common_days={result.common_days}")

        print("\nTop 10:")
        for i, c in enumerate(result.top10, start=1):
            print(
                f"{i:02d}) loss={c.loss:.3f} err={c.err:.3f} pen={c.pen:.3f} "
                f"switches={c.switches}/{c.days} "
                f"tau={c.cfg.tau_days:.1f} "
                f"hon={c.cfg.heating_on:.1f} hoff={c.cfg.heating_off:.1f} "
                f"con={c.cfg.cooling_on:.1f} coff={c.cfg.cooling_off:.1f}"
            )

        # --- opzionale: salva CSV per ispezione ---
        try:
            duty_daily.to_csv("duty_daily.csv", index=True)
            obs_daily.to_csv("obs_daily.csv", index=True, header=["observed_regime"])
            # frame è grande: salva solo se ti serve
            # frame.to_csv("frame_5m.csv", index=True)
            print("\nSaved: duty_daily.csv, obs_daily.csv")
        except Exception as e:
            print(f"\n[WARN] Could not save CSVs: {e}")

        return 0

    except Exception as e:
        print("\n[ERROR] Pipeline failed:")
        print(repr(e))
        raise

    finally:
        # chiudi Influx client
        pipeline.reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
