from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from ...zone.config import RcZoneParams


@dataclass(slots=True)
class RcZoneModel:
    """Modello RC discreto del primo ordine per una singola zona.

    Implementa l'integrazione di Eulero forward dell'equazione::

        dT/dt = (T_out - T) / tau_h + k_active * u

    dove:

    * ``tau_h``     - costante di tempo termica della zona [h].
    * ``k_active``  - guadagno di azionamento [°C/h]: positivo in riscaldamento
      (``params.k_c_per_h``), negativo in raffrescamento (``params.k_cool_c_per_h``).
    * ``u in {0,1}`` - stato valvola (0 = chiusa, 1 = aperta).

    Il modello è **direzionale**: la stessa struttura matematica descrive sia il
    riscaldamento che il raffrescamento cambiando solo il segno di ``k_active``.
    Il chiamante seleziona la direzione tramite il parametro ``cooling`` di
    :meth:`simulate`.

    Stabilità numerica
    ------------------
    L'integrazione di Eulero forward è stabile se ``dt_h / tau_h < 2``.
    Con i default (``dt_minutes=10``, ``tau_h=6``): ``dt_h/tau_h ~= 0.028 << 1``.
    Il metodo è anche accurato (errore di troncamento O(dt^2)).

    Il termine ``k_active * u`` è additivo e non influenza la stabilità: dipende
    solo dal rapporto ``dt_h / tau_h``.

    Punto fisso
    -----------
    Con ``u = 1`` costante la temperatura converge a::

        T_eq = T_out + k_active * tau_h

    * Riscaldamento (T_out=5°C, k=0.8, tau=6h):  T_eq = 5 + 4.8 = 9.8°C
    * Raffrescamento (T_out=21°C, k=-0.603, tau=6h): T_eq = 21 - 3.6 = 17.4°C

    Il dew-point guard blocca il raffrescamento prima che la zona raggiunga T_eq
    in raffrescamento (tipicamente 13-16°C di limite di condensa).
    """

    params: RcZoneParams

    def simulate(
        self,
        *,
        t0_c: float,
        t_out_c: Iterable[float],
        u: Iterable[int],
        dt_minutes: int,
        cooling: bool = False,
    ) -> List[float]:
        """Simula le temperature di zona sull'orizzonte di pianificazione.

        Parametri
        ---------
        t0_c : float  [°C]
            Temperatura operativa di zona al passo k=0 (condizione iniziale).

        t_out_c : Iterable[float]  [°C]
            Traiettoria della temperatura esterna sull'orizzonte (N valori).
            Tipicamente un vettore costante con la temperatura corrente (approx
            flat profile); future versioni possono usare forecast orario.

        u : Iterable[int]  {0, 1}
            Sequenza di comando valvola sull'orizzonte (N valori, 0 o 1).
            Lunghezza uguale a ``t_out_c``.

        dt_minutes : int  [min]
            Passo temporale di discretizzazione. Deve essere coerente con
            ``MpcConfig.dt_minutes``.

        cooling : bool  (default False)
            Seleziona la direzione di azionamento:

            * ``False`` (default): **riscaldamento**. Usa ``params.k_c_per_h``
              (positivo). Comportamento identico alla versione precedente
              (retrocompatibilità garantita).
            * ``True``: **raffrescamento**. Usa ``params.k_cool_c_per_h``
              (negativo). Con valvola aperta la temperatura scende.

            Il parametro non altera la struttura dell'equazione né la stabilità
            numerica: cambia solo il segno del termine di azionamento.

        Ritorna
        -------
        List[float]
            Lista di N temperature [°C], ciascuna T(k+1) dopo l'applicazione
            del comando u(k). Lunghezza uguale alla sequenza ``u``.

        Note
        ----
        Retrocompatibilità: tutti i chiamanti esistenti che non passano ``cooling``
        ottengono il comportamento heating invariato (``cooling=False``).
        """

        tau_h = max(0.25, float(self.params.tau_h))
        # Seleziona guadagno in funzione della direzione operativa.
        # cooling=False (default): riscaldamento, k > 0 -> T sale con valvola aperta.
        # cooling=True: raffrescamento, k < 0 -> T scende con valvola aperta.
        k_active = (
            float(self.params.k_cool_c_per_h)
            if cooling
            else float(self.params.k_c_per_h)
        )
        dt_h = float(dt_minutes) / 60.0

        out: List[float] = []
        t = float(t0_c)
        for to, uk in zip(t_out_c, u):
            # Integrazione Eulero forward:
            # T[k+1] = T[k] + dt_h * ((T_out - T[k]) / tau_h + k_active * u[k])
            dt_term = (float(to) - t) / tau_h
            actuation_term = k_active * (1.0 if int(uk) else 0.0)
            t = t + dt_h * (dt_term + actuation_term)
            out.append(t)

        return out
