import sqlite3

import pytest

from ai_desktop_agent.vm.table_memory import TableMemory


def test_query_groups_rows_and_preserves_key_strings_and_display_order():
    m = TableMemory()
    m.remember("Jobs", [[["Key"], ["009"], ["001"], ["004"]]])
    m.remember("Hours", [[["Key", "Hours"], ["001", "7"], ["009", "3"], ["001", "-2"]]])
    sql = (
        "SELECT j.c1,(SELECT coalesce(sum(h.c2),0) FROM t2 h WHERE h.c1=j.c1),0 "
        "FROM t1 j ORDER BY j.rowid"
    )
    assert m.select(sql) == [["009", 3, 0], ["001", 5, 0], ["004", 0, 0]]
    assert m.cells(sql, "Z2", ["", "", "=AA{row}*2"])[-1] == {"address": "AB4", "value": "=AA4*2"}
    m.query(sql)
    assert "009" in m.context()
    m.remember("Hours", [[["Key", "Hours"], ["001", "2"]]])
    assert m.last_result == ""
    assert m.select(sql)[1] == ["001", 2, 0]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t1",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA database_list",
        "select randomblob(90000)",
    ],
)
def test_only_bounded_read_queries(sql):
    m = TableMemory()
    m.remember("Jobs", [[["Key"], ["001"]]])
    with pytest.raises(sqlite3.DatabaseError):
        m.select(sql)
    assert m.select("select c1 from t1") == [["001"]]


def test_grid_checks_entire_output_before_writing():
    m = TableMemory()
    m.remember("Data", [[["key"], ["x"]]])
    with pytest.raises(ValueError, match="番地"):
        m.cells("select c1,c1 from t1", "XFD1", [])
    with pytest.raises(ValueError):
        m.cells("select '=EXTERNAL()' || c1 from t1", "A1", [])


def test_reuse_derived_rows_without_retyping_and_invalidate_on_source_change():
    m = TableMemory()
    m.remember(
        "Counts", [[["item", "delta"], ["A", "2"], ["A", "4"], ["B", "-3"]]], document="first"
    )
    m.query("select c1,sum(c2) as total from t1 group by c1")
    assert m.render("select * from q1 order by rowid", " | ") == "A | 6\nB | -3"
    assert m.cells("select c1,cast(c2 as real) from q1 order by rowid", "A2", [])[1]["value"] == "6"
    m.remember("Counts", [[["item", "delta"], ["Z", "9"]]], document="second")
    assert m.select("select * from t2") == [["Z", "9"]]
    assert m.select("select c1 from q1") == [["A"], ["B"]]
    m.remember("Counts", [[["item", "delta"], ["A", "5"]]], document="first")
    with pytest.raises(sqlite3.DatabaseError):
        m.select("select * from q1")


def test_bulk_output_requires_values_read_from_captured_tables():
    m = TableMemory()
    with pytest.raises(ValueError, match="FROM"):
        m.cells("select * from (values ('invented',4))", "B2", [])
