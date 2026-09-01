"""Regression tests for the corrupt-timestamp guard on DJI takeoff times.

CFW ended up with 24 flights filed in ER between 2027 and 2099 because the
guard rejected timestamps that were too OLD but never ones that were too NEW:
a future-dated frame passed `dt.year >= _MIN_YEAR` and became the flight_key.

`dji_er_tasks` imports geopandas/ecoscope at module scope, which the test
environment need not have. These tests therefore exec only the timestamp
helpers out of the real shipped source file, so they still fail if that file
regresses.
"""

import ast
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "dji_er_tasks" / "__init__.py"
WANTED = {
    "_parse_dt",
    "_datetime_from_filename",
    "_max_flight_year",
    "resolve_takeoff_dt",
    "MIN_FLIGHT_YEAR",
}


def _load_helpers():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    keep = [
        node
        for node in tree.body
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANTED)
        or (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) in WANTED for t in node.targets)
        )
    ]
    found = {getattr(n, "name", None) or n.targets[0].id for n in keep}
    missing = WANTED - found
    assert not missing, f"helpers missing from {SRC}: {sorted(missing)}"
    ns = {"datetime": datetime, "timezone": timezone, "re": __import__("re"), "Path": Path}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(SRC), "exec"), ns)
    return ns


HELPERS = _load_helpers()
resolve_takeoff_dt = HELPERS["resolve_takeoff_dt"]

FILENAME = Path("DJIFlightRecord_2026-08-15_[07-14-35].txt")
FILENAME_DT = datetime(2026, 8, 15, 7, 14, 35, tzinfo=timezone.utc)


def frame(dt_str: str, on_ground: bool = False) -> dict:
    return {"custom": {"dateTime": dt_str}, "osd": {"isOnGround": on_ground}}


def test_sane_takeoff_frame_is_used_as_is():
    frames = [frame("2026-08-15T05:14:35Z"), frame("2026-08-15T05:14:36Z")]
    assert resolve_takeoff_dt(frames, 0, FILENAME) == datetime(
        2026, 8, 15, 5, 14, 35, tzinfo=timezone.utc
    )


def test_future_dated_takeoff_frame_is_rejected():
    """The bug: a 2099 takeoff frame used to sail through and become the key."""
    frames = [frame("2099-11-07T10:38:40Z"), frame("2026-08-15T05:14:36Z")]
    got = resolve_takeoff_dt(frames, 0, FILENAME)
    assert got.year == 2026, f"future timestamp leaked through: {got}"
    assert got == datetime(2026, 8, 15, 5, 14, 36, tzinfo=timezone.utc)


def test_ancient_takeoff_frame_is_rejected():
    frames = [frame("1970-01-01T00:00:00Z"), frame("2026-08-15T05:14:36Z")]
    assert resolve_takeoff_dt(frames, 0, FILENAME).year == 2026


@pytest.mark.parametrize("bad", ["2099-11-07T10:38:40Z", "2085-01-16T03:33:37Z", "1970-01-01T00:00:00Z"])
def test_all_frames_corrupt_falls_back_to_filename(bad):
    """Every frame garbage -> filename. Future-only garbage used to skip this."""
    frames = [frame(bad), frame(bad), frame(bad)]
    assert resolve_takeoff_dt(frames, 0, FILENAME) == FILENAME_DT


def test_grounded_frames_are_not_used_as_takeoff():
    frames = [
        frame("2099-11-07T10:38:40Z"),
        frame("2026-08-15T05:14:36Z", on_ground=True),
        frame("2026-08-15T05:14:40Z"),
    ]
    assert resolve_takeoff_dt(frames, 0, FILENAME) == datetime(
        2026, 8, 15, 5, 14, 40, tzinfo=timezone.utc
    )


def test_unparseable_frames_are_skipped_not_fatal():
    frames = [frame("2099-11-07T10:38:40Z"), frame("not-a-date"), frame("2026-08-15T05:14:41Z")]
    assert resolve_takeoff_dt(frames, 0, FILENAME).year == 2026


def test_next_year_is_allowed_but_year_after_is_not():
    """_MAX_YEAR is now+1, so clock skew across New Year is tolerated."""
    nxt = datetime.now(timezone.utc).year + 1
    frames = [frame(f"{nxt}-01-02T05:00:00Z")]
    assert resolve_takeoff_dt(frames, 0, FILENAME).year == nxt

    frames = [frame(f"{nxt + 1}-01-02T05:00:00Z")]
    assert resolve_takeoff_dt(frames, 0, FILENAME) == FILENAME_DT


def test_no_filename_match_returns_original_rather_than_crashing():
    frames = [frame("2099-11-07T10:38:40Z")]
    got = resolve_takeoff_dt(frames, 0, Path("garbled.txt"))
    assert got.year == 2099  # documented last resort: never raise mid-batch
