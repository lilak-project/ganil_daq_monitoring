"""GET .xcfg files resolved the way CompoundConfig (and so cconfiged) resolves them.

An .xcfg is a tree of objects, each with a name and an index (`<Instance
id="3">`; no id is the empty index). `*` marks a default object: its contents
apply to the real instances beside it. Which value a real path gets is decided
by `Search` in CompoundConfig (src/CCfg/View/Search.hpp, GET 20210208); this is
that algorithm, line for line:

* at each level the exact index is tried, then `*`;
* the higher levels weigh more: CoBo[3].AsAd[1].x looks in CoBo[3].AsAd[1],
  then CoBo[*].AsAd[1], then CoBo[*].AsAd[*] -- it backtracks from the deepest
  instance upward, replacing one index with `*` at a time;
* a default only applies to an instance that exists: AsAd[*] does not stand
  for an AsAd[7] nobody declared (the `iInstance` rule).

The tests (tests/test_xcfg.py) are CompoundConfig's own: the fixtures of
test/CCfg/View/Configs.cpp and the answers of test/CCfg/CConfigTest.cpp.
"""

from __future__ import annotations

import itertools
from xml.etree import ElementTree

STAR = "*"


class Node:
    __slots__ = ("name", "index", "value", "attrs", "children", "parent", "include",
                 "span", "pre_comment", "post_comment")

    def __init__(self, name: str, index: str, value: str | None, attrs: dict, parent: "Node | None"):
        self.name, self.index, self.value, self.attrs = name, index, value, attrs
        self.children: list[Node] = []
        self.parent = parent
        self.include = attrs.get("include")
        #: Byte range of a scalar's text in the file, for writing a value back
        #: in place -- everything else in the file stays byte for byte.
        self.span: tuple[int, int] | None = None
        self.pre_comment = ""
        self.post_comment = ""

    def find(self, name: str, index: str) -> "Node | None":
        return next((c for c in self.children if c.name == name and c.index == index), None)

    @property
    def is_object(self) -> bool:
        return self.value is None

    def path(self) -> list[tuple[str, str]]:
        out, node = [], self
        while node.parent is not None:
            out.append((node.name, node.index))
            node = node.parent
        return out[::-1]


def parse(text: str | bytes) -> Node:
    """The tree, as XmlToConfigVisitor builds it, with each scalar's place in the file."""
    from xml.parsers import expat

    raw = text.encode("utf-8") if isinstance(text, str) else text
    parser = expat.ParserCreate("utf-8")
    stack: list[dict] = []
    root: list[Node] = []
    pending = {"comment": ""}

    def start(name, attrs):
        here = parser.CurrentByteIndex
        stack.append({"name": name, "attrs": dict(attrs), "kids": [], "text": [],
                      "open_end": _tag_end(raw, here), "comment": pending["comment"]})
        pending["comment"] = ""

    def end(name):
        here = parser.CurrentByteIndex
        frame = stack.pop()
        attrs = frame["attrs"]
        index = attrs.pop("id", "")
        # TinyXml's condensed whitespace: runs become one space, ends trimmed.
        text = " ".join("".join(frame["text"]).split())
        # XmlToConfigVisitor::isObject: a `type` attribute or a lone text makes
        # a scalar; no text at all -- an empty <Aget id="0"/> too -- an object.
        scalar = "type" in attrs or (text and not frame["kids"])
        node = Node(name, index, text if scalar else None, attrs, None)
        node.pre_comment = frame["comment"]
        if scalar and frame["open_end"] is not None and raw[here:here + 2] == b"</":
            node.span = (frame["open_end"], here)
        elif not scalar:
            node.post_comment = pending["comment"]      # comments left inside, before </x>
            pending["comment"] = ""
        node.children = frame["kids"]
        for kid in node.children:
            kid.parent = node
        if stack:
            stack[-1]["kids"].append(node)
        else:
            root.append(node)

    def chars(data):
        if stack:
            stack[-1]["text"].append(data)

    def comment(data):
        pending["comment"] += data

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.CommentHandler = comment
    parser.Parse(raw, True)
    return root[0]


def _tag_end(raw: bytes, start: int) -> int | None:
    """Where a start tag that begins at `start` ends (just after its `>`)."""
    quote = None
    for i in range(start, len(raw)):
        c = raw[i:i + 1]
        if quote:
            if c == quote:
                quote = None
        elif c in (b'"', b"'"):
            quote = c
        elif c == b">":
            return i + 1
    return None


class _Search:
    """CompoundConfig's View::Search<Node>, line for line (Search.hpp)."""

    def __init__(self, path: list[tuple[str, str]]):
        self.path = path
        self.i_instance = 0
        self.level = 0          # currentBacktrackLevel
        self.max_level = 0      # maxBacktrackLevel: what a Result reports

    # Search::search(fullSearchPath, start, startIndexInPath=0)
    def search(self, root: Node) -> Node | None:
        if len(self.path) == 1:
            return root
        return self.search_or_backtrack(1, root, None, False)

    # Search::searchNext(fullSearchPath, currentSibling)
    def search_next(self, sibling: Node) -> Node | None:
        self.i_instance = len(self.path) - 1
        return self.backtrack(len(self.path), sibling, None)

    # Search::search(iPath, current, backtrackLimit, btStart)
    def find(self, i: int, current: Node, limit: Node | None, bt_start: bool) -> Node | None:
        name, wanted = self.path[i]
        # Beginning a backtrack branch: look for the default object, not the
        # node itself, because that is where we came from.
        index = STAR if bt_start else wanted
        child = current.find(name, index)
        if child is None:
            # The default object only stands for an instance already met at this depth.
            if self.i_instance >= i and index != STAR:
                child = current.find(name, STAR)
                if child is None:
                    return None
            else:
                return None
        if self.i_instance < i:
            self.i_instance = i
        if i + 1 == len(self.path):
            return child
        return self.search_or_backtrack(i + 1, child, limit, False)

    # Search::backtrack(iPath, current, backtrackLimit)
    def backtrack(self, i: int, current: Node, limit: Node | None) -> Node | None:
        self.level += 1
        self.max_level = max(self.max_level, self.level)
        parent = current.parent
        if parent is not limit:
            if current.index == STAR:
                # Every possibility at this level is spent: go up again.
                result = self.backtrack(i - 1, parent, limit)
            else:
                result = self.search_or_backtrack(i - 1, parent, limit, True)
        else:
            result = None
        self.level -= 1
        return result

    # Search::searchOrBacktrack(iPath, current, backtrackLimit, btStart)
    def search_or_backtrack(self, i: int, current: Node, limit: Node | None, bt_start: bool) -> Node | None:
        # Nested backtracks cannot go past the current object.
        found = self.find(i, current, current.parent, bt_start)
        return found if found is not None else self.backtrack(i, current, limit)


def _full(root: Node, keys) -> list[tuple[str, str]]:
    return [(root.name, root.index)] + [tuple(k) for k in keys]


def search(root: Node, keys: list[tuple[str, str]]) -> Node | None:
    """The node a real path resolves to, or None -- CConfig::search()."""
    return _Search(_full(root, keys)).search(root)


def _inherited(root: Node, keys, start: Node) -> list[tuple[Node, int]]:
    """View::InheritedIterator: the default objects after `start`, most specific
    first, each with the backtrack level it took to reach it."""
    out, current = [], start
    while True:
        s = _Search(_full(root, keys))
        nxt = s.search_next(current)
        if nxt is None:
            return out
        out.append((nxt, s.max_level))
        current = nxt


def _listing(root: Node, keys, base: Node, base_level: int):
    """CConfig::getAttributes() on a config whose resolution is `base`, reached
    at `base_level`: the names in order, and for each the nearest (node, level)."""
    defaults = [(base, base_level)] + _inherited(root, keys, base)
    order: list[tuple[str, str]] = []
    lookup: dict[tuple[str, str], tuple[Node, int]] = {}
    for node, level in reversed(defaults):
        for child in node.children:
            key = (child.name, child.index)
            if key not in lookup:
                order.append(key)
            lookup[key] = (child, level)
    return order, lookup


def _config(root: Node, keys) -> tuple[Node, int] | None:
    """The config cconfiged holds for a path: walked down from the root the way
    ConfigNode::populate builds it, each child keeping the node and backtrack
    level its parent's listing gave it (CConfig(res, path, backtrackLevel))."""
    node, level = root, 0
    for i, key in enumerate(keys):
        _, lookup = _listing(root, keys[:i], node, level)
        if tuple(key) not in lookup:
            return None
        node, level = lookup[tuple(key)]
    return node, level


def children(root: Node, keys: list[tuple[str, str]]) -> list[dict]:
    """CConfig::getAttributes(): what cconfiged lists under an object.

    The object and its default objects (InheritedIterator), every child of
    each, the least specific object first so the file's order is kept; a name
    met again is taken from the nearer object. Default (`*`) children are
    listed too, as cconfiged lists them. `bl` is the backtrack level (the BL
    column): 0 is the object's own, more is inherited -- blue in cconfiged.
    `override`: an own value that a default also sets -- dark red there.
    """
    keys = [tuple(k) for k in keys]
    found_base = _config(root, keys)
    if found_base is None:
        return []
    order, lookup = _listing(root, keys, *found_base)
    rows = []
    for key in order:
        found, level = lookup[key]
        child_keys = list(keys) + [key]
        # ConfigNode::resolve(): an attribute overrides when one of its default
        # objects has the same index.
        override = any(d.index == found.index for d, _ in _inherited(root, child_keys, found))
        rows.append({
            "name": key[0],
            "index": key[1],
            "object": found.is_object,
            "value": None if found.is_object else found.value,
            "unit": found.attrs.get("unit"),
            "range": found.attrs.get("range"),
            "bl": level,
            "override": override,
            "from": None if level == 0 else ".".join(f"{n}[{i}]" if i else n for n, i in found.path()),
            "include": found.include,
            # cconfiged: bold name; red background when the range does not hold it.
            "bold": level == 0 and has_scalar_instances(found),
            "valid": found.is_object or in_range(found.value, parse_range(found.attrs.get("range"))),
            "type": None if found.is_object else value_of(found.value)[0],
            "comment": "\n".join(c for c in (found.pre_comment.strip(), found.post_comment.strip()) if c),
            # Editable in place: a scalar of this very object (an inherited one is overridden first).
            "editable": level == 0 and not found.is_object and found.span is not None,
        })
    return rows


# ── values and ranges ─────────────────────────────────────────────────────────
# Grammar/ValueGrammar.hpp: strict real (a dot is required), "0x" hex, int,
# bool (any case), else string; a number followed by anything is a string.
# Grammar/IntervalGrammar.hpp + EnumGrammar.hpp for ranges; Interval::contains
# and Enum::contains for the check (bounds must have the value's type).

import re as _re

_REAL = _re.compile(r"[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?")
_HEX = _re.compile(r"0x([0-9a-fA-F]+)")
_INT = _re.compile(r"[+-]?\d+")
INT_MIN, INT_MAX, UINT_MAX = -2**31, 2**31 - 1, 2**32 - 1


def value_of(text: str) -> tuple[str, object]:
    """(type, value) as CompoundConfig reads an untyped scalar's text."""
    text = text.strip()
    if _REAL.fullmatch(text):
        return "real", float(text)
    m = _HEX.fullmatch(text)
    if m and int(m.group(1), 16) <= UINT_MAX:
        return "hex", int(m.group(1), 16)
    if _INT.fullmatch(text) and INT_MIN <= int(text) <= INT_MAX:
        return "int", int(text)
    if text.lower() in ("true", "false"):
        return "bool", text.lower() == "true"
    return "string", text


def parse_range(text: str | None) -> dict | None:
    """An interval {kind, lower, upper, lower_mode, upper_mode} or an enum
    {kind, values}; None when there is no range or it cannot be read."""
    if not text:
        return None
    t = text.strip()
    m = _re.fullmatch(r"([\[\]])\s*([^,\[\]]*?)\s*,\s*([^,\[\]]*?)\s*([\[\]])", t)
    if m:
        lo, hi = m.group(2), m.group(3)
        return {
            "kind": "interval",
            "lower_mode": "unbounded" if lo == "-inf" else ("include" if m.group(1) == "[" else "exclude"),
            "upper_mode": "unbounded" if hi == "+inf" else ("include" if m.group(4) == "]" else "exclude"),
            "lower": None if lo == "-inf" else value_of(_unquote(lo)),
            "upper": None if hi == "+inf" else value_of(_unquote(hi)),
        }
    m = _re.fullmatch(r"\{\s*(.*?)\s*\}", t)
    if m:
        items = [x for x in _re.split(r"\s*,\s*", m.group(1)) if x] if m.group(1) else []
        return {"kind": "enum", "values": [value_of(_unquote(x)) for x in items]}
    return None


def _unquote(text: str) -> str:
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] == '"' else text


def in_range(value_text: str, rng: dict | None) -> bool:
    """ConfigNode::hasValidValue for a scalar: no range, or the range holds it."""
    if rng is None:
        return True
    kind, value = value_of(value_text)
    if rng["kind"] == "enum":
        return any(k == kind and v == value for k, v in rng["values"])
    if rng["lower_mode"] != "unbounded":
        k, lo = rng["lower"]
        if k != kind or not (lo <= value if rng["lower_mode"] == "include" else lo < value):
            return False
    if rng["upper_mode"] != "unbounded":
        k, hi = rng["upper"]
        if k != kind or not (value <= hi if rng["upper_mode"] == "include" else value < hi):
            return False
    return True


def has_scalar_instances(node: Node) -> bool:
    """ConfigNode::hasScalarInstances: an object whose own subtree, as written,
    holds a scalar (its name is bold in cconfiged)."""
    return node.is_object and any(not c.is_object or has_scalar_instances(c) for c in node.children)
