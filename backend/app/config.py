"""Filesystem locations and tunables shared by the whole backend.

Everything this service reads is on the HOST filesystem. The GANIL DAQ itself
runs inside the `stark_daq` container, but its run directory is a bind mount of
`/data1` and its run-control logs are written there too -- so nothing here needs
`docker exec`, and the service cannot disturb the acquisition even by accident.

That is not only convenience. The container's PID 1 does not reap orphans, so
every process started inside it that outlives its parent becomes a permanent
zombie against a 4096-process rlimit shared with the DAQ. A monitor that shelled
into the container once a second would be a slow leak aimed at the thing it is
watching.
"""

from __future__ import annotations

import os
from pathlib import Path

#: backend/
BACKEND_ROOT = Path(__file__).resolve().parent.parent

#: Where this service's STATE lives -- kept apart from its code so the service is
#: movable as one folder (LILAK SERVICE_CONTRACT §9). Standalone this is
#: ``backend/``; started by the LILAK portal it is the portal's own data dir for
#: this service, handed over as PORTAL_DATA_ROOT + PORTAL_SERVICE.
_portal_root = os.environ.get("PORTAL_DATA_ROOT")
_portal_service = os.environ.get("PORTAL_SERVICE")
STATE_ROOT = (Path(_portal_root) / _portal_service
              if _portal_root and _portal_service else BACKEND_ROOT)

#: Our own state: the elog credentials, and how far we have read each log.
DATA_DIR = Path(os.environ.get("GANIL_DATA_DIR", STATE_ROOT / "data")).resolve()

#: The experiment's acquisition tree. `run/` holds the data files, `log/` the
#: run-control logs. One experiment per service instance: `config_acq.txt` at
#: the root of the mount lists the others, and pointing this at one of them is
#: a matter of changing this path.
ACQ_ROOT = Path(os.environ.get("GANIL_ACQ_ROOT", "/data1/ko2520/acquisition")).resolve()

LOG_DIR = Path(os.environ.get("GANIL_LOG_DIR", ACQ_ROOT / "log")).resolve()
RUN_DIR = Path(os.environ.get("GANIL_RUN_DIR", ACQ_ROOT / "run")).resolve()
MERGED_DIR = RUN_DIR / "merged"

#: The run-control GUI's log: `Started run #N`, the operator's note, `Stopped
#: run #N`. This is the only place the note exists.
RCC_GUI_LOG = LOG_DIR / "rcc_gui.txt"

#: The run-control server's log: the storage file each run wrote, and the ECC
#: and NARVAL errors. Used for detail, never for the run boundary itself --
#: rcc_gui.txt is what the operator saw.
RCC_LOG = LOG_DIR / "rcc.txt"

#: Built frontend (``frontend/dist``). Served at / when present.
STATIC_DIR = Path(
    os.environ.get("GANIL_STATIC_DIR", BACKEND_ROOT.parent / "frontend" / "dist")
).resolve()

#: How often the logs are re-read and the live run's file re-stat'ed. The DAQ
#: writes a run boundary in well under a second and then nothing for minutes;
#: two seconds is inside anybody's idea of "now" and is four stat() calls a
#: minute against a disk that is also taking the data.
POLL_INTERVAL = float(os.environ.get("GANIL_POLL_INTERVAL", "2.0"))

#: How often the live view is pushed to open browsers.
BROADCAST_INTERVAL = float(os.environ.get("GANIL_BROADCAST_INTERVAL", "2.0"))

#: A `Started run #N` line and the note under it are one log event written in
#: one go, but a poll can land between the two writes. Rather than push a run to
#: elog with an empty note that the next poll would have filled in, a start is
#: held for this long -- or until the next timestamped line proves the note is
#: complete, whichever comes first.
START_SETTLE_SECONDS = float(os.environ.get("GANIL_START_SETTLE", "4.0"))

#: Samples of the live file's size kept for the rate readout, at POLL_INTERVAL
#: each. Ten minutes is enough to see a rate change and cheap to hold.
RATE_HISTORY = int(os.environ.get("GANIL_RATE_HISTORY", "300"))

#: Attempts returned by /api/runs when the caller does not say.
DEFAULT_RUN_LIMIT = 200

#: The GANIL processes that make up a working DAQ, as (label, match) pairs.
#: Matched against the full command line of every process on the host -- the
#: container shares no PID namespace but its processes are still visible in the
#: host's /proc, which is how this is read without entering the container.
DAQ_PROCESSES: tuple[tuple[str, str], ...] = (
    ("Run control server", "RunControlServer"),
    ("Run control GUI", "IHMRC.jar"),
    ("Run control log", "RCLOG"),
    ("ECC SOAP server", "getEccSoapServer"),
    ("NARVAL coordinator", "sub_system_coordinator"),
    ("CoBo catcher", "gnarval_cobo_catcher"),
    ("MFM storer", "gnarval_mfm_storer"),
    ("Naming service", "narval_naming_service"),
    ("CORBA naming", "po_cos_naming"),
    ("Central log", "central_log"),
    ("AWS shell", "aws_shell"),
)

#: The container the DAQ runs in. Only its name is used, and only to report
#: whether it is up -- nothing is ever executed inside it.
CONTAINER_NAME = os.environ.get("GANIL_CONTAINER", "stark_daq")
