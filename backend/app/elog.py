"""This service as a LILAK elog SYSTEM.

A *service* answers when elog asks. A *system* also speaks first: it owns a run
and tells elog when one starts and when it ends, so the elog's run number and
the DAQ's are the same number by construction rather than by someone retyping
it at four in the morning.

This service is the system for the GANIL DAQ even though it does not own the
run -- the run control inside the container does, and it has no way to tell a
logbook anything. Nothing else on this host knows a run has begun, so the
choice is between a watcher announcing it and nobody announcing it.

That has one honest consequence worth stating plainly: **elog cannot command a
run back.** There is no path from here into the DAQ, by design and by the fact
that the only entry point is a run-control GUI. Registration therefore buys the
run boundaries and the operator's note, not remote control.

Three things arrive on one endpoint, `POST /api/elog`:

    {"event": "elog_handshake"}    -> who we are (Discover, at registration)
    {"event": "elog_credentials"}  -> elog's address and our API token, saved
    {"mode": "task"|"snapshot"|"realtime", "run_number": N, ...}
                                   -> elog ASKING for the run status

and one outbound call, `POST <elog>/api/logs` with that token, on each boundary.

The third is worth spelling out, because "a system pushes" reads as if it only
pushes. It does not: elog reaches a system on the same `request_url` a plain
service uses (`utils_webhook.fetch_service`), and `fire_webhook_fills` resolves
a format's owner through `fmt.system_id or fmt.subsystem_id`, so a task log on
one of this system's own run formats is filled by asking us. A request envelope
carries no `event` key, which is how the two are told apart.

elog gives up after **5 seconds** and `realtime` mode repeats as often as twice
a second, so the reply is assembled only from state the watcher already holds --
no file is read, nothing is polled, on the request path.

**Nothing here may disturb the watch.** elog being down, slow, or not yet
registered is the ordinary case, not an error: every push happens on its own
daemon thread and every failure is logged and dropped.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.error
import urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import portal_peer
from .config import ACQ_ROOT, DATA_DIR
from .runs import Attempt

log = logging.getLogger("ganil.elog")

router = APIRouter(tags=["elog"])

#: Where elog's address and our token are kept. Written 0600: the token can
#: post logs as this system.
CREDENTIALS_FILE = DATA_DIR / "elog_credentials.json"

#: elog's own log_type map (routes/logs.py). Only the two boundaries are pushed
#: from here; Init and Monitoring are the operator's to write.
LOG_TYPE_START = 12
LOG_TYPE_END = 13

#: Long enough for a busy elog, short enough that a stuck one cannot pile up
#: threads across a shift.
TIMEOUT = 5.0

#: A start announced more than this long after it happened was not observed --
#: it was adopted from the log at startup. Well above START_SETTLE.
JOINED_LATE_SECONDS = 60.0

#: The fields the Start-of-run log carries, and the only two the DAQ actually
#: knows at the moment a run begins: which run it is, and why it was started.
#: These keys must exist on the "Start of GANIL run" format in elog -- a key the
#: format does not declare is stored on the entry but nothing renders it.
FIELD_KEYS = ("run_number", "note")

_lock = threading.Lock()


# ── Credentials ──────────────────────────────────────────────────────────────

def load_credentials() -> dict[str, str]:
    """elog's address and our token, or {} before registration."""
    try:
        data = json.loads(CREDENTIALS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_credentials(elog_url: str, elog_token: str) -> None:
    with _lock:
        CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CREDENTIALS_FILE.write_text(
            json.dumps({"elog_url": elog_url, "elog_token": elog_token}, indent=2) + "\n"
        )
        try:
            CREDENTIALS_FILE.chmod(0o600)
        except OSError:                   # pragma: no cover - exotic filesystem
            log.warning("could not restrict permissions on %s", CREDENTIALS_FILE)


def registered() -> bool:
    creds = load_credentials()
    return bool(creds.get("elog_url") and creds.get("elog_token"))


# ── The endpoint elog calls ──────────────────────────────────────────────────

@router.post("/api/elog", summary="LILAK elog handshake and credentials")
async def elog_endpoint(request: Request):
    try:
        envelope = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "body must be JSON"})
    if not isinstance(envelope, dict):
        return JSONResponse(status_code=400, content={"detail": "body must be a JSON object"})

    event = envelope.get("event")

    if event == "elog_handshake":
        log.info("elog handshake from %s", envelope.get("elog_url") or "?")
        return {
            "name": "GANIL DAQ",
            "description": ("GANIL run control, watched — pushes run start and end "
                            "with the operator's note, and answers with the run "
                            "going now when asked. Read-only: it cannot start or "
                            "stop a run."),
            "hostname": socket.gethostname(),
            "directory": str(ACQ_ROOT),
            # The whole point: elog creates the Init/Start/End/Monitoring run
            # formats for us and issues a token.
            "is_system": True,
            # Empty on purpose. A system's fields are the run formats elog seeds
            # itself; declaring more here would create a second, unused format.
            "log_fields": [],
        }

    if event == "elog_credentials":
        elog_url = str(envelope.get("elog_url") or "").strip()
        token = str(envelope.get("elog_token") or "").strip()
        if not elog_url or not token:
            return JSONResponse(status_code=400,
                                content={"detail": "elog_url and elog_token are both required"})
        save_credentials(elog_url, token)
        log.info("elog credentials stored; will push runs to %s", elog_url)
        return {"ok": True}

    # No `event` key: this is elog asking for data, not announcing itself.
    if event is None and ("mode" in envelope or "format_id" in envelope):
        return status_reply(envelope)

    return JSONResponse(status_code=400, content={"detail": f"unknown event {event!r}"})


# ── Answering a request ──────────────────────────────────────────────────────
#
# The keys below are stored on the entry whether or not the format declares
# them; only `title` and `body` are builtins that always render. To see the rest
# in the logbook, add them to the "Monitoring GANIL run" format in elog's format
# editor: `run_number` and `elapsed_s` and `size_bytes` as number entries,
# `state` and `note` as text. Everything essential is in the title and the body
# regardless, which is why those carry the run number in words.

REQUEST_FIELD_KEYS = ("run_number", "note", "state", "elapsed_s", "size_bytes")


def status_reply(envelope: dict) -> dict:
    """What is going on, as elog's webhook response shape.

    `mode` is "task", "snapshot" or "realtime". A task fill names the run it is
    filling for, and that is answered about THAT run -- a task log opened for
    run 78 must not quietly report run 81 because 81 happens to be the one
    running now. Snapshot and realtime mean "now", so they get the live run.
    """
    if _monitor is None:                  # pragma: no cover - install() not run
        return {"title": "GANIL DAQ", "body": "the watcher is not running"}

    mode = str(envelope.get("mode") or "task")
    asked = envelope.get("run_number")

    attempt = None
    if mode == "task" and isinstance(asked, int):
        # Newest attempt at that number: a run started twice is asked about as
        # the run it ended up being.
        attempt = next((a for a in _monitor.attempts() if a.run == asked), None)
    if attempt is None:
        attempt = _monitor.live_attempt()
    if attempt is None:
        attempt = next(iter(_monitor.attempts(limit=1)), None)

    if attempt is None:
        return {
            "title": "No run on record",
            "body": "The run-control log names no run yet.",
            "fields": {"state": "none"},
        }

    live = attempt.is_live
    state = "running" if live else "finished"
    elapsed = attempt.duration_s
    if elapsed is None:
        from datetime import datetime, timezone
        elapsed = (datetime.now(timezone.utc) - attempt.started).total_seconds()

    rate = _monitor.rate_bytes_per_s() if live else None

    lines = [f"Run {attempt.run} — {state}",
             f"started: {attempt.started.isoformat()}"]
    if not live and attempt.stopped:
        lines.append(f"stopped: {attempt.stopped.isoformat()}")
    lines.append(f"elapsed: {elapsed:,.0f} s")
    if attempt.files:
        lines.append(f"written: {_size(attempt.size)} in {len(attempt.files)} file(s)")
        if rate is not None:
            lines.append(f"rate: {_size(int(rate))}/s")
    elif attempt.storage_off:
        lines.append("written: nothing — storage was off")
    else:
        lines.append("written: no data file found")

    # A component that is down while a run is going is the thing a shifter most
    # needs to see in a logbook entry, so it is not left to the web page.
    down = [p["label"] for p in _monitor.processes() if not p["running"]]
    if down:
        lines.append("DOWN: " + ", ".join(down))
    if attempt.note:
        lines += ["", attempt.note]

    title = (f"Run {attempt.run} running · {elapsed / 3600:.1f} h"
             if live else f"Run {attempt.run} finished")

    return {
        "title": title,
        "body": "\n".join(lines),
        "fields": {
            "run_number": _number(attempt.run),
            "note": attempt.note,
            "state": state,
            "elapsed_s": _number(round(elapsed)),
            "size_bytes": _number(attempt.size),
        },
    }


# ── Pushing a run boundary ───────────────────────────────────────────────────

def _post(log_type: int, run_number: int, title: str, body: str,
          fields: dict | None = None) -> None:
    """One log into elog. Runs on its own thread; never raises to the caller."""
    creds = load_credentials()
    url, token = creds.get("elog_url"), creds.get("elog_token")
    if not url or not token:
        log.debug("no elog credentials yet; run %s not pushed", run_number)
        return

    try:
        # Resolved per call: elog is portal-managed too, and its port is
        # assigned at start time (see portal_peer).
        target = portal_peer.resolve(url.rstrip("/")) + "/api/logs"
    except portal_peer.PortalPeerError as err:
        log.warning("cannot reach elog for run %s: %s", run_number, err)
        return

    envelope = {
        "title": title,
        "body": body,
        "log_type": log_type,
        "run_number": run_number,
        "is_auto": True,
    }
    if fields:
        envelope["format_fields"] = fields
    request = urllib.request.Request(
        target, data=json.dumps(envelope).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            if answer.status not in (200, 201):
                log.warning("elog answered %s for run %s", answer.status, run_number)
            else:
                log.info("run %s: %s pushed to elog", run_number,
                         "start" if log_type == LOG_TYPE_START else "end")
    except (urllib.error.URLError, OSError, ValueError) as err:
        # The logbook being unreachable must never be the watcher's problem.
        log.warning("could not push run %s to elog: %s", run_number, err)


def _push(log_type: int, run_number: int, title: str, body: str,
          fields: dict | None = None) -> None:
    threading.Thread(
        target=_post, args=(log_type, run_number, title, body, fields),
        name=f"elog-push-{run_number}", daemon=True,
    ).start()


def _number(value: float | int | None) -> float | None:
    """A number for elog, sent as a PLAIN number rather than a wrapper.

    `normalize_format_fields` only canonicalizes the keys the *format* declares
    as `number_entry`; anything else passes through untouched and is rendered by
    the UI as whatever it is. So a `{"single": 81}` sent for a key the format
    does not declare reaches the logbook as the literal string
    `[object Object]`, which is what happened to run_number, elapsed_s and
    size_bytes on the first Monitoring entry.

    A plain number is right in BOTH cases: `normalize_number_entry` documents
    "a plain number (treated as single)" and turns it into exactly the canonical
    shape `{"single": x}` would have produced, so declaring the field later
    changes nothing here; and until somebody declares it, the logbook shows
    `81` instead of `[object Object]`.
    """
    return None if value is None else float(value)


def _size(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# ── What the watcher calls ───────────────────────────────────────────────────

def run_started(attempt: Attempt) -> None:
    """Tell elog a run has begun, with the note it was started with.

    The note goes up twice on purpose -- as a `format_fields` entry, which a
    filter or a plot can reach, and in the body, which is what a shifter reads
    on a phone. It is the only thing anybody writes about a GANIL run at the
    moment it starts, so losing it to a field nothing renders would defeat the
    exercise.
    """
    from datetime import datetime, timezone
    lines = [f"run {attempt.run} started {attempt.started.isoformat()}"]

    # A start announced long after it happened is one this watcher joined
    # mid-run -- it was started before the service was. Saying so keeps the
    # logbook honest about why the entry's own timestamp is hours later.
    age = (datetime.now(timezone.utc) - attempt.started).total_seconds()
    if age > JOINED_LATE_SECONDS:
        lines.append(f"(the monitoring service joined {age / 3600:.1f} h into this "
                     f"run; the run itself began at the time above)")

    if attempt.note:
        lines += ["", attempt.note]
    else:
        lines += ["", "(started with no note)"]

    # The DAQ's name in the title: a logbook carries several systems' runs and
    # "Run 150" alone does not say whose.
    _push(LOG_TYPE_START, attempt.run, f"GANIL DAQ · Run {attempt.run}", "\n".join(lines),
          {"run_number": _number(attempt.run), "note": attempt.note})


def run_ended(attempt: Attempt) -> None:
    """Tell elog the run is over, and what it left on disk."""
    lines = []
    if attempt.duration_s is not None:
        lines.append(f"elapsed: {attempt.duration_s:,.0f} s")
    if attempt.files:
        lines.append(f"size: {_size(attempt.size)} in {len(attempt.files)} file(s)")
        for data in attempt.files:
            lines.append(f"  {data.name}  ({_size(data.size)})")
    elif attempt.storage_off:
        lines.append("no data file: storage was off for this run")
    else:
        lines.append("no data file found for this run")
    if attempt.note:
        lines += ["", attempt.note]

    # This went up with an EMPTY title, so the end of a run — the entry you most
    # want to find — was a blank row in the list.
    _push(LOG_TYPE_END, attempt.run, f"GANIL DAQ · Run {attempt.run} — finished",
          "\n".join(lines), {"run_number": _number(attempt.run)})


#: Set by install(). The request path reads it; nothing here polls.
_monitor = None


def install(monitor) -> None:
    """Announce this monitor's run boundaries to elog, and answer about them."""
    global _monitor
    _monitor = monitor
    monitor.on_run_started.append(run_started)
    monitor.on_run_ended.append(run_ended)
