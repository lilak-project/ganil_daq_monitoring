"""Reading GANIL's run-control logs.

Two files, two jobs:

``rcc_gui.txt`` is the run boundary and the only place the operator's note
exists. One event looks like this, note and all::

    (17:04:26 - 31/08/2026) INFO - rcc_gui - Started run #80
    background run overnight started at the Couniting room
    (00:48:16 - 01/09/2026) INFO - rcc_gui - Stopped run #80

The note is every line under the header until the next timestamped line. It can
run to several lines and it is frequently the only record of what a run was for.

``rcc.txt`` is the run-control server: which file a run's storage wrote, and the
ECC and NARVAL errors. It is used for detail only. The boundary comes from the
GUI log because that is what the operator saw happen.

**A run number is not unique.** A run started with storage off writes no file
and does not advance the counter, so ``#3`` appears twice in an afternoon with
different notes. What is unique is the attempt: one Started..Stopped pair. The
catalog is a list of attempts, and the data file is what tells two attempts at
the same number apart.

Timestamps in both files are the DAQ container's clock, which runs on **UTC**
while the host this service runs on is on local time. They are parsed as UTC and
handed out as aware datetimes, so a browser in Seoul shows the hour the shifter
would recognise instead of one nine hours out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

#: `(HH:MM:SS - DD/MM/YYYY)` in rcc_gui.txt, `(HH:MM:SS - DD/MM/YY)` in rcc.txt.
_STAMP = re.compile(
    r"^\((?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\s*-\s*"
    r"(?P<day>\d{2})/(?P<month>\d{2})/(?P<year>\d{2}(?:\d{2})?)\)\s*"
    r"(?P<level>[A-Z]+)\s*-\s*(?P<source>[^-]+?)\s*-\s*(?P<message>.*)$"
)

_STARTED = re.compile(r"^Started run\s*#?(\d+)", re.IGNORECASE)
_STOPPED = re.compile(r"^Stopped run\s*#?(\d+)", re.IGNORECASE)

#: rcc.txt, at STOP. The file name is only ever named here.
_STORAGE_STOPPED = re.compile(
    r"stopped storage actor whose storage mode was (?P<mode>on|off)"
    r"(?:,\s*run\s*#?(?P<run>\d+))?(?:,\s*file name\s*(?P<file>\S+))?",
    re.IGNORECASE,
)
_STORAGE_STARTING = re.compile(r"starting storage of run\s*#?(\d+)", re.IGNORECASE)
#: rcc.txt, whoever pressed it -- the GUI, a script, a SOAP client. The only
#: trace a run started with storage off leaves: no run number, no file.
_SERVER_VERB = re.compile(r"^received (START|STOP)$")


def parse_stamp(line: str) -> datetime | None:
    """The timestamp of a log line, as an aware UTC datetime, or None."""
    match = _STAMP.match(line)
    return _stamp_of(match) if match else None


def _stamp_of(match: re.Match) -> datetime | None:
    year = int(match.group("year"))
    if year < 100:                        # rcc.txt writes a two-digit year
        year += 2000
    try:
        return datetime(
            year, int(match.group("month")), int(match.group("day")),
            int(match.group("h")), int(match.group("m")), int(match.group("s")),
            tzinfo=timezone.utc,
        )
    except ValueError:
        # A corrupt line -- a half-written record at the tail, most likely.
        return None


@dataclass
class GuiEvent:
    """One `Started` / `Stopped` / error record from rcc_gui.txt."""

    kind: str                             # "started" | "stopped" | "error"
    time: datetime
    run: int | None = None
    note: str = ""
    message: str = ""
    #: Byte offset of the line after this event's last line. A start whose note
    #: is still being written has not reached the next header yet, and the
    #: watcher uses this to know how much it may safely consume.
    end_offset: int = 0
    #: True when a following timestamped line proved the note is complete.
    closed: bool = False


def parse_gui(text: str, base_offset: int = 0) -> list[GuiEvent]:
    """Every event in a chunk of rcc_gui.txt, notes attached.

    `base_offset` is where `text` began in the file, so `end_offset` is a real
    file position and the caller can resume from it.
    """
    events: list[GuiEvent] = []
    offset = base_offset
    current: GuiEvent | None = None
    note_lines: list[str] = []

    def close(next_offset: int, closed: bool) -> None:
        nonlocal current, note_lines
        if current is None:
            return
        current.note = "\n".join(note_lines).strip()
        current.end_offset = next_offset
        current.closed = closed
        events.append(current)
        current, note_lines = None, []

    for line in text.splitlines(keepends=True):
        line_start = offset
        offset += len(line.encode("utf-8"))
        stripped = line.rstrip("\r\n")

        match = _STAMP.match(stripped)
        if not match:
            # A note line, or the blank line that follows an event.
            if current is not None:
                note_lines.append(stripped)
            continue

        # A new header proves the previous event's note is finished.
        close(line_start, closed=True)

        stamp = _stamp_of(match)
        if stamp is None:
            continue
        message = match.group("message").strip()

        started = _STARTED.match(message)
        stopped = _STOPPED.match(message)
        if started:
            current = GuiEvent(kind="started", time=stamp, run=int(started.group(1)),
                               message=message)
        elif stopped:
            events.append(GuiEvent(kind="stopped", time=stamp, run=int(stopped.group(1)),
                                   message=message, end_offset=offset, closed=True))
        elif match.group("level") == "ERROR":
            events.append(GuiEvent(kind="error", time=stamp, message=message,
                                   end_offset=offset, closed=True))

    # The last event has no header after it: its note may still be being
    # written, so it is reported open and the caller decides whether to wait.
    close(offset, closed=False)
    return events


@dataclass
class StorageEvent:
    """One storage transition from rcc.txt."""

    time: datetime
    mode: str                             # "on" | "off" | "starting" | "start" | "stop"
    run: int | None = None
    file: str | None = None


@dataclass
class RccFacts:
    """What rcc.txt adds: how storage behaved, and what went wrong.

    Not where the data files are. A file names its own run and start time --
    ``run_0081.dat.01-09-26_05h42m54s`` -- so the catalog reads the run
    directory and matches on that, which stays right even for runs taken before
    this service existed or after a log rotation dropped their lines.

    What rcc.txt does know and the filenames cannot is that an attempt stored
    *nothing*: ``stopped storage actor whose storage mode was off`` carries no
    run number, so it is kept as a timestamped event and matched by time.
    """

    storage: list[StorageEvent] = field(default_factory=list)
    #: Recent errors, newest last: (time, message).
    errors: list[tuple[datetime, str]] = field(default_factory=list)


def parse_rcc(text: str, keep_errors: int = 200) -> RccFacts:
    """Storage transitions and errors from a chunk of rcc.txt."""
    facts = RccFacts()
    for line in text.splitlines():
        match = _STAMP.match(line)
        if not match:
            continue
        stamp = _stamp_of(match)
        if stamp is None:
            continue
        message = match.group("message").strip()

        stopped = _STORAGE_STOPPED.search(message)
        if stopped:
            run = stopped.group("run")
            facts.storage.append(StorageEvent(
                time=stamp,
                mode=stopped.group("mode").lower(),
                run=int(run) if run else None,
                file=stopped.group("file"),
            ))
            continue

        starting = _STORAGE_STARTING.search(message)
        if starting:
            facts.storage.append(StorageEvent(
                time=stamp, mode="starting", run=int(starting.group(1))))
            continue

        verb = _SERVER_VERB.match(message)
        if verb:
            facts.storage.append(StorageEvent(time=stamp, mode=verb.group(1).lower()))
            continue

        if match.group("level") == "ERROR":
            facts.errors.append((stamp, message))

    if len(facts.errors) > keep_errors:
        del facts.errors[:-keep_errors]
    return facts


def iter_tail(path, offset: int, limit: int = 4_000_000) -> Iterator[tuple[str, int]]:
    """Yield ``(text, new_offset)`` for whatever was appended since `offset`.

    Handles the two ways these files move under a reader: they are rotated
    (rcclog.xml.1 beside rcclog.xml), and a rotation makes the live file shorter
    than where we were reading. A file that shrank is re-read from the start --
    losing a little work, never a run.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < offset:
        offset = 0                        # rotated or truncated
    if size == offset:
        return
    if size - offset > limit:
        # First read of a season's worth of log: take the tail, and start on a
        # line boundary so a half-line never parses as an event.
        offset = size - limit
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read(size - offset)
    except OSError:
        return
    if offset and b"\n" in raw:
        skipped = raw.index(b"\n") + 1
        offset += skipped
        raw = raw[skipped:]
    yield raw.decode("utf-8", errors="replace"), offset + len(raw)
