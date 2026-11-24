import sqlite3

import pytest

from app.db import readonly_connection
from app.lake import warehouse as wh

CSV = b"Region,Sales Amount\nnorth,10\nsouth,20\nnorth,5\n"


@pytest.fixture(scope="module")
def sales(tmp_path_factory):
    raw = tmp_path_factory.mktemp("raw") / "sales.csv"
    raw.write_bytes(CSV)
    return wh.load_dataframe("sales", "sales.csv", CSV, raw, None)


def test_load_normalizes_columns(sales):
    assert sales.table_name == "ds_sales" and sales.row_count == 3
    assert [c["name"] for c in sales.columns] == ["region", "sales_amount"]


def test_select_runs_and_limits(sales):
    out = wh.run_query("SELECT region, SUM(sales_amount) AS total FROM ds_sales GROUP BY region ORDER BY region")
    assert out["rows"] == [["north", 15], ["south", 20]]
    assert wh.run_query("SELECT * FROM ds_sales", limit=2)["truncated"] is True


@pytest.mark.parametrize("sql", [
    "DROP TABLE ds_sales",
    "DELETE FROM ds_sales",
    "SELECT 1; DROP TABLE ds_sales",
    "PRAGMA writable_schema=1",
    "ATTACH DATABASE 'x.db' AS x",
    "",
    "-- just a comment",
])
def test_writes_and_multi_statements_rejected(sales, sql):
    with pytest.raises(wh.SQLValidationError):
        wh.run_query(sql)


def test_readonly_connection_cannot_write_even_if_validation_is_bypassed(sales):
    conn = readonly_connection()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM ds_sales")
    conn.close()
    assert wh.run_query("SELECT COUNT(*) FROM ds_sales")["rows"] == [[3]]


def test_curated_table_roundtrip_and_csv_export(sales):
    ds = wh.create_curated("north_sales", "SELECT * FROM ds_sales WHERE region='north'")
    assert ds.table_name == "cur_north_sales" and ds.row_count == 2
    assert wh.run_query("SELECT SUM(sales_amount) FROM cur_north_sales")["rows"] == [[15]]
    from app.lake.zones import zone_dir
    assert (zone_dir("curated") / "cur_north_sales.csv").exists()


def test_schema_hides_system_tables(sales):
    names = {t["table"] for t in wh.schema()}
    assert "ds_sales" in names and "documents" not in names
