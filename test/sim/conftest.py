# test/sim/conftest.py
def pytest_addoption(parser):
    parser.addoption(
        "--sim-input",
        action="store",
        default=None,
        help="Percorso alternativo al file JSON di input per le simulazioni comfort band.",
    )
    parser.addoption(
        "--sim-override",
        action="append",
        default=[],
        help=(
            "Override puntuale dotted-key=valore (ripetibile). "
            "Es: --sim-override global.default_t_op=19.5"
        ),
    )