#!/usr/bin/env python3
"""A stand-in for the GANIL run control's SOAP server, for testing without a DAQ.

    python3 tools/fake_rcc.py [port]          # default 6911

Answers the operations rcc.py asks, with a state machine that moves the way the
real one does: Init OFFLINE/IDLE -> READY, Start READY -> RUNNING, Stop
RUNNING -> READY, storage on/off per Storage actor. Commands sent from the
wrong state are refused with Error=1, as the real server does.
"""

from __future__ import annotations

import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EQUIPMENT = [("ECC", "eccore"), ("NARVAL", "narval"), ("CoBo[0]", "actor"),
             ("Storage[0]", "storage"), ("Storage[1]", "storage")]
STATE = {"sm": 1, "run": 100, "ecc": None, "storage": {"Storage[0]": True, "Storage[1]": True}}


def answer(op: str, args: dict[str, str]) -> tuple[str, str]:
    """(element, fields xml) for one operation."""
    ok = "<ns:Error>0</ns:Error><ns:ErrorMessage></ns:ErrorMessage>"

    def refuse(message: str, element: str = "rccResponse") -> tuple[str, str]:
        return element, f"<ns:Error>1</ns:Error><ns:ErrorMessage>{message}</ns:ErrorMessage>"

    def sm(code: int) -> tuple[str, str]:
        return "rccResponseSM", f"{ok}<ns:SMState>{code}</ns:SMState><ns:SMTransition>0</ns:SMTransition><ns:SMError>0</ns:SMError>"

    name = args.get("name", "")
    if op == "rccStateMachine":
        return sm(STATE["sm"])
    if op == "rccEquipmentStateMachine":
        if not any(n == name for n, _ in EQUIPMENT):
            return refuse(f'The equipment "{name}" does not exist', "rccResponseSM")
        return sm(STATE["ecc"] if name == "ECC" and STATE["ecc"] is not None else STATE["sm"])
    if op == "rccGetEquipmentCount":
        return "rccResponseInt", f"{ok}<ns:value>{len(EQUIPMENT)}</ns:value>"
    if op == "rccGetEquipmentByIndex":
        n, t = EQUIPMENT[int(args["index"])]
        return "rccResponseEquip", f"{ok}<ns:name>{n}</ns:name><ns:typestr>{t}</ns:typestr>"
    if op == "rccReadStorageFileName":
        return "rccResponseString", f"{ok}<ns:value>{'run.dat' if STATE['storage'].get(name) else 'X'}</ns:value>"
    if op == "rccGetStorageRunNumber":
        return "rccResponseInt", f"{ok}<ns:value>{STATE['run']}</ns:value>"
    if op == "rccGetExperimentName":
        return "rccResponseString", f"{ok}<ns:value>fake</ns:value>"
    if op == "rccGetExperimentCfgFile":
        return "rccResponseString", f"{ok}<ns:value>/home/cens-alpha-00/ganacq_manip/fake/configurefiles/fake.rcc.xml</ns:value>"
    if op == "rccGetMonitoringState":
        return "rccResponseBool", f"{ok}<ns:value>true</ns:value>"
    if op == "rccInit":
        if STATE["sm"] not in (0, 1, 2):
            return refuse("Operation forbidden")
        STATE["sm"] = 2
        return "rccResponseString", f"{ok}<ns:value>Experiment initialized</ns:value>"
    if op == "rccStart":
        if STATE["sm"] != 2:
            return refuse("Operation forbidden")
        STATE["sm"] = 3
        return "rccResponseString", f"{ok}<ns:value>Experiment started</ns:value>"
    if op == "rccStop":
        if STATE["sm"] not in (3, 4):
            return refuse("Operation forbidden")
        STATE["sm"] = 2
        if any(STATE["storage"].values()):
            STATE["run"] += 1
        return "rccResponseString", f"{ok}<ns:value>Experiment stopped</ns:value>"
    if op == "rccExit":
        if STATE["sm"] in (3, 4):
            return refuse("Operation forbidden")
        STATE["sm"], STATE["ecc"] = 1, None
        return "rccResponseString", f"{ok}<ns:value>Experiment exited</ns:value>"
    if op == "rccEquipmentExit":
        STATE["ecc"] = 1
        return "rccResponseString", f'{ok}<ns:value>Equipment "{name}" exited</ns:value>'
    if op == "rccEquipmentInit":
        STATE["ecc"] = 2
        return "rccResponseString", f'{ok}<ns:value>Equipment "{name}" initialized</ns:value>'
    if op in ("rccStorageOn", "rccStorageOff"):
        if STATE["sm"] != 2:
            return refuse("Storage mode can be changed only in READY state")
        STATE["storage"][name] = op == "rccStorageOn"
        return "rccResponseString", f"{ok}<ns:value>storage</ns:value>"
    return refuse(f"fake: {op} not implemented")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        match = re.search(r"<\w+:Body>\s*<\w+:(\w+)>(.*?)</\w+:\1>", body, re.S)
        op, inner = (match.group(1), match.group(2)) if match else ("?", "")
        args = dict(re.findall(r"<rcc:(\w+)>(.*?)</rcc:\1>", inner))
        element, fields = answer(op, args)
        out = (f'<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
               f'xmlns:ns="urn:rcc"><SOAP-ENV:Body><ns:{element}>{fields}</ns:{element}></SOAP-ENV:Body></SOAP-ENV:Envelope>').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args) -> None:
        pass


def serve(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 6911
    print(f"fake RCC on 127.0.0.1:{port}")
    serve(port).serve_forever()
