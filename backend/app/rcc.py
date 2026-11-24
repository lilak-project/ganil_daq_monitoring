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
        self._ecc_choices: list[dict] = []
        self._ecc_choices_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def forget_equipment(self) -> None:
        """Another run control: its equipment list is asked afresh."""
        self._equipment = []
        self._ecc_choices_at = 0.0

    def _ecc_config(self) -> dict:
        ecc = next((n for n, k in self._equipment if k in ("eccore", "ecc")), None)
        if not ecc:
            return {}
        found = _config_ids(call("rccGetECCoreConfigID", name=ecc)["value"])
        return found[0] if found else {}

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
            if not self._ecc_choices_at or time.time() - self._ecc_choices_at > 60:
                ecc = next((n for n, k in self._equipment if k in ("eccore", "ecc")), None)
                self._ecc_choices = _config_ids(call("rccECCoreGetConfigIDs", name=ecc)["value"]) if ecc else []
                self._ecc_choices_at = time.time()
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
                # The ECC configuration the run control has selected (describe /
                # prepare / configure), and the sets the ECC offers.
                "ecc_config": self._ecc_config(),
                "ecc_choices": self._ecc_choices,
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


def _config_ids(text: str) -> list[dict]:
    """`<ConfigId><SubConfigId type="describe">x</SubConfigId>…</ConfigId>`, one
    dict per ConfigId: {describe: x, prepare: y, configure: z}."""
    import re

    return [dict(re.findall(r'<SubConfigId type="([^"]+)">([^<]*)</SubConfigId>', block))
            for block in re.findall(r"<ConfigId>(.*?)</ConfigId>", text, re.S)]


def _docker_read(path: str, limit: int = 4_000_000) -> str:
    """One file out of the DAQ container with `docker cp`: the daemon copies
    it out, nothing runs inside the container (config.py)."""
    import io
    import subprocess
    import tarfile

    try:
        out = subprocess.run(["docker", "cp", f"{config.CONTAINER_NAME}:{path}", "-"],
                             capture_output=True, timeout=20, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(out)) as archive:
            member = archive.next()
            if member is None or not member.isfile():
                raise RCCError(f"{path} is not a file")
            if member.size > limit:
                raise RCCError(f"{path} is {member.size} bytes, too large to show")
            return archive.extractfile(member).read().decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as err:
        detail = getattr(err, "stderr", b"") or b""
        raise RCCError(f"could not read {path} from {config.CONTAINER_NAME}: "
                       f"{detail.decode(errors='replace').strip() or err}") from err


def _docker_get(path: str, limit: int = 4_000_000) -> tuple[bytes, "object"]:
    """One file out of the DAQ container (`docker cp`), with its tar header --
    owner, group and mode, so a save can put them back as they were."""
    import io
    import subprocess
    import tarfile

    try:
        out = subprocess.run(["docker", "cp", f"{config.CONTAINER_NAME}:{path}", "-"],
                             capture_output=True, timeout=20, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(out)) as archive:
            member = archive.next()
            if member is None or not member.isfile():
                raise RCCError(f"{path} is not a file")
            if member.size > limit:
                raise RCCError(f"{path} is {member.size} bytes, too large")
            return archive.extractfile(member).read(), member
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as err:
        detail = getattr(err, "stderr", b"") or b""
        raise RCCError(f"could not read {path} from {config.CONTAINER_NAME}: "
                       f"{detail.decode(errors='replace').strip() or err}") from err


def _docker_put(path: str, data: bytes, like) -> None:
    """Write a file into the DAQ container with `docker cp -a`, owner, group and
    mode copied from `like` (the tar header it was read with). The daemon
    writes it; nothing runs inside the container."""
    import io
    import posixpath
    import subprocess
    import tarfile

    info = tarfile.TarInfo(posixpath.basename(path))
    info.size, info.mode, info.mtime = len(data), like.mode, time.time()
    info.uid, info.gid, info.uname, info.gname = like.uid, like.gid, like.uname, like.gname
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.addfile(info, io.BytesIO(data))
    try:
        subprocess.run(["docker", "cp", "-a", "-", f"{config.CONTAINER_NAME}:{posixpath.dirname(path)}"],
                       input=buffer.getvalue(), capture_output=True, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError) as err:
        detail = getattr(err, "stderr", b"") or b""
        raise RCCError(f"could not write {path} into {config.CONTAINER_NAME}: "
                       f"{detail.decode(errors='replace').strip() or err}") from err


def ecc_file(step: str, name: str) -> dict:
    """An ECC configuration file (`configure-AlphaTest-Nov12-2026.xcfg`).

    They sit in the experiment's `configurefiles/`, in a folder per step
    (`describe/`, `configure/`) or loose beside it. Prepare has no files of
    its own on these setups: it reads the configure file of the same name.
    """
    import posixpath
    import re

    if step not in ("describe", "prepare", "configure") or not re.fullmatch(r"[\w.+-]+", name):
        raise ValueError("not an ECC configuration")
    rcc_file = watch.last().get("config_file") or call("rccGetExperimentCfgFile")["value"]
    cut = rcc_file.find("/configurefiles/")
    base = rcc_file[:cut + len("/configurefiles")] if cut >= 0 else posixpath.dirname(rcc_file)
    tried = []
    for kind in ([step, "configure"] if step == "prepare" else [step]):
        for path in (f"{base}/{kind}/{kind}-{name}.xcfg", f"{base}/{kind}-{name}.xcfg"):
            tried.append(path)
            try:
                data, _ = _docker_get(path)
            except RCCError:
                continue
            import hashlib
            return {"step": step, "name": name, "path": path, "text": data.decode("utf-8", errors="replace"),
                    "sha": hashlib.sha256(data).hexdigest()}
    raise RCCError("not found: " + ", ".join(tried))


_trees: dict[tuple[str, str], tuple[float, object]] = {}


def ecc_tree(step: str, name: str, keys: list[tuple[str, str]]) -> dict:
    """What one object of an ECC configuration holds once its defaults are
    applied -- what cconfiged shows (xcfg.py). Files are read again after a
    minute, so an edit shows without a restart."""
    from . import xcfg

    cached = _trees.get((step, name))
    if cached is None or time.time() - cached[0] > 60:
        found = ecc_file(step, name)
        cached = (time.time(), (found["path"], xcfg.parse(found["text"].encode()), found["sha"]))
        _trees[(step, name)] = cached
    path, root, sha = cached[1]
    return {"path": path, "sha": sha, "root": root.name + (f"[{root.index}]" if root.index else ""),
            "rows": xcfg.children(root, keys)}


def ecc_find(step: str, name: str, query: str, limit: int = 200) -> dict:
    """Every object or value in the file, as written, whose name, index or value
    holds `query` (cconfiged's Find): paths to open the tree at."""
    from . import xcfg

    ecc_tree(step, name, [])                     # load (and cache) the file
    _, (path, root, sha) = _trees[(step, name)]
    q = query.strip().lower()
    hits: list[dict] = []

    def walk(node, keys):
        for child in node.children:
            k = keys + [[child.name, child.index]]
            label = f"{child.name}[{child.index}]" if child.index else child.name
            if q and (q in label.lower() or (child.value is not None and q in child.value.lower())):
                hits.append({"keys": k, "label": ".".join(f"{n}[{i}]" if i else n for n, i in k),
                             "value": child.value})
                if len(hits) >= limit:
                    return True
            if walk(child, k):
                return True
        return False

    walk(root, [])
    return {"hits": hits, "truncated": len(hits) >= limit}


def ecc_save(step: str, name: str, sha: str, edits: list[dict], who: str) -> dict:
    """Write edited values into an ECC configuration, in place.

    Only the text of each edited value changes; the rest of the file stays as
    it was, byte for byte. Refused when the file changed since it was read
    (`sha`), when a value is not this object's own (inherited values are
    overridden first), or when a value is outside its range. The previous file
    is kept in DATA_DIR/xcfg-backups first; every save is audited.
    """
    import hashlib
    from xml.sax.saxutils import escape as xml_escape
    from . import xcfg

    found = ecc_file(step, name)
    path = found["path"]
    data, like = _docker_get(path)
    if hashlib.sha256(data).hexdigest() != sha:
        raise RCCError("the file changed since it was opened (someone saved it); reopen it and edit again")
    root = xcfg.parse(data)
    splices = []
    for edit in edits:
        keys = [tuple(k) for k in edit["keys"]]
        value = str(edit["value"]).strip()
        hit = xcfg._config(root, keys)
        label = ".".join(f"{n}[{i}]" if i else n for n, i in keys)
        if hit is None:
            raise ValueError(f"{label}: not in the file")
        node, level = hit
        if level != 0 or node.is_object or node.span is None:
            raise ValueError(f"{label}: not a value of its own (override it first)")
        if not xcfg.in_range(value, xcfg.parse_range(node.attrs.get("range"))):
            raise ValueError(f"{label}: {value!r} is outside {node.attrs.get('range')}")
        splices.append((node.span, xml_escape(value).encode(), label, node.value, value))
    if not splices:
        raise ValueError("nothing to save")
    new = data
    for (start, end), text, *_ in sorted(splices, key=lambda x: x[0][0], reverse=True):
        new = new[:start] + text + new[end:]
    xcfg.parse(new)                              # still a file CompoundConfig can read
    backups = config.DATA_DIR / "xcfg-backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backups / f"{path.rsplit('/', 1)[-1]}.{stamp}"
    backup.write_bytes(data)
    _docker_put(path, new, like)
    check, _ = _docker_get(path)
    if check != new:
        raise RCCError(f"{path}: what was written does not read back the same; the previous file is {backup}")
    _trees.pop((step, name), None)
    changes = ", ".join(f"{label}: {old} -> {value}" for _, _, label, old, value in splices)
    record(who, "xcfg-save", True, f"{path}: {changes} (backup {backup.name})")
    return {"path": path, "sha": hashlib.sha256(new).hexdigest(), "backup": str(backup), "changed": len(splices)}


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


def config_text() -> dict:
    """The configuration file the run control has loaded, read out of the container."""
    path = watch.last().get("config_file") or call("rccGetExperimentCfgFile")["value"]
    if not path:
        raise RCCError("the run control has no configuration loaded")
    text = _docker_read(path)
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
