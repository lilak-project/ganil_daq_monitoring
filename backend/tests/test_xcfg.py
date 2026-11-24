"""xcfg.py against CompoundConfig's own tests.

The fixtures are test/CCfg/View/Configs.cpp (Config1-3) written as XML; the
answers are test/CCfg/CConfigTest.cpp's, GET 20210208. A path that the
library resolves to a node must resolve to the same node here.

    python tests/test_xcfg.py
"""

from app import xcfg

CONFIG1 = """<cfg><A id="*"><x>-1.5</x><y>0</y></A><A id="1"><y>5</y></A></cfg>"""
CONFIG2 = """<A>
  <B id="*"><C id="*"><D id="0"><x>4.5</x></D></C></B>
  <B id="0"><C id="0"><D id="*"><y>-2.3</y></D><D id="0"><z>0.1</z></D></C></B>
</A>"""
CONFIG3 = """<cfg>
  <A id="*"><B id="0"><C id="0"><D id="0"><x>4.5</x><y>-0.5</y><z>0.5</z></D></C></B></A>
  <A id="0">
    <B id="*"><C id="*"><D id="0"><y>2.5</y><w>-6.5</w></D></C></B>
    <B id="0"><C id="0"><D id="*"><x>0</x></D></C></B>
  </A>
</cfg>"""


def where(root, *keys):
    found = xcfg.search(root, [tuple(k) for k in keys])
    return None if found is None else ".".join(f"{n}[{i}]" if i else n for n, i in found.path())


def test_config1():
    root = xcfg.parse(CONFIG1)
    assert where(root, ("A", "1"), ("x", "")) == "A[*].x"            # testSearch1Back
    assert where(root, ("A", "*"), ("x", "")) == "A[*].x"            # testSearchNoBack
    assert xcfg.search(root, [("A", "1"), ("y", "")]).value == "5"   # testSearchOverriding
    assert where(root, ("A", "2"), ("x", "")) is None                # testSearchNotFound: no A[2]
    assert where(root, ("A", "1")) == "A[1]"                         # testSearchConsecutive
    # testIterator: A[1] holds x (from A[*]) then y (its own)
    rows = xcfg.children(root, [("A", "1")])
    assert [(r["name"], r["from"]) for r in rows] == [("x", "A[*].x"), ("y", None)], rows


def test_config2():
    root = xcfg.parse(CONFIG2)
    d0 = [("B", "0"), ("C", "0"), ("D", "0")]
    assert where(root, *d0) == "B[0].C[0].D[0]"                       # testSearchObjectNoBack
    assert where(root, *d0, ("z", "")) == "B[0].C[0].D[0].z"          # testSearchScalarNoBack
    assert where(root, *d0, ("y", "")) == "B[0].C[0].D[*].y"          # testSearchScalar1Back
    assert where(root, *d0, ("x", "")) == "B[*].C[*].D[0].x"          # testSearchScalar3Back
    assert where(root, *d0, ("w", "")) is None                        # testSearchNotFound2


def test_config3():
    root = xcfg.parse(CONFIG3)
    d0 = [("A", "0"), ("B", "0"), ("C", "0"), ("D", "0")]
    assert where(root, *d0) == "A[0].B[*].C[*].D[0]"                  # testSearch1Back3
    assert where(root, *d0, ("x", "")) == "A[*].B[0].C[0].D[0].x"     # testSearchNestedBack3
    # testIterateOrder: x, y, z, w -- and from where
    rows = xcfg.children(root, d0)
    assert [(r["name"], r["from"]) for r in rows] == [
        ("x", "A[*].B[0].C[0].D[0].x"),
        ("y", "A[0].B[*].C[*].D[0].y"),
        ("z", "A[*].B[0].C[0].D[0].z"),
        ("w", "A[0].B[*].C[*].D[0].w"),
    ], rows


def test_nest_iterate():
    """testNestIterate: what cconfiged lists under each object, defaults included."""
    root = xcfg.parse(CONFIG3)
    listed = lambda keys: [(r["name"], r["index"], r["bl"]) for r in xcfg.children(root, keys)]
    assert listed([]) == [("A", "*", 0), ("A", "0", 0)]
    assert listed([("A", "0")]) == [("B", "0", 0), ("B", "*", 0)]
    assert listed([("A", "0"), ("B", "0")]) == [("C", "0", 0), ("C", "*", 1)]


if __name__ == "__main__":
    test_nest_iterate()
    test_config1()
    test_config2()
    test_config3()
    print("ok")
