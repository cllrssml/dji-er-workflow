"""Regression tests for the DJI frame clock.

DJI Fly 46 logs decode `custom.dateTime` as garbage on most frames - random
instants from 1970 to 2099, many in plausible years - interleaved with the
correct time. A year-range guard let 2015-2027 garbage through and ~30
flights filed in the wrong year. The clock is now `dateTime - flyTime` voted
against the filename (local time, minus the RC's UTC offset).

`dji_er_tasks` imports geopandas/ecoscope at module scope, which the test
environment need not have, so only the clock helpers are exec'd out of the
real shipped source file.
"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "dji_er_tasks" / "__init__.py"
WANTED = {"_parse_dt", "_datetime_from_filename", "resolve_frame_clock",
          "RC_UTC_OFFSET_H", "_CLOCK_AGREE"}


def _load_helpers():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    keep = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANTED)
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in WANTED for t in n.targets))
        or (isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) in WANTED)
    ]
    ns = {"datetime": datetime, "timedelta": timedelta, "timezone": timezone,
          "re": __import__("re"), "Path": Path}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(SRC), "exec"), ns)
    return ns


clock_of = _load_helpers()["resolve_frame_clock"]

FILENAME = Path("DJIFlightRecord_2026-08-20_[15-12-46].txt")   # SAST
TRUE_CLOCK = datetime(2026, 8, 20, 13, 12, 46, 500000, tzinfo=timezone.utc)


def frame(fly_s: float, dt: datetime | str) -> dict:
    s = dt if isinstance(dt, str) else dt.isoformat().replace("+00:00", "Z")
    return {"custom": {"dateTime": s}, "osd": {"flyTime": fly_s}}


def good(fly_s: float) -> dict:
    return frame(fly_s, TRUE_CLOCK + timedelta(seconds=fly_s))


def test_clean_log_clock_is_the_frames_own():
    frames = [good(t / 5) for t in range(50)]
    assert clock_of(frames, FILENAME) == TRUE_CLOCK


def test_plausible_year_garbage_is_outvoted():
    # The real failure: garbage that passes any year-range guard.
    frames = [frame(0.1, "1970-01-01T00:00:00Z"),
              frame(1.0, "2026-02-21T02:50:34.932Z"),
              frame(2.0, "2018-02-23T08:22:44.313Z"),
              frame(3.0, "2027-04-03T10:19:44.566Z"),
              good(50.7), frame(60.0, "2026-07-10T13:57:40Z"), good(864.7), good(965.9)]
    assert clock_of(frames, FILENAME) == TRUE_CLOCK


def test_garbage_majority_still_loses_to_filename_agreement():
    frames = [frame(i, f"20{30 + i}-01-01T00:00:00Z") for i in range(40)] + [good(500.0)]
    assert clock_of(frames, FILENAME) == TRUE_CLOCK


def test_no_agreeing_frame_falls_back_to_filename_minus_utc_offset():
    frames = [frame(1.0, "2017-05-30T22:10:29Z"), frame(2.0, "1970-01-01T00:00:00Z")]
    assert clock_of(frames, FILENAME) == datetime(2026, 8, 20, 13, 12, 46, tzinfo=timezone.utc)


def test_filename_is_local_time_not_utc():
    # A fallback must never store the SAST filename labelled as UTC (+2 h error).
    got = clock_of([], FILENAME)
    assert got.hour == 13


def test_unparseable_datetimes_are_ignored():
    frames = [frame(1.0, "not a date"), {"custom": {}, "osd": {"flyTime": 2.0}}, good(10.0)]
    assert clock_of(frames, FILENAME) == TRUE_CLOCK


def test_no_filename_uses_largest_cluster():
    frames = [good(t) for t in range(10)] + [frame(1.0, "2099-01-01T00:00:00Z"),
                                             frame(2.0, "2044-04-27T04:56:52Z")]
    assert clock_of(frames, Path("garbled.txt")) == TRUE_CLOCK


def test_nothing_usable_returns_none():
    assert clock_of([frame(1.0, "junk")], Path("garbled.txt")) is None
