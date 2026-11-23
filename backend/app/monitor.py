"""The watcher: read the logs, hold the state, announce the boundaries.

One thread polls, everything else reads what it left behind. The logs are
followed by byte offset rather than re-parsed whole -- rcc.txt is already 2 MB
and grows all shift -- and the run directory is re-scanned each poll because it
is a hundred-odd entries and the live file's size is the whole point.

Two rules this module exists to keep:

**Never push history.** The first time this service runs it adopts whatever the
log already says and pushes none of it. A logbook that filled with eighty-three
runs from last week the moment somebody started a monitoring service would be
worse than no logbook. What has been announced is remembered across restarts in
`data/watch_state.json`.

**Never push half a note.** `Started run #81` and the operator's note are one
event written in one go, but a poll can land between the two writes. A start is
held until the log proves the note is complete -- the next timestamped line --
or until START_SETTLE_SECONDS have passed, whichever comes first.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config, rcc
from .rcclog import GuiEvent, iter_tail, parse_gui, parse_rcc
from .runs import Attempt, DataFile, build_attempts, scan_data_files

log = logging.getLogger("ganil.monitor")

#: How often the DAQ's processes are re-checked. Slower than the log poll: a
#: /proc walk is a few hundred opens, and a component that dies is not news
#: that goes stale in two seconds.
PROCESS_INTERVAL = 10.0


@dataclass
class PendingStart:
    """A `Started` seen but not yet announced, waiting for its note."""

    event: GuiEvent
    first_seen: float


class Monitor:
    """Everything this service knows, refreshed by one background thread."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._events: list[GuiEvent] = []
        self._open_event: GuiEvent | None = None
        self._attempts: list[Attempt] = []
        self._files: list[DataFile] = []
        self._orphans: list[DataFile] = []
        self._errors: list[tuple[datetime, str]] = []
        self._storage: list = []

        self._gui_offset = 0
        self._rcc_offset = 0

        self._pending: PendingStart | None = None
        self._announced_start: str | None = None   # key of the last start pushed
        self._announced_end: str | None = None
        self._adopted = False

        self._rate: deque[tuple[float, int]] = deque(maxlen=config.RATE_HISTORY)
        self._processes: list[dict] = []
        self._processes_at = 0.0
        self._last_poll: datetime | None = None
        self._error: str | None = None

        #: Called with an Attempt when a run starts / ends. Registered by the
        #: elog module; a failure in one must never stop the loop.
        self.on_run_started: list[Callable[[Attempt], None]] = []
        self.on_run_ended: list[Callable[[Attempt], None]] = []

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._load_state()
        self._thread = threading.Thread(target=self._loop, name="ganil-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    def reset(self) -> None:
        """Forget everything read: the acquisition tree changed (settings.py).

        The new tree is read as on a first start -- its finished runs are
        adopted without being pushed to elog, a run still going is announced.
        """
        with self._lock:
            self._events, self._open_event = [], None
            self._attempts, self._files, self._orphans = [], [], []
            self._errors, self._storage = [], []
            self._gui_offset = self._rcc_offset = 0
            self._pending = None
            self._announced_start = self._announced_end = None
            self._adopted = False
            self._rate.clear()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:             # a bad line must not end the watch
                log.exception("refresh failed")
            self._stop.wait(config.POLL_INTERVAL)

    # ── the poll ─────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        with self._lock:
            self._read_logs()
            self._files = scan_data_files(config.RUN_DIR)
            self._attempts = build_attempts(self._all_events(), self._files, self._storage)
            self._attach_page_notes()
            self._mark_orphans()
            self._sample_rate()
            self._last_poll = datetime.now(timezone.utc)
            self._error = None
            transitions = self._transitions()
        # Callbacks run outside the lock: an elog push must not be able to
        # block the next poll, however slow the logbook is.
        for kind, attempt in transitions:
            self._fire(kind, attempt)

    def _read_logs(self) -> None:
        """Consume whatever was appended to both logs since the last poll.

        The GUI log is read from the byte where the last *incomplete* event
        began, not from end of file. An event whose note is still being written
        has to be re-read whole next poll, and an event always starts at a
        header line, so that offset is a clean boundary to resume from.
        """
        if not config.RCC_GUI_LOG.exists():
            self._error = f"{config.RCC_GUI_LOG} does not exist"
            return

        try:
            size = config.RCC_GUI_LOG.stat().st_size
        except OSError as err:
            self._error = f"cannot read {config.RCC_GUI_LOG}: {err}"
            return
        if size < self._gui_offset:
            # Rotated. Everything we had is still in self._events; start over
            # on the new file rather than reading a truncated middle.
            log.info("%s was rotated; re-reading from the start", config.RCC_GUI_LOG.name)
            self._gui_offset = 0
            self._events = []
        if size > self._gui_offset:
            try:
                with config.RCC_GUI_LOG.open("rb") as handle:
                    handle.seek(self._gui_offset)
                    text = handle.read(size - self._gui_offset).decode("utf-8", errors="replace")
            except OSError as err:
                self._error = f"cannot read {config.RCC_GUI_LOG}: {err}"
                return
            self._commit_gui(parse_gui(text, base_offset=self._gui_offset))

        for text, offset in iter_tail(config.RCC_LOG, self._rcc_offset):
            facts = parse_rcc(text)
            self._rcc_offset = offset
            self._storage.extend(facts.storage)
            self._errors.extend(facts.errors)
            del self._storage[:-2000]
            del self._errors[:-200]

    def _commit_gui(self, parsed: list[GuiEvent]) -> None:
        """Keep the finished events; hold the unfinished one for the next poll.

        `end_offset` of a closed event is where the next one begins, so the
        offset to resume from is the *previous* event's end -- the byte the open
        event's own header starts at.
        """
        if not parsed:
            return
        if parsed[-1].closed:
            self._events.extend(parsed)
            self._open_event = None
            self._gui_offset = parsed[-1].end_offset
            return

        self._open_event = parsed[-1]
        self._events.extend(parsed[:-1])
        # Rewind to the open event's header: its predecessor's end_offset, or
        # where this read began when it is the only event in the chunk.
        self._gui_offset = parsed[-2].end_offset if len(parsed) > 1 else self._gui_offset

    def _all_events(self) -> list[GuiEvent]:
        """Committed events plus the one still being written, which is the live run."""
        return self._events + ([self._open_event] if self._open_event else [])

    def _attach_page_notes(self) -> None:
        """A run started from this page with a note: the note is ours, not the
        GUI's (rcc.py). It goes to the run with no note that started within
        half a minute after it was written -- the Start it was sent with."""
        page = rcc.notes()
        if not page:
            return
        for attempt in self._attempts:
            if attempt.note:
                continue
            started = attempt.started.timestamp()
            for entry in reversed(page):
                if 0 <= started - entry["at"] <= 30:
                    attempt.note = entry["note"]
                    break

    def _mark_orphans(self) -> None:
        claimed = {f.path for a in self._attempts for f in a.files}
        self._orphans = [f for f in self._files if f.path not in claimed]

    def _sample_rate(self) -> None:
        live = self.live_attempt()
        if live is None:
            self._rate.clear()
            return
        self._rate.append((time.time(), live.size))

    def rate_history(self, points: int = 60) -> list[float]:
        """Write rate between consecutive samples, in MB/s, newest last.

        The deque holds (time, total size); a rate needs the difference, and a
        size that went backwards (a new run, a truncated file) is a break, not
        a negative rate, so it is dropped rather than drawn.
        """
        with self._lock:
            samples = list(self._rate)
        rates: list[float] = []
        for (t0, s0), (t1, s1) in zip(samples, samples[1:]):
            span = t1 - t0
            if span <= 0 or s1 < s0:
                continue
            rates.append((s1 - s0) / span / 1e6)
        return rates[-points:]

    def rate_bytes_per_s(self) -> float | None:
        """Write rate of the live run over the samples held, or None.

        Measured over the whole window rather than the last two samples: NARVAL
        flushes in bursts, and a two-sample rate alternates between zero and a
        spike that means nothing to anyone watching.
        """
        if len(self._rate) < 2:
            return None
        (t0, s0), (t1, s1) = self._rate[0], self._rate[-1]
        if t1 <= t0:
            return None
        return max(0.0, (s1 - s0) / (t1 - t0))

    # ── transitions ──────────────────────────────────────────────────────────

    @staticmethod
    def _key(attempt: Attempt) -> str:
        """Identity of an attempt, stable across restarts and unique per start."""
        return f"{attempt.run}@{attempt.started.isoformat()}"

    def _transitions(self) -> list[tuple[str, Attempt]]:
        """What has to be announced, given what already has been."""
        attempts = self._attempts
        if not attempts:
            return []

        if not self._adopted:
            # First ever poll with no saved state. Finished runs are adopted
            # silently -- a logbook that filled with last week's runs would be
            # worse than no logbook. A run that is STILL GOING is different, and
            # is announced: elog derives "the current run" from the highest run
            # number on a log entry (`_current_run_number`), so a main system
            # that swallows the run in progress leaves the logbook with no run
            # number at all until the next run starts. The start log says it was
            # joined mid-run; see elog.JOINED_LATE_SECONDS.
            self._adopted = True
            if self._announced_start is None:
                last = attempts[-1]
                if last.is_live:
                    log.info("adopting %d finished attempts silently; run %s is still "
                             "going and will be announced to elog.",
                             len(attempts) - 1, last.run)
                    # Fall through: the normal path below announces it.
                else:
                    self._announced_start = self._key(last)
                    self._announced_end = self._key(last)
                    self._save_state()
                    log.info("adopted the log as it stands; %d attempts will not be "
                             "pushed, and no run is going.", len(attempts))
                    return []

        out: list[tuple[str, Attempt]] = []
        latest = attempts[-1]

        # A start is announced once its note has settled.
        if self._key(latest) != self._announced_start:
            open_event = self._open_event
            # A run recovered from the server log has no note and never will --
            # the note lives only in the GUI log, which is precisely the thing
            # that failed to write. Waiting on the GUI's open event for one is
            # worse than pointless: that event belongs to an EARLIER run, so the
            # `event.time != latest.started` test below restarted the settle
            # timer on every poll and the run was never announced at all. Runs
            # 213 to 217 sat unannounced for thirty-six hours behind run 212's
            # start line, the last thing the GUI ever wrote.
            settled = (latest.stopped is not None or latest.from_server_log
                       or open_event is None or open_event.closed)
            if not settled:
                if self._pending is None or self._pending.event.time != latest.started:
                    self._pending = PendingStart(open_event, time.time())
                settled = (time.time() - self._pending.first_seen) >= config.START_SETTLE_SECONDS
                if settled:
                    log.info("run %s: note did not settle in %.0fs; pushing what there is",
                             latest.run, config.START_SETTLE_SECONDS)
            if settled:
                self._pending = None
                self._announced_start = self._key(latest)
                self._save_state()
                out.append(("started", latest))

        # An end is announced when the attempt we announced the start of stops.
        if (latest.stopped is not None
                and self._key(latest) == self._announced_start
                and self._key(latest) != self._announced_end):
            self._announced_end = self._key(latest)
            self._save_state()
            out.append(("ended", latest))
        return out

    def _fire(self, kind: str, attempt: Attempt) -> None:
        for callback in (self.on_run_started if kind == "started" else self.on_run_ended):
            try:
                callback(attempt)
            except Exception:
                log.exception("%s callback failed for run %s", kind, attempt.run)

    # ── persisted state ──────────────────────────────────────────────────────

    def _state_file(self) -> Path:
        return config.DATA_DIR / "watch_state.json"

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_file().read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self._announced_start = data.get("announced_start")
        self._announced_end = data.get("announced_end")
        self._gui_offset = 0              # offsets are not saved: re-reading the
        self._rcc_offset = 0              # tail is cheap and always correct
        self._adopted = bool(self._announced_start)
        log.info("resuming; last announced start %s", self._announced_start or "(none)")

    def _save_state(self) -> None:
        try:
            self._state_file().parent.mkdir(parents=True, exist_ok=True)
            self._state_file().write_text(json.dumps({
                "announced_start": self._announced_start,
                "announced_end": self._announced_end,
            }, indent=2) + "\n")
        except OSError as err:
            log.warning("could not save watch state: %s", err)

    # ── readers ──────────────────────────────────────────────────────────────

    def live_attempt(self) -> Attempt | None:
        with self._lock:
            if self._attempts and self._attempts[-1].is_live:
                return self._attempts[-1]
            return None

    def attempts(self, limit: int | None = None) -> list[Attempt]:
        with self._lock:
            items = list(reversed(self._attempts))
        return items[:limit] if limit else items

    def orphans(self) -> list[DataFile]:
        with self._lock:
            return list(self._orphans)

    def errors(self, limit: int = 50) -> list[tuple[datetime, str]]:
        with self._lock:
            return self._errors[-limit:][::-1]

    def processes(self) -> list[dict]:
        """Which GANIL components are up, refreshed on its own interval."""
        now = time.time()
        with self._lock:
            fresh = self._processes and (now - self._processes_at) < PROCESS_INTERVAL
            if fresh:
                return list(self._processes)
        found = _scan_processes()
        with self._lock:
            self._processes, self._processes_at = found, now
            return list(found)

    def snapshot(self) -> dict:
        """The live view, as the UI and the health check want it."""
        now = datetime.now(timezone.utc)
        live = self.live_attempt()
        with self._lock:
            attempts = len(self._attempts)
            last_poll = self._last_poll
            error = self._error
        processes = self.processes()
        return {
            "now": now.isoformat(),
            "live": live.as_dict(now) if live else None,
            "rate_bytes_per_s": self.rate_bytes_per_s(),
            "attempts": attempts,
            "last_poll": last_poll.isoformat() if last_poll else None,
            "log_error": error,
            "processes": processes,
            "daq_up": any(p["running"] for p in processes),
            "acq_root": str(config.ACQ_ROOT),
            "container": config.CONTAINER_NAME,
        }


def _scan_processes() -> list[dict]:
    """Which of the DAQ's components are running, read from the host's /proc.

    The DAQ runs in a container with its own PID namespace, but the host's
    /proc shows its processes all the same -- so this needs no docker at all.
    That matters more than convenience here: the container's PID 1 does not
    reap orphans, so anything executed inside it leaks a permanent zombie
    against the rlimit the DAQ itself is spending.
    """
    running: dict[str, dict] = {}
    proc = Path("/proc")
    try:
        entries = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return [{"label": label, "match": match, "running": False, "pid": None}
                for label, match in config.DAQ_PROCESSES]

    for entry in entries:
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        if not cmdline.strip():
            continue                      # kernel thread, or a zombie
        for label, match in config.DAQ_PROCESSES:
            if label in running or match not in cmdline:
                continue
            try:
                pid = int(entry.name)
                started = entry.stat().st_mtime
            except (OSError, ValueError):
                continue
            running[label] = {"label": label, "match": match, "running": True,
                              "pid": pid, "since": datetime.fromtimestamp(
                                  started, tz=timezone.utc).isoformat()}

    return [running.get(label, {"label": label, "match": match, "running": False,
                                "pid": None, "since": None})
            for label, match in config.DAQ_PROCESSES]


#: One watcher per process, as the routers and the elog module both reach for it.
monitor = Monitor()
