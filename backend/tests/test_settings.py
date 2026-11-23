"""Settings saved from the page: checked, written, applied without a restart.

    python tests/test_settings.py
"""

import os
import tempfile
from pathlib import Path

base = Path(tempfile.mkdtemp())
for name in ("run0826a", "run1201b"):
    (base / "experiments" / name / "acquisition" / "log").mkdir(parents=True)
(base / "experiments" / "notes").mkdir()
os.environ["GANIL_ACQ_ROOT"] = str(base / "experiments" / "run0826a" / "acquisition")
os.environ["GANIL_DATA_DIR"] = str(base / "data")

from app import config, settings  # noqa: E402


def test_settings():
    assert [e["name"] for e in settings.experiments()] == ["run0826a", "run1201b"]
    new = str(base / "experiments" / "run1201b" / "acquisition")
    values, changed = settings.save({"acq_root": new, "rcc_port": "7000"})
    assert changed == ["acq_root", "rcc_port"], changed
    assert config.RCC_GUI_LOG == Path(new).resolve() / "log" / "rcc_gui.txt" and config.RCC_PORT == 7000
    for bad in ({"acq_root": str(base / "experiments" / "notes")}, {"rcc_port": 0}, {"rcc_poll": 0.1}):
        try:
            settings.save(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    first = str(Path(os.environ["GANIL_ACQ_ROOT"]).resolve())
    settings.save({"acq_root": first, "rcc_port": 6911})    # back to the first, its own port
    assert settings.saved()[str(Path(new).resolve())]["rcc_port"] == 7000  # the other kept its own
    settings.save({"acq_root": new, **settings.saved()[str(Path(new).resolve())]})
    settings.apply(settings.validate({"acq_root": first, "rcc_port": 1}))
    settings.load()                                  # the saved file wins again
    assert config.RCC_PORT == 7000 and "run1201b" in str(config.ACQ_ROOT)


if __name__ == "__main__":
    test_settings()
    print("ok")
