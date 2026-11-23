"""The GANIL run control (RCC), spoken to directly over SOAP.

The RCC server listens on the host (the DAQ container shares the host's
network), so this service asks it over HTTP -- the same SOAP the run-control
GUI and the `ganildaq-*` scripts in the container send. Nothing is executed
inside the container: a `docker exec` there leaves zombies the container's
PID 1 never reaps (see config.py), a SOAP request leaves nothing.

Operation names and arguments are from the RCC's own `rcc.wsdl`
(/opt/ganildaq/20160112/RCC/Linux-x86_64 in the container). Two things that
are easy to get wrong:

* the experiment's state is `rccStateMachine`; `ExperimentStateMachine` is the
  name of the command-line client, not of the SOAP operation;
* an argument must carry the `urn:rcc` namespace (`<rcc:name>`), or the server
  reads it as an empty string and answers "equipment "" does not exist".

Commands are gated on the state the way the container's scripts gate them:
Start from READY, Stop from RUNNING or PAUSED, Init from OFFLINE / IDLE /
READY, storage on/off from READY. Every one is written to `audit.jsonl`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.client import HTTPConnection, HTTPException
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from . import config

log = logging.getLogger("ganil.rcc")

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS = "urn:rcc"

#: The run-control GUI's SMState enum.
STATES = ("OFFLINE", "IDLE", "READY", "RUNNING", "PAUSED", "ERROR", "NOMONITORING", "WARNING")
OFFLINE, IDLE, READY, RUNNING, PAUSED = range(5)

ERROR, NOMONITORING, WARNING = 5, 6, 7

#: Which state each command may be sent from: the experiment's state, or for
#: the ECC-only commands the ECC's own (the experiment must not be taking data).
ALLOWED = {
    "init": {OFFLINE, IDLE, READY},
    "start": {READY},
    "stop": {RUNNING, PAUSED},
    "storage-on": {READY},
    "storage-off": {READY},
    # Breakup (Exit) is the way out of an error, so it is allowed in one.
    "breakup": {IDLE, READY, ERROR, NOMONITORING, WARNING},
    "ecc-init": {OFFLINE, IDLE},
    "ecc-breakup": {IDLE, READY, ERROR, WARNING},
}
#: The commands that act on the ECC alone (`ganildaq-init` / `-breakup --equipment ECC`).
ECC_ONLY = {"ecc-init", "ecc-breakup"}
#: The commands that may be sent while the experiment carries an error.
IN_ERROR_OK = {"breakup", "ecc-breakup"}


class RCCError(RuntimeError):
    pass


def call(op: str, timeout: float | None = None, **args: object) -> dict[str, str]:
    """One SOAP operation. Returns the reply's fields; raises on a refusal."""
    body = "".join(f"<rcc:{k}>{escape(str(v))}</rcc:{k}>" for k, v in args.items())
    envelope = (f'<soap:Envelope xmlns:soap="{SOAP}" xmlns:rcc="{NS}"><soap:Body>'
                f"<rcc:{op}>{body}</rcc:{op}></soap:Body></soap:Envelope>")
    try:
        connection = HTTPConnection(config.RCC_HOST, config.RCC_PORT, timeout=timeout or config.RCC_TIMEOUT)
        try:
            connection.request("POST", "/", envelope.encode(),
                               {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'})
            raw = connection.getresponse().read()
        finally:
            connection.close()
        root = ElementTree.fromstring(raw)
    except (OSError, HTTPException, ElementTree.ParseError) as err:
        raise RCCError(f"run control at {config.RCC_HOST}:{config.RCC_PORT} did not answer: {err}") from err
    reply = root.find(f"{{{SOAP}}}Body/*")
    if reply is None:
        raise RCCError(f"{op}: empty reply")
    if reply.tag == f"{{{SOAP}}}Fault":
        raise RCCError(f"{op}: {(reply.findtext('faultstring') or 'SOAP fault').strip()}")
    fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "") for child in reply}
    if fields.get("Error", "0") != "0":
        raise RCCError(f"{op}: {fields.get('ErrorMessage') or 'refused'}")
    return fields


def _state(fields: dict[str, str]) -> dict:
    code = int(fields.get("SMState", -1))
    return {"code": code, "name": STATES[code] if 0 <= code < len(STATES) else f"UNKNOWN({code})",
            "transition": int(fields.get("SMTransition", 0)), "error": int(fields.get("SMError", 0))}


class Watch:
    """Asks the RCC every few seconds and keeps the last answer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._last: dict = {"reachable": False, "error": "not asked yet", "at": None}
        #: Name and type of every equipment. Only changes when a configuration
        #: is loaded, so it is re-read once a minute rather than every poll.
        self._equipment: list[tuple[str, str]] = []
        self._equipment_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def forget_equipment(self) -> None:
        """Another run control: its equipment list is asked afresh."""
        self._equipment = []

    def last(self) -> dict:
        with self._lock:
            return self._last

    def read(self) -> dict:
        """Everything the page shows, asked now."""
        try:
            if not self._equipment or time.time() - self._equipment_at > 60:
                count = int(call("rccGetEquipmentCount")["value"])
                self._equipment = [(e["name"], e.get("typestr", "")) for e in
                                   (call("rccGetEquipmentByIndex", index=i) for i in range(count))]
                self._equipment_at = time.time()
            equipment = []
            storages = []
            for name, kind in self._equipment:
                item = {"name": name, "type": kind, "state": _state(call("rccEquipmentStateMachine", name=name))}
                if kind == "storage":
                    # The GUI writes the file name X for a storage that is off.
                    item["storage_on"] = call("rccReadStorageFileName", name=name)["value"] != "X"
                    storages.append(item)
                equipment.append(item)
            run = None
            if storages:
                run = int(call("rccGetStorageRunNumber", name=storages[0]["name"])["value"])
            ons = {s["storage_on"] for s in storages}
            snap = {
                "reachable": True, "error": None, "at": time.time(),
                "experiment": call("rccGetExperimentName")["value"],
                # The configuration loaded, as a path inside the DAQ container.
                "config_file": call("rccGetExperimentCfgFile")["value"],
                "state": _state(call("rccStateMachine")),
                "monitoring": call("rccGetMonitoringState")["value"] == "true",
                "run_number": run,
                "storage": None if not storages else ("on" if ons == {True} else "off" if ons == {False} else "mixed"),
                "equipment": equipment,
            }
        except (RCCError, KeyError, ValueError) as err:
            snap = {"reachable": False, "error": str(err), "at": time.time()}
        with self._lock:
            self._last = snap
        return snap

    def command(self, verb: str, who: str, note: str = "") -> dict:
        """Send one command if the state allows it; the answer and what came of it.

        A Start may carry a note. The run-control GUI writes its note to
        rcc_gui.txt; a run started here never reaches that file, so the note is
        kept in notes.jsonl and given to the run that starts at that moment.
        """
        if verb not in ALLOWED:
            raise ValueError(f"not a command: {verb}")
        note = note.strip()[:2000] if verb == "start" else ""
        with self._command_lock:
            now = self.read()
            ok, reply = False, ""
            if not now["reachable"]:
                reply = now["error"]
            elif now["state"]["transition"] or (now["state"]["error"] and verb not in IN_ERROR_OK):
                reply = f"run control is {'in a transition' if now['state']['transition'] else 'in error'}; try again when it settles"
            elif verb in ECC_ONLY and now["state"]["code"] in (RUNNING, PAUSED):
                reply = f"{verb} is not possible while the experiment is {now['state']['name']}"
            elif (gate := _gate_state(verb, now)) not in ALLOWED[verb]:
                reply = f"{verb} is not possible while {'the ECC is' if verb in ECC_ONLY else ''} {STATES[gate] if 0 <= gate < len(STATES) else gate}".replace("  ", " ")
            else:
                try:
                    if note:
                        _write_note(note, who)
                    reply = self._send(verb, now)
                    ok = True
                except RCCError as err:
                    reply = str(err)
            record(who, verb, ok, reply + (f" · note: {note}" if note else ""))
            return {"ok": ok, "reply": reply, "rcc": self.read()}

    @staticmethod
    def _send(verb: str, now: dict) -> str:
        if verb in ECC_ONLY:
            op = "rccEquipmentInit" if verb == "ecc-init" else "rccEquipmentExit"
            return call(op, timeout=config.RCC_COMMAND_TIMEOUT, name=_ecc(now)).get("value", "") or f"{verb} sent"
        if verb == "breakup":
            return call("rccExit", timeout=config.RCC_COMMAND_TIMEOUT).get("value", "") or "breakup sent"
        if verb in ("init", "start", "stop"):
            # A command answers when it is done: Stop took 9 s on grgroup, Init longer.
            return call({"init": "rccInit", "start": "rccStart", "stop": "rccStop"}[verb],
                        timeout=config.RCC_COMMAND_TIMEOUT).get("value", "") or f"{verb} sent"
        want = verb == "storage-on"
        names = [e["name"] for e in now["equipment"] if e["type"] == "storage" and e["storage_on"] != want]
        for name in names:
            call("rccStorageOn" if want else "rccStorageOff", timeout=config.RCC_COMMAND_TIMEOUT, name=name)
        return f"storage {'on' if want else 'off'}: {', '.join(names) or 'already'}"

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="rcc-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._command_lock.locked():
                self.read()
            self._stop.wait(config.RCC_POLL)


def record(who: str, verb: str, ok: bool, reply: str) -> None:
    line = {"at": time.time(), "who": who, "command": verb, "ok": ok, "reply": reply}
    log.info("command %s by %s: %s", verb, who, reply)
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (config.DATA_DIR / "audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("could not write the audit line")


def _ecc(now: dict) -> str:
    """The ECC equipment's name (`ECC` on the GANIL setups)."""
    return next((e["name"] for e in now["equipment"] if e["type"] in ("eccore", "ecc")), "ECC")


def _gate_state(verb: str, now: dict) -> int:
    """The state a command is judged on: the ECC's for the ECC-only ones."""
    if verb in ECC_ONLY:
        ecc = _ecc(now)
        return next((e["state"]["code"] for e in now["equipment"] if e["name"] == ecc), -1)
    return now["state"]["code"]


def _write_note(note: str, who: str) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (config.DATA_DIR / "notes.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.time(), "who": who, "note": note}, ensure_ascii=False) + "\n")


def notes() -> list[dict]:
    """Every note given with a Start from this page, oldest first."""
    try:
        lines = (config.DATA_DIR / "notes.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def config_text(limit: int = 4_000_000) -> dict:
    """The configuration file the run control has loaded, read out of the
    container with `docker cp` -- the daemon copies it out; nothing runs
    inside the container, so no zombie is left (config.py)."""
    import io
    import subprocess
    import tarfile

    path = watch.last().get("config_file") or call("rccGetExperimentCfgFile")["value"]
    if not path:
        raise RCCError("the run control has no configuration loaded")
    try:
        out = subprocess.run(["docker", "cp", f"{config.CONTAINER_NAME}:{path}", "-"],
                             capture_output=True, timeout=20, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(out)) as archive:
            member = archive.next()
            if member is None or not member.isfile():
                raise RCCError(f"{path} is not a file")
            if member.size > limit:
                raise RCCError(f"{path} is {member.size} bytes, too large to show")
            text = archive.extractfile(member).read().decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as err:
        detail = getattr(err, "stderr", b"") or b""
        raise RCCError(f"could not read {path} from {config.CONTAINER_NAME}: "
                       f"{detail.decode(errors='replace').strip() or err}") from err
    return {"path": path, "container": config.CONTAINER_NAME, "text": text, **parse_config(text)}


def parse_config(text: str) -> dict:
    """An RCC configuration (`*.rcc.xml`) laid out for the page.

    `equipment`: every element that is not a link, NARVAL's actors and
    storages under it, each with its attributes and parameters. The ECC's
    `configid` packs the three ECC configurations (describe / prepare /
    configure) into one attribute; they are split out, because which xcfg is
    loaded is what a shifter asks first. `links`: who feeds whom.
    """
    import re

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as err:
        return {"parse_error": str(err), "equipment": [], "links": []}

    def tag(el) -> str:
        return el.tag.rsplit("}", 1)[-1]

    def item(el, parent: str = "") -> dict:
        attrs = dict(el.attrib)
        out = {"kind": tag(el), "name": attrs.pop("name", tag(el)), "parent": parent, "attrs": attrs,
               "parameters": [dict(p.attrib) for p in el if tag(p) == "parameter"]}
        if "configid" in attrs:
            out["ecc_configs"] = dict(re.findall(r'\[SubConfigId type="([^"]+)"\]([^\[]*)\[/SubConfigId\]', attrs["configid"]))
        return out

    equipment, links = [], []
    for el in root:
        kind = tag(el)
        if kind == "format":
            continue
        if kind == "link":
            links.append(dict(el.attrib))
            continue
        equipment.append(item(el))
        equipment += [item(child, equipment[-1]["name"]) for child in el if tag(child) != "parameter"]
    return {"equipment": equipment, "links": links}


def audit(limit: int = 50) -> list[dict]:
    try:
        lines = (config.DATA_DIR / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in reversed(lines[-limit:]) if line.strip()]


watch = Watch()
