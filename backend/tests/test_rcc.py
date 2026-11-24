"""The SOAP client and the state gate, against tools/fake_rcc.py.

    python tests/test_rcc.py
"""

import os
import sys
import tempfile
import threading
from pathlib import Path

os.environ["GANIL_DATA_DIR"] = tempfile.mkdtemp()
os.environ["RCC_PORT"] = "16911"
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import fake_rcc  # noqa: E402
from app import rcc  # noqa: E402


def test_gate_and_commands():
    server = fake_rcc.serve(16911)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        watch = rcc.Watch()
        snap = watch.read()
        assert snap["reachable"] and snap["state"]["name"] == "IDLE" and snap["storage"] == "on", snap
        assert len(snap["equipment"]) == 5
        assert snap["ecc_config"]["configure"] == "MeshPulser"
        assert [c["configure"] for c in snap["ecc_choices"]] == ["AllOff", "MeshPulser"]

        assert not watch.command("start", "t")["ok"]            # IDLE: refused here, not sent
        assert watch.command("init", "t")["ok"]
        assert watch.command("storage-off", "t")["ok"]
        assert watch.read()["storage"] == "off"
        result = watch.command("start", "t", "beam on target")
        assert result["ok"] and result["rcc"]["state"]["name"] == "RUNNING", result
        assert rcc.notes()[-1]["note"] == "beam on target"           # kept for the run it starts
        assert not watch.command("storage-on", "t")["ok"]       # not while running
        assert watch.command("stop", "t")["ok"]
        assert watch.read()["state"]["name"] == "READY"
        assert [e["command"] for e in rcc.audit()][:2] == ["stop", "storage-on"]
        assert not watch.command("ecc-init", "t")["ok"]          # the ECC is READY already
        assert watch.command("ecc-breakup", "t")["ok"]
        assert watch.command("ecc-init", "t")["ok"]
        assert watch.command("breakup", "t")["ok"]
        assert watch.read()["state"]["name"] == "IDLE"
    finally:
        server.shutdown()

    assert not rcc.Watch().read()["reachable"]                 # nobody on the port


def test_parse_config():
    xml = """<configuration xmlns="file:///x.rcc.xml"><format version="1"/>
<eccore name="ECC" hostname="127.0.0.1" configid='[ConfigId][SubConfigId type="describe"]d1[/SubConfigId][SubConfigId type="configure"]c1[/SubConfigId][/ConfigId]'>
<parameter name="bytes_in" value="0"/></eccore>
<narval name="NARVAL"><actor name="CoBo[0]" hostname="h"><parameter name="p" value="1"/></actor><storage name="Storage[0]"/></narval>
<link name="link_0" source="ECC" destination="CoBo[0]"/></configuration>"""
    got = rcc.parse_config(xml)
    assert [(e["kind"], e["name"], e["parent"]) for e in got["equipment"]] == [
        ("eccore", "ECC", ""), ("narval", "NARVAL", ""), ("actor", "CoBo[0]", "NARVAL"), ("storage", "Storage[0]", "NARVAL")]
    assert got["equipment"][0]["ecc_configs"] == {"describe": "d1", "configure": "c1"}
    assert got["links"][0]["destination"] == "CoBo[0]" and got["equipment"][2]["parameters"][0]["value"] == "1"


if __name__ == "__main__":
    test_parse_config()
    test_gate_and_commands()
    print("ok")
