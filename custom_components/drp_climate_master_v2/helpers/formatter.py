

def fnum(x, nd=1):
    return f"{x:.{nd}f}" if x is not None else "-"

def fbool(b, on="on", off="off"):
    return on if b is True else (off if b is False else "-")