"""Componente CloMetProvider — stima adattiva di CLO e MET per zona.

Fornisce stime fisicamente fondate dei parametri di abbigliamento (CLO)
e metabolismo (MET) per il calcolo PMV/PPD (ISO 7730 / ASHRAE 55).

Moduli pubblici
---------------
- model     : dataclass di output (CloEstimate, MetEstimate, ComfortParameters)
              ed enum RoomType, TimeOfDay.
- config    : CloMetConfig con tutti i parametri numerici nominati.
- provider  : ComfortParameterProvider — punto di accesso principale.
"""

from .provider import ComfortParameterProvider
