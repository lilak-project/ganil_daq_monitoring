"""Settings a controller changes from the page, kept in ``DATA_DIR/settings.json``.

Kept per experiment: each acquisition folder remembers its own run-control
address and polling, so picking an experiment brings its settings back. The
file is ``{"acq_root": <the one in use>, "experiments": {<acq_root>: {...}}}``.

The environment (the portal manifest's ``start.env``) gives the first values;
what is saved here wins over it, and is applied at once -- no restart. The one
that changes most is the experiment: its name is part of the acquisition path
(``…/experiments/run0826a/acquisition``), so a new experiment used to mean a
manifest edit. Now it is a pick from the folders that look like one.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import config

log = logging.getLogger("ganil.settings")

#: The first values, from the environment, before anything was saved.
DEFAULTS = {
    "acq_root": str(config.ACQ_ROOT),
    "rcc_host": config.RCC_HOST,
    "rcc_port": config.RCC_PORT,
    "rcc_poll": config.RCC_POLL,
}

#: Where the experiments sit side by side: two levels above the acquisition
#: tree (…/experiments/<name>/acquisition), unless the environment says.
EXPERIMENTS_DIR = Path(os.environ.get("GANIL_EXPERIMENTS_DIR", config.ACQ_ROOT.parent.parent))


def _file() -> Path:
    return config.DATA_DIR / "settings.json"


def current() -> dict:
    return {
        "acq_root": str(config.ACQ_ROOT),
        "rcc_host": config.RCC_HOST,
        "rcc_port": config.RCC_PORT,
        "rcc_poll": config.RCC_POLL,
    }


def experiments() -> list[dict]:
    """Every folder under EXPERIMENTS_DIR with an `acquisition/log` in it."""
    try:
        folders = sorted(p for p in EXPERIMENTS_DIR.iterdir() if (p / "acquisition" / "log").is_dir())
    except OSError:
        return []
    return [{"name": p.name, "acq_root": str((p / "acquisition").resolve())} for p in folders]


def validate(values: dict) -> dict:
    """The values cleaned, or ValueError naming what is wrong."""
    out = {**current(), **{k: v for k, v in values.items() if k in DEFAULTS}}
    root = Path(str(out["acq_root"])).expanduser().resolve()
    if not (root / "log").is_dir():
        raise ValueError(f"{root} has no log/ folder: not an acquisition tree")
    out["acq_root"] = str(root)
    out["rcc_host"] = str(out["rcc_host"]).strip()
    if not out["rcc_host"]:
        raise ValueError("the run control's host is empty")
    port, poll = int(out["rcc_port"]), float(out["rcc_poll"])
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} is out of range")
    if not 1 <= poll <= 60:
        raise ValueError("ask the run control every 1 to 60 seconds")
    out["rcc_port"], out["rcc_poll"] = port, poll
    return out


def apply(values: dict) -> None:
    root = Path(values["acq_root"])
    config.ACQ_ROOT = root
    config.LOG_DIR = root / "log"
    config.RUN_DIR = root / "run"
    config.MERGED_DIR = config.RUN_DIR / "merged"
    config.RCC_GUI_LOG = config.LOG_DIR / "rcc_gui.txt"
    config.RCC_LOG = config.LOG_DIR / "rcc.txt"
    config.RCC_HOST = values["rcc_host"]
    config.RCC_PORT = int(values["rcc_port"])
    config.RCC_POLL = float(values["rcc_poll"])


#: What each experiment remembers (everything but the folder itself).
PER_EXPERIMENT = ("rcc_host", "rcc_port", "rcc_poll")


def _read() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"acq_root": None, "experiments": {}}
    if "experiments" not in data:            # the first, flat format: one experiment
        root = data.get("acq_root")
        data = {"acq_root": root, "experiments": {root: {k: data[k] for k in PER_EXPERIMENT if k in data}} if root else {}}
    return data


def saved() -> dict:
    """Every experiment's remembered settings, by acquisition folder."""
    return _read()["experiments"]


def load() -> None:
    """At start-up: the experiment in use, with its own settings, over the environment's values."""
    data = _read()
    if not data["acq_root"]:
        return
    try:
        apply(validate({**DEFAULTS, **data["experiments"].get(data["acq_root"], {}), "acq_root": data["acq_root"]}))
        log.info("settings from %s: %s", _file(), current())
    except (ValueError, TypeError) as err:
        log.warning("ignoring %s: %s", _file(), err)


def save(values: dict) -> tuple[dict, list[str]]:
    """Validate, remember under its experiment, apply. Returns the values and the names that changed."""
    before = current()
    after = validate(values)
    data = _read()
    data["acq_root"] = after["acq_root"]
    data["experiments"][after["acq_root"]] = {k: after[k] for k in PER_EXPERIMENT}
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _file().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    apply(after)
    return after, [k for k in after if after[k] != before[k]]
