"""Saving edited ECC values: in place, backed up, refused when it should be.

    python tests/test_xcfg_save.py
"""

import hashlib
import os
import tarfile
import tempfile

os.environ["GANIL_DATA_DIR"] = tempfile.mkdtemp()
from app import rcc, xcfg  # noqa: E402

FILE = b"""<?xml version="1.0" encoding="UTF-8"?>
<Setup id="Conditions">
\t<!-- the threshold -->
\t<Node id="CoBo">
\t\t<Instance id="*">
\t\t\t<readoutDepth unit="buckets" range="[0, 512]">256</readoutDepth>
\t\t\t<Gain range="{120fC, 240fC}">120fC</Gain>
\t\t</Instance>
\t\t<Instance id="3"><isActive>true</isActive></Instance>
\t</Node>
</Setup>
"""
PATH = "/home/x/configurefiles/configure/configure-T.xcfg"
store = {"data": FILE}
like = tarfile.TarInfo("configure-T.xcfg")
like.uid, like.gid, like.mode = 1000, 1000, 0o644
rcc._docker_get = lambda path, limit=0: (store["data"], like)
rcc._docker_put = lambda path, data, info: store.update(data=data, put=(info.uid, info.mode))
rcc.ecc_file = lambda step, name: {"path": PATH, "text": store["data"].decode(), "sha": hashlib.sha256(store["data"]).hexdigest()}


def sha():
    return hashlib.sha256(store["data"]).hexdigest()


def test_save():
    star = [["Node", "CoBo"], ["Instance", "*"]]
    out = rcc.ecc_save("configure", "T", sha(), [{"keys": star + [["readoutDepth", ""]], "value": "500"},
                                                 {"keys": star + [["Gain", ""]], "value": "240fC"}], "t")
    assert out["changed"] == 2
    assert store["data"] == FILE.replace(b">256<", b">500<").replace(b">120fC<", b">240fC<")  # nothing else moved
    assert store["put"] == (1000, 0o644)                                                    # owner and mode kept
    assert open(out["backup"], "rb").read() == FILE
    root = xcfg.parse(store["data"])
    assert xcfg.search(root, [("Node", "CoBo"), ("Instance", "3"), ("readoutDepth", "")]).value == "500"

    for edits, why in (
        ([{"keys": star + [["readoutDepth", ""]], "value": "600"}], "outside"),         # out of range
        ([{"keys": star + [["Gain", ""]], "value": "1pC"}], "outside"),                  # not in the enum
        ([{"keys": [["Node", "CoBo"], ["Instance", "3"], ["readoutDepth", ""]], "value": "1"}], "override it first"),
    ):
        try:
            rcc.ecc_save("configure", "T", sha(), edits, "t")
        except ValueError as err:
            assert why in str(err), err
        else:
            raise AssertionError(f"accepted {edits}")
    try:
        rcc.ecc_save("configure", "T", "stale", [{"keys": star + [["readoutDepth", ""]], "value": "1"}], "t")
    except rcc.RCCError as err:
        assert "changed since" in str(err)
    else:
        raise AssertionError("saved over a file that changed")


if __name__ == "__main__":
    test_save()
    print("ok")
