import io

import pytest

import engines
from engines import Table


CSV = """\
region,product,units,price,sold_at,in_stock
north,widget,10,2.5,2024-01-15,true
north,gadget,3,10.0,2024-02-01,false
south,widget,7,2.5,2024-01-20,true
south,gadget,5,10.0,2024-03-10,true
east,widget,,2.5,2024-03-12,false
"""


@pytest.fixture()
def table() -> Table:
    return Table.from_csv_text(CSV, "sales.csv")


def test_type_inference(table: Table) -> None:
    assert table.col_types == {
        "region": "string",
        "product": "string",
        "units": "number",
        "price": "number",
        "sold_at": "date",
        "in_stock": "bool",
    }
    assert table.rows[4]["units"] is None


def test_describe_numeric(table: Table) -> None:
    info = engines.describe(table, "units")["units"]
    assert info["type"] == "number"
    assert info["non_empty"] == 4
    assert info["missing"] == 1
    assert info["min"] == 3
    assert info["max"] == 10
    assert info["mean"] == 6.25


def test_describe_string_top_values(table: Table) -> None:
    info = engines.describe(table, "region")["region"]
    assert info["unique"] == 3
    assert ("north", 2) in [tuple(v) for v in info["top_values"]]


def test_filters(table: Table) -> None:
    rows = engines.apply_filters(
        table,
        [{"column": "region", "op": "eq", "value": "north"}],
    )
    assert len(rows) == 2

    rows = engines.apply_filters(
        table,
        [
            {"column": "price", "op": "gte", "value": "10"},
            {"column": "in_stock", "op": "eq", "value": "true"},
        ],
    )
    assert len(rows) == 1
    assert rows[0]["region"] == "south"

    rows = engines.apply_filters(
        table,
        [{"column": "product", "op": "in", "value": ["gadget"]}],
    )
    assert len(rows) == 2

    rows = engines.apply_filters(table, [{"column": "units", "op": "is_null"}])
    assert len(rows) == 1 and rows[0]["region"] == "east"

    rows = engines.apply_filters(
        table,
        [{"column": "sold_at", "op": "between", "value": "2024-01-01", "value2": "2024-01-31"}],
    )
    assert len(rows) == 2


def test_unknown_column_and_op(table: Table) -> None:
    with pytest.raises(engines.AnalysisError):
        engines.apply_filters(table, [{"column": "nope", "op": "eq", "value": "x"}])
    with pytest.raises(engines.AnalysisError):
        engines.apply_filters(table, [{"column": "region", "op": "drop", "value": "x"}])


def test_group_and_aggregate(table: Table) -> None:
    result = engines.group_and_aggregate(
        table,
        group_by=["region"],
        metrics=[{"column": "units", "function": "sum", "alias": "total_units"}],
        filters=None,
        order_by="total_units",
        order_desc=True,
        limit=10,
    )
    by_region = {g["region"]: g["total_units"] for g in result["groups"]}
    assert by_region == {"north": 13.0, "south": 12.0, "east": 0.0}
    assert result["group_count"] == 3


def test_group_with_filters_and_mean(table: Table) -> None:
    result = engines.group_and_aggregate(
        table,
        group_by=["product"],
        metrics=[{"column": "units", "function": "mean"}],
        filters=[{"column": "in_stock", "op": "eq", "value": "true"}],
        order_by="mean_units",
        order_desc=True,
        limit=10,
    )
    by_product = {g["product"]: g["mean_units"] for g in result["groups"]}
    assert by_product == {"widget": 8.5, "gadget": 5.0}


def test_group_rejects_text_sum(table: Table) -> None:
    with pytest.raises(engines.AnalysisError):
        engines.group_and_aggregate(
            table,
            group_by=["region"],
            metrics=[{"column": "product", "function": "sum"}],
            filters=None,
            order_by=None,
            order_desc=True,
            limit=10,
        )


def test_semicolon_and_bom() -> None:
    text = "\ufeffa;b\n1;2\n3;4\n"
    table = Table.from_csv_text(text, "t.csv")
    assert table.headers == ["a", "b"]
    assert len(table.rows) == 2


def test_empty_and_garbage() -> None:
    with pytest.raises(engines.AnalysisError):
        Table.from_csv_text("", "empty.csv")
    with pytest.raises(engines.AnalysisError):
        Table.from_csv_text("just one line", "bad.csv")


def test_preview_formats_dates_and_floats(table: Table) -> None:
    rows = engines.preview(table, 2)
    assert rows[0]["sold_at"] == "2024-01-15 00:00:00"
    assert rows[0]["price"] == 2.5 and rows[0]["units"] == 10
