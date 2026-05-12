# test/unit/conftest.py
from __future__ import annotations
import sys
import pathlib

_ROOT = pathlib.Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))