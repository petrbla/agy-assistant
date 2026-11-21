"""Asistent Antigravity – orchestrátor AI zaměstnance."""

from pathlib import Path


def _read_version() -> str:
    """Načte verzi aplikace ze souboru `VERSION` v kořenu repozitáře."""
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        text = version_file.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return "0.0.6"


__version__: str = _read_version()
