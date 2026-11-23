"""The run catalog: what was taken, when, and where it landed.

An **attempt** is one `Started run #N` .. `Stopped run #N` pair from the GUI log.
It is the unit here rather than the run number, because the number is not
unique: a run started with storage off writes nothing and does not advance the
counter, so `#3` can appear three times in an afternoon with three different
notes. Two attempts at the same number are told apart by the file they wrote.

A run's data is not one file. NARVAL rolls the storage over at a size limit, so
run 57 is ``run_0057.dat.28-08-26_08h09m37s`` plus ``….1``, ``….2`` and so on;
the run's size is their sum, and ``cobo-frame-merge`` is what puts them back
together. `merged/` holds the results of that, ``.gmer``, and those are not run
data -- counting them would double every merged run.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .rcclog import GuiEvent, StorageEvent

#: ``run_0081.dat.01-09-26_05h42m54s`` and its ``.1``, ``.2``… continuations.
_DATA_FILE = re.compile(
    r"^run_(?P<run>\d+)\.dat\."
    r"(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{2})_"
    r"(?P<h>\d{2})h(?P<m>\d{2})m(?P<s>\d{2})s"
    r"(?:\.(?P<part>\d+))?$"
)
#: Anything else in the run directory that names a run: merges and the like.
_DERIVED = re.compile(r"^run_(?P<run>\d+)[_.]")

#: How far from an attempt's start its file may be stamped and still be its
#: file. Storage opens a few seconds BEFORE the GUI logs the start (the run
#: control starts NARVAL, then says so), and a slow ECC init stretches that.
#: Five minutes is far longer than any observed gap and far shorter than the
#: gap between two attempts at the same number.
MATCH_WINDOW = timedelta(minutes=5)
#: How far apart the GUI's line and the server's can be for one press.
SERVER_ECHO = timedelta(seconds=30)


@dataclass
class DataFile:
    """One file on disk, and the run it belongs to."""

    name: str
    run: int
    started: datetime
    part: int
    size: int
    modified: datetime
    #: Where it is, relative to the run directory: `cobo0/run_0240.dat…` when
    #: each storage actor writes its own folder, just the name when not.
    path: str = ""
    #: A product made from the raw files afterwards (`run_0231_merged.dat`),
    #: not something the DAQ wrote. It names its run but carries no start time.
    derived: bool = False

    def as_dict(self) -> dict:
        data = asdict(self)
        data["started"] = self.started.isoformat()
        data["modified"] = self.modified.isoformat()
        return data


def scan_data_files(run_dir: Path) -> list[DataFile]:
    """Every data file of the run directory, its storage folders included.

    Each storage actor may write its own folder (`cobo0/`, `mutant/` on
    grgroup), so the folders one level down are walked too -- except
    `merged/`, whose `.gmer` files are products of the raw files, not runs.
    Files in the run directory itself that name a run but are not raw output
    (`run_0231_merged.dat`) are listed as derived, under their run.
    """
    files: list[DataFile] = []
    try:
        entries = [(e, e.name) for e in run_dir.iterdir()]
    except OSError:
        return files
    for folder in [e for e, _ in entries if e.is_dir() and e.name != "merged"]:
        try:
            entries += [(e, f"{folder.name}/{e.name}") for e in folder.iterdir()]
        except OSError:
            continue

    for entry, rel in entries:
        match = _DATA_FILE.match(entry.name)
        derived = None if match or "/" in rel else _DERIVED.match(entry.name)
        if not match and not derived:
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue                      # vanished between listing and stat
        if not entry.is_file():
            continue
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if derived:
            files.append(DataFile(name=entry.name, run=int(derived.group("run")), started=modified,
                                  part=0, size=stat.st_size, modified=modified, path=rel, derived=True))
            continue
        try:
            started = datetime(
                2000 + int(match.group("year")), int(match.group("month")),
                int(match.group("day")), int(match.group("h")),
                int(match.group("m")), int(match.group("s")), tzinfo=timezone.utc,
            )
        except ValueError:
            continue
        files.append(DataFile(
            name=entry.name,
            run=int(match.group("run")),
            started=started,
            part=int(match.group("part") or 0),
            size=stat.st_size,
            modified=modified,
            path=rel,
        ))
    files.sort(key=lambda f: (f.started, f.path, f.part))
    return files


@dataclass
class Attempt:
    """One Started..Stopped pair, with whatever it wrote."""

    run: int
    started: datetime
    note: str = ""
    stopped: datetime | None = None
    #: Files this attempt wrote, base first. Empty when storage was off.
    files: list[DataFile] = field(default_factory=list)
    #: True once rcc.txt confirms storage was off for it -- as opposed to a
    #: file we simply have not found, which is a different sentence to print.
    storage_off: bool = False
    #: True when the GUI log never recorded this run and it was recovered from
    #: the run-control server's log instead. Such an attempt has no note --
    #: the note only ever exists in the GUI log -- and the UI says where it
    #: came from rather than showing a run with a silently empty note.
    from_server_log: bool = False

    @property
    def is_live(self) -> bool:
        return self.stopped is None

    @property
    def duration_s(self) -> float | None:
        if self.stopped is None:
            return None
        return (self.stopped - self.started).total_seconds()

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    def as_dict(self, now: datetime | None = None) -> dict:
        elapsed = self.duration_s
        if elapsed is None and now is not None:
            elapsed = (now - self.started).total_seconds()
        return {
            "run": self.run,
            "started": self.started.isoformat(),
            "stopped": self.stopped.isoformat() if self.stopped else None,
            "note": self.note,
            "live": self.is_live,
            "elapsed_s": elapsed,
            "size": self.size,
            "storage_off": self.storage_off,
            "from_server_log": self.from_server_log,
            "files": [f.as_dict() for f in self.files],
        }


def build_attempts(events: list[GuiEvent], files: list[DataFile],
                   storage: list[StorageEvent] | None = None) -> list[Attempt]:
    """Pair the GUI's starts and stops, then hang the data files off them.

    A start with no stop is the live run. A start followed by another start --
    which happens when the run control is restarted mid-run -- closes the first
    one at the second's timestamp, because a run whose successor has begun is
    over whatever the log failed to say.
    """
    attempts: list[Attempt] = []
    open_attempt: Attempt | None = None

    for event in events:
        if event.kind == "started":
            if open_attempt is not None:
                open_attempt.stopped = event.time
            open_attempt = Attempt(run=event.run or 0, started=event.time, note=event.note)
            attempts.append(open_attempt)
        elif event.kind == "stopped" and open_attempt is not None:
            # The run number is checked loosely: a stop that names a different
            # number than the open start still ends it, because there is only
            # ever one run going.
            open_attempt.stopped = event.time
            open_attempt = None

    attempts = _recover_missing(attempts, storage or [])
    _attach_files(attempts, files)
    _attach_storage(attempts, storage or [])
    return attempts


def _recover_missing(attempts: list[Attempt], storage: list[StorageEvent]) -> list[Attempt]:
    """Add the runs the GUI log never recorded, from the server log.

    The GUI log misses a run when the GUI drops the line (run #38, #203), and
    always when the run was started some other way: a script, `ganil_rc`, a
    SOAP client. rcc.txt has every one, because the server says `received
    START` and `received STOP` whoever asked.

    One attempt per `received START`. Its number is the one storage announced
    just after (`Storage[n] : starting storage of run #N`, once per storage
    actor -- so counted once, not once per actor). A run with storage off
    announces none, so it gets the number the counter is at: one past the last
    run that stored, since a run that stores nothing does not advance it.

    The GUI log stays the authority wherever it HAS the run: it is what the
    operator saw, and it is the only place the note exists. This fills gaps and
    never overrides. A recovered attempt is marked, because an empty note on it
    means "the GUI never wrote one", not "the operator left it blank".
    """
    starts = [e for e in storage if e.mode == "start"]
    stops = [e.time for e in storage if e.mode == "stop"]
    numbered = [e for e in storage if e.mode == "starting" and e.run is not None]
    if not starts:
        return attempts

    recovered: list[Attempt] = []
    for index, start in enumerate(starts):
        following = starts[index + 1].time if index + 1 < len(starts) else None
        number = next((e.run for e in numbered
                       if start.time <= e.time <= start.time + SERVER_ECHO
                       and (following is None or e.time < following)), None)
        if any(abs(a.started - start.time) <= SERVER_ECHO
               or (number is not None and a.run == number
                   and abs(a.started - start.time) <= MATCH_WINDOW)
               for a in attempts):
            continue                      # the GUI has this one
        run = number
        if run is None:
            before = [e.run for e in numbered if e.time < start.time]
            run = max(before) + 1 if before else 0
        stopped = next((t for t in stops
                        if t > start.time and (following is None or t <= following)), following)
        recovered.append(Attempt(run=run, started=start.time, stopped=stopped,
                                 storage_off=number is None, from_server_log=True))

    if not recovered:
        return attempts
    merged = attempts + recovered
    merged.sort(key=lambda a: a.started)
    # The same rule the GUI pairing uses, now across both sources: an attempt
    # whose successor has begun is over, whichever log failed to say so. Without
    # this a GUI start with no stop would stay "live" forever behind a recovered
    # run, and two runs would be going at once.
    for attempt, following in zip(merged, merged[1:]):
        if attempt.stopped is None or attempt.stopped > following.started:
            attempt.stopped = following.started
    return merged


def _attach_files(attempts: list[Attempt], files: list[DataFile]) -> None:
    """Give every file to the attempt it belongs to.

    Matched on run number and then on time, because the run number alone is
    ambiguous. The continuation parts (``.1``, ``.2``…) carry the base file's
    timestamp in their own name, so they land on the same attempt without
    needing to be traced through it.
    """
    by_run: dict[int, list[Attempt]] = {}
    for attempt in attempts:
        by_run.setdefault(attempt.run, []).append(attempt)

    derived = [f for f in files if f.derived]
    for data in files:
        if data.derived:
            continue
        candidates = by_run.get(data.run)
        if not candidates:
            continue
        best, best_gap = None, MATCH_WINDOW
        for attempt in candidates:
            gap = abs(data.started - attempt.started)
            if gap <= best_gap:
                best, best_gap = attempt, gap
        if best is not None:
            best.files.append(data)

    # A derived file names only its run: it goes to the last attempt at that
    # number that wrote anything, which is the one it was made from.
    for data in derived:
        wrote = [a for a in by_run.get(data.run, []) if a.files]
        if wrote:
            wrote[-1].files.append(data)

    for attempt in attempts:
        attempt.files.sort(key=lambda f: (f.derived, f.path, f.part))


def _attach_storage(attempts: list[Attempt], storage: list[StorageEvent]) -> None:
    """Mark the attempts rcc.txt says stored nothing.

    The ``storage mode was off`` line carries no run number, so it is matched to
    whichever attempt was open when it was written.
    """
    offs = [event.time for event in storage if event.mode == "off"]
    if not offs:
        return
    for attempt in attempts:
        if attempt.files:
            continue
        end = attempt.stopped or (attempt.started + timedelta(days=1))
        # A little slack at the end: the storage actor is stopped a few seconds
        # after the GUI logs the stop.
        end = end + timedelta(seconds=30)
        if any(attempt.started <= off <= end for off in offs):
            attempt.storage_off = True
