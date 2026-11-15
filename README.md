# GANIL DAQ Monitoring

Run status for the GANIL acquisition. Which run is going, the note it was
started with, what it is writing and how fast, and every run already taken.

```bash
cd frontend && npm install && npm run build && cd ..
./start_server.sh              # http://<this-host>:8008
```

It reads files and nothing else. There is no control surface here and there
cannot be one: the only way to start or stop a GANIL run is the run-control GUI
inside the `stark_daq` container, over X11. Read-only is not a policy this
service enforces — it is the shape of the thing.

The page is drawn with the NEON kit (`../neon_ui`), the DAQ screens' look.

## Where it reads

Everything is on the host, under `/data1/ko2520/acquisition`. The DAQ runs in a
container, but its run directory is a bind mount of `/data1` and its run-control
logs are written there too.

| | |
|---|---|
| `log/rcc_gui.txt` | `Started run #N`, the operator's note, `Stopped run #N` |
| `log/rcc.txt` | which file each run's storage wrote; ECC and NARVAL errors |
| `run/run_NNNN.dat.*` | the data itself — run number, start time and size |
| the host's `/proc` | whether each of the DAQ's eleven components is up |

**Nothing is executed inside the container**, and that is deliberate rather than
tidy. `stark_daq`'s PID 1 does not reap orphans: every process that outlives its
parent in there becomes a permanent zombie against a 4096-process rlimit the DAQ
itself is spending. A monitor that shelled in once a second would be a slow leak
aimed at the thing it is watching. Component status comes from the host's
`/proc`, which shows the container's processes anyway.

## What an "attempt" is

The run number is **not unique**. A run started with storage off writes no file
and does not advance the counter, so `#3` appears three times in one afternoon
with three different notes. The unit here is therefore the *attempt* — one
`Started`..`Stopped` pair — and two attempts at the same number are told apart
by the file they wrote.

A run's data is also not one file: NARVAL rolls storage over at a size limit, so
run 57 is `run_0057.dat.28-08-26_08h09m37s` plus `.1`, `.2`… and the run's size
is their sum. `merged/*.gmer` are `cobo-frame-merge` products of those and are
not counted.

Files are matched to attempts on run number and then on time, within five
minutes: storage opens a few seconds *before* the GUI logs the start. A file
that matches no attempt is not hidden — it is listed at the bottom of the page.
Usually it is an aborted storage of a few hundred bytes; a run whose log lines
were rotated away looks the same and is not, which is why it is shown rather
than dropped.

## Times

The logs and the data filenames carry the DAQ container's clock, which runs on
**UTC**, while this host is on local time. They are parsed as UTC and handed out
as aware timestamps, so the browser shows the hour the shifter was standing
there rather than one nine hours out.

## In the LILAK elog — this service is a SYSTEM

A *service* answers when elog asks. A **system** also speaks first: it tells
elog when a run starts and ends, so the elog's run number and the DAQ's are the
same number by construction rather than by someone retyping it.

Register it once, in the elog's Experiment tab → New Service, with

```
portal://ganil_daq_monitoring/api/elog
```

`portal://` rather than a host and port: a portal-managed service's port comes
from a pool at start time and can differ after a restart, so a literal
`http://127.0.0.1:<port>` goes stale. Running this standalone instead, register
its real address.

Discover reads back `is_system: true`; on registration elog creates the
`Init / Start / End / Monitoring of GANIL run` formats, issues an API token, and
POSTs it here as an `elog_credentials` event, stored in
`backend/data/elog_credentials.json`, mode 0600.

| | |
|---|---|
| run starts | `log_type: 12` — `run_number` and `note`, plus a readable body |
| run ends | `log_type: 13` — `run_number`, with the length, the files and their sizes |

### A system answers as well as pushes

"A system pushes" reads as if that is all it does. It is not. elog reaches a
system on the same `request_url` a plain service uses, and `fire_webhook_fills`
resolves a format's owner through `fmt.system_id or fmt.subsystem_id` — so a
task log on one of this system's own run formats is filled by **asking us**. A
request envelope carries `mode` and no `event` key, which is how the two are
told apart on this end.

```
POST /api/elog  {"mode": "realtime", "run_number": 81}
  → { "title": "Run 81 running · 7.0 h",
      "body":  "Run 81 — running\nstarted: …\nelapsed: 25,157 s\n
                written: 328.8 MB in 1 file(s)\nrate: 6.2 kB/s\n
                \nBackground run.. we wonder what is that peak around 7 MeV…",
      "fields": { run_number, note, state, elapsed_s, size_bytes } }
```

`mode` is `task`, `snapshot` or `realtime`. **A task fill is answered about the
run it names**, not about the run going now: a task log opened for run 78 must
not quietly report run 81 because 81 is what happens to be running. Snapshot and
realtime mean *now*, and get the live run.

If a component is down while a run is going, the body says which — that is the
thing a shifter most needs in a logbook entry, and it should not be left to
whoever thinks to open the web page.

Only `title` and `body` are builtins that always render. To see the rest as
fields, add `run_number` / `elapsed_s` / `size_bytes` (number entry) and `state`
/ `note` (text) to the run format in elog's format editor.

**Which format**, and this is easy to get wrong: registered as the **main
system**, this service does not use the `… GANIL DAQ run` formats that
`sync_system_formats` created — `link_main_system_formats` attaches the four
GLOBAL ones instead (`Init / Start / End / Monitoring run log`), and the named
ones are left unlinked with `system_id` NULL. The format to edit is therefore
**`Monitoring run log`**, which every main system shares; a key added there is
added for all of them. There is only one main system, so today that is this one.

Editing it needs elog `manager` (a portal admin — the two vocabularies are the
same word, and elog re-syncs the role from the portal on every entry). Until
2026-09-02 it could not be edited at all: `update_format` refused any format
owned by a system, which locked the exact format that a system's pushed keys
need declaring on. That guard is gone; the format is now labelled rather than
locked.
Everything essential is in the title and the body regardless, which is why those
carry the run number in words.

Numbers go up as **plain numbers**, not as `{"single": x}`.
`normalize_format_fields` only canonicalizes the keys the format declares, so a
wrapper sent for an undeclared key reaches the logbook as the literal string
`[object Object]`. A plain number is right either way:
`normalize_number_entry` documents "a plain number (treated as single)" and
produces exactly the shape the wrapper would have, so declaring the field later
changes nothing — and until then the logbook shows `81`.

elog gives up after **5 s** and `realtime` repeats as often as twice a second,
so the reply is assembled only from state the watcher already holds — no file is
read on the request path. Measured: 4.4 ms average, 33 ms worst of ten.

**These keys must exist on the "Start of GANIL run" format**, added once in
elog's format editor: `run_number` (number entry) and `note` (text).
`sync_system_formats` never rewrites an existing format's fields, so the
additions survive re-registration.

### It is a system that cannot be commanded back

Worth stating plainly, because the other systems in this portal are not like
this. `mte_control` is the system for MTE runs because it *spawns* the runner —
it knows a run has begun because it began it. Here the run control is a Java GUI
inside a container with no API, so this service is a **watcher** that announces
boundaries it read out of a log. elog gets the run number and the note; it gets
no way to start, stop or configure a GANIL run, and there is no way to add one
short of putting an API on the run control itself.

### Two rules the watcher exists to keep

**It never pushes finished history.** The first time it runs it adopts whatever
the log already says and pushes none of it. A logbook that filled with
eighty-three runs from last week the moment somebody started a monitoring
service would be worse than no logbook. What has been announced is remembered in
`data/watch_state.json`, so a restart mid-experiment does not repeat itself.

A run that is **still going** when the service starts is the exception, and is
announced. elog derives "the current run" from the highest run number on a log
entry (`routes/services.py::_current_run_number`), so a main system that
swallows the run in progress leaves the logbook with no run number at all until
the next run begins — which is a main system failing at its one job. The start
log says plainly that the watcher joined mid-run, so the entry's own timestamp
being hours after the run's start is not a puzzle.

**It never pushes half a note.** `Started run #81` and the note under it are one
event written in one go, but a poll can land between the two writes. A start is
held until the next timestamped line proves the note is complete, or until
`GANIL_START_SETTLE` seconds have passed — whichever comes first. The note is
the only part of a GANIL run nobody can reconstruct afterwards, so it is worth
four seconds.

Stopping the service stops the watch. A boundary that happens while it is down
is not pushed — but it is not lost either: it is in `rcc_gui.txt`, and the next
start adopts the log as it stands.

## In the portal

`portal-data/ganil_daq_monitoring/service.json` registers it; the portal
discovers the directory and assigns a port on first start. Standalone it
defaults to 8008.

## Configuration

Every setting is an environment variable, and nothing is required.

| | |
|---|---|
| `GANIL_ACQ_ROOT` | the experiment's acquisition dir (default `/data1/ko2520/acquisition`) |
| `GANIL_LOG_DIR`, `GANIL_RUN_DIR` | override either half of it |
| `GANIL_POLL_INTERVAL` | seconds between reads (default 2) |
| `GANIL_START_SETTLE` | seconds a start waits for its note (default 4) |
| `GANIL_DATA_DIR` | where the elog credentials and the watch state live |
| `GANIL_CONTAINER` | the container name shown on the page (default `stark_daq`) |

Pointing it at another experiment is a matter of `GANIL_ACQ_ROOT`: the
acquisition mount's `config_acq.txt` lists them.

## API

| | |
|---|---|
| `GET /api/status` | the live view: current attempt, rate, components |
| `GET /api/runs?limit=` | every attempt, newest first, plus the orphan files |
| `GET /api/errors?limit=` | recent run-control errors |
| `GET /api/health` | for the portal |
| `WS /ws/status` | the live view, pushed every two seconds |
| `POST /api/elog` | the elog handshake, credentials, and data requests (§ above) |

The socket is one-way. Nothing is read from the browser, because there is
nothing it could ask this service to do.
