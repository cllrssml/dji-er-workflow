"""Regression tests for resolving an event UUID out of an ecoscope events frame.

ecoscope's ERClient.get_events does `gdf.set_index("id")`, so the returned rows
have NO "id" column. `row.get("id")` therefore returned None, the caller built
the URL "activity/event/None", ER 404'd, and the already-ingested folio was
never linked to the patrol the run had just created. Batch 1 of the 2026-09-02
backfill created four drone_patrols with event_count 0 this way.
"""

import ast
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

SRC = Path(__file__).resolve().parents[1] / "dji_er_tasks" / "__init__.py"


def _load():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    keep = [
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_event_id"
    ]
    assert keep, f"_event_id missing from {SRC}"
    keep = [
        n
        for n in tree.body
        if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_event_id")
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "_UUID_RE" for t in n.targets))
    ]
    ns: dict[str, Any] = {"Any": Any, "re": __import__("re")}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(SRC), "exec"), ns)
    return ns["_event_id"]


_event_id = _load()

UUID = "3a7d8819-494f-4236-ac4c-323bbebfa13c"


def _frame_indexed_by_id():
    """Exactly what ecoscope get_events returns: id is the index, not a column."""
    return pd.DataFrame({"time": ["2024-01-04T15:48:18+00:00"]}, index=pd.Index([UUID], name="id"))


def test_id_read_from_index_when_there_is_no_id_column():
    row = next(r for _, r in _frame_indexed_by_id().iterrows())
    assert "id" not in row.index
    assert _event_id(row) == UUID


def test_id_column_still_works_if_ecoscope_stops_indexing():
    df = pd.DataFrame({"id": [UUID], "time": ["x"]})
    assert _event_id(df.iloc[0]) == UUID


def test_index_wins_over_a_null_id_column():
    df = pd.DataFrame({"id": [None], "time": ["x"]}, index=pd.Index([UUID], name="id"))
    assert _event_id(df.iloc[0]) == UUID


@pytest.mark.parametrize("bad", [None, float("nan"), ""])
def test_unresolvable_returns_none_not_the_string_None(bad):
    """The bug: str(None) == 'None' produced the URL activity/event/None."""
    df = pd.DataFrame({"id": [bad]}, index=pd.RangeIndex(1))
    assert _event_id(df.iloc[0]) is None


def test_never_emits_the_literal_string_None_for_a_fully_empty_row():
    row = pd.Series({"time": "x"})
    assert _event_id(row) is None


def test_positional_index_label_is_not_mistaken_for_an_id():
    """A RangeIndex label is 0, 1, 2 ... - never a valid event UUID."""
    df = pd.DataFrame({"time": ["x"]}, index=pd.RangeIndex(1))
    assert _event_id(df.iloc[0]) is None
