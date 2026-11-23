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

        assert not watch.command("start", "t")["ok"]            # IDLE: refused here, not sent
        assert watch.command("init", "t")["ok"]
        assert watch.command("storage-off", "t")["ok"]
        assert watch.read()["storage"] == "off"
        result = watch.command("start", "t")
        assert result["ok"] and result["rcc"]["state"]["name"] == "RUNNING", result
        assert not watch.command("storage-on", "t")["ok"]       # not while running
        assert watch.command("stop", "t")["ok"]
        assert watch.read()["state"]["name"] == "READY"
        assert [e["command"] for e in rcc.audit()][:2] == ["stop", "storage-on"]
    finally:
        server.shutdown()

    assert not rcc.Watch().read()["reachable"]                 # nobody on the port


if __name__ == "__main__":
    test_gate_and_commands()
    print("ok")
