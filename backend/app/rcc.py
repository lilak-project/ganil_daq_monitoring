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

#: Which state each command may be sent from.
ALLOWED = {
    "init": {OFFLINE, IDLE, READY},
    "start": {READY},
    "stop": {RUNNING, PAUSED},
    "storage-on": {READY},
    "storage-off": {READY},
}


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

    def command(self, verb: str, who: str) -> dict:
        """Send one command if the state allows it; the answer and what came of it."""
        if verb not in ALLOWED:
            raise ValueError(f"not a command: {verb}")
        with self._command_lock:
            now = self.read()
            ok, reply = False, ""
            if not now["reachable"]:
                reply = now["error"]
            elif now["state"]["transition"] or now["state"]["error"]:
                reply = f"run control is {'in a transition' if now['state']['transition'] else 'in error'}; try again when it settles"
            elif now["state"]["code"] not in ALLOWED[verb]:
                reply = f"{verb} is not possible while {now['state']['name']}"
            else:
                try:
                    reply = self._send(verb, now)
                    ok = True
                except RCCError as err:
                    reply = str(err)
            record(who, verb, ok, reply)
            return {"ok": ok, "reply": reply, "rcc": self.read()}

    @staticmethod
    def _send(verb: str, now: dict) -> str:
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


def audit(limit: int = 50) -> list[dict]:
    try:
        lines = (config.DATA_DIR / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in reversed(lines[-limit:]) if line.strip()]


watch = Watch()
