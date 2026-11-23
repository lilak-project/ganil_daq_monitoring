"""Runs the GUI log never saw, recovered from rcc.txt.

    python tests/test_recover.py
"""

from app import rcclog, runs

GUI = """(22:18:52 - 23/11/2026) INFO - rcc_gui - Started run #240
note
(22:19:22 - 23/11/2026) INFO - rcc_gui - Stopped run #240
"""

RCC = """(21:48:13 - 23/11/26) INFO - rcc - received START
(21:48:13 - 23/11/26) INFO - rcc - Storage[0] : starting storage of run #239
(21:48:13 - 23/11/26) INFO - rcc - Storage[1] : starting storage of run #239
(21:48:14 - 23/11/26) INFO - rcc - Storage[2] : starting storage of run #239
(21:57:51 - 23/11/26) INFO - rcc - received STOP
(22:18:52 - 23/11/26) INFO - rcc - received START
(22:18:52 - 23/11/26) INFO - rcc - Storage[0] : starting storage of run #240
(22:19:22 - 23/11/26) INFO - rcc - received STOP
(22:27:39 - 23/11/26) INFO - rcc - received START
(22:27:44 - 23/11/26) INFO - rcc - finished execution of START
"""


def test_recover():
    events = rcclog.parse_gui(GUI)
    storage = rcclog.parse_rcc(RCC).storage
    got = [(a.run, a.started.strftime("%H:%M:%S"), a.stopped and a.stopped.strftime("%H:%M:%S"),
            a.storage_off, a.from_server_log) for a in runs.build_attempts(events, [], storage)]
    assert got == [
        (239, "21:48:13", "21:57:51", False, True),  # once, not once per storage actor
        (240, "22:18:52", "22:19:22", False, False),  # the GUI has it: not doubled
        (241, "22:27:39", None, True, True),  # started by a script, storage off: live
    ], got


def test_half_written_line_is_left_for_next_read():
    import tempfile
    from pathlib import Path
    log = Path(tempfile.mkdtemp()) / "rcc.txt"
    log.write_bytes(b"(22:27:39 - 23/11/26) INFO - rcc - received START\n(23:06:06 - 23/11/26) INFO - rcc - received ST")
    seen, offset = [], 0
    for text, offset in rcclog.iter_tail(log, offset):
        seen += rcclog.parse_rcc(text).storage
    with log.open("ab") as handle:
        handle.write(b"OP\n")
    for text, offset in rcclog.iter_tail(log, offset):
        seen += rcclog.parse_rcc(text).storage
    assert [e.mode for e in seen] == ["start", "stop"], seen


if __name__ == "__main__":
    test_half_written_line_is_left_for_next_read()
    test_recover()
    print("ok")
