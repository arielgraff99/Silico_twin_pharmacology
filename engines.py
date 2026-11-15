"""Pure-Python CSV analysis engine.

No code execution, no eval, no dynamic imports: every operation is a fixed,
type-checked computation over parsed rows. The Agents SDK tools in tools.py
are thin wrappers around this module.
"""

from __future__ import annotations

import csv
import io
import math
import re
import statistics
from datetime import datetime

MAX_FILTER_ROWS = 200
MAX_GROUP_ROWS = 500
TOP_VALUES = 5


class AnalysisError(Exception):
    """Raised for user-fixable problems: bad column, bad operator, bad value."""


def _parse_bool(text: str) -> bool | None:
    lowered = text.strip().lower()
    if lowered in {"true", "yes", "y"}:
        return True
    if lowered in {"false", "no", "n"}:
        return False
    return None


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S")


def _parse_date(text: str) -> datetime | None:
    stripped = text.strip()
    try:
        return datetime.fromisoformat(stripped)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(stripped, fmt)
        except ValueError:
            continue
    return None


_INT_RE = re.compile(r"^[+-]?\d+$")


def _parse_scalar(text: str) -> object:
    """Parse one CSV cell into bool | int | float | datetime | str."""
    if _INT_RE.match(text):
        try:
            return int(text)
        except ValueError:
            pass
    try:
        value = float(text)
        if not (math.isnan(value) or math.isinf(value)):
            return value
    except ValueError:
        pass
    as_bool = _parse_bool(text)
    if as_bool is not None:
        return as_bool
    as_date = _parse_date(text)
    if as_date is not None:
        return as_date
    return text


def _infer_column_type(values: list[str]) -> str:
    """Return the dominant type ('number', 'bool', 'date', 'string') of a column."""
    if not values:
        return "string"
    scores = {"number": 0, "bool": 0, "date": 0, "string": 0}
    for text in values:
        parsed = _parse_scalar(text)
        if isinstance(parsed, bool):
            scores["bool"] += 1
        elif isinstance(parsed, (int, float)):
            scores["number"] += 1
        elif isinstance(parsed, datetime):
            scores["date"] += 1
        else:
            scores["string"] += 1
    total = len(values)
    best = max(scores, key=lambda kind: scores[kind])
    if best != "string" and scores[best] / total < 0.8:
        return "string"
    return best


def _coerce(text: str, kind: str) -> object:
    if kind == "number":
        try:
            return float(text)
        except ValueError as exc:
            raise AnalysisError(f"Value {text!r} is not a number.") from exc
    if kind == "bool":
        parsed = _parse_bool(text) if isinstance(text, str) else None
        if parsed is None and isinstance(text, bool):
            return text
        if parsed is None:
            raise AnalysisError(f"Value {text!r} is not a boolean (true/false).")
        return parsed
    if kind == "date":
        parsed = _parse_date(text) if isinstance(text, str) else None
        if parsed is None:
            raise AnalysisError(f"Value {text!r} is not a recognized date.")
        return parsed
    return text


def _format_value(value: object) -> object:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return value


class Table:
    """A parsed CSV dataset held in memory for one session."""

    def __init__(self, name: str, headers: list[str], rows: list[dict], col_types: dict[str, str]):
        self.name = name
        self.headers = headers
        self.rows = rows
        self.col_types = col_types

    @classmethod
    def from_csv_text(cls, csv_text: str, name: str) -> "Table":
        if not csv_text or not csv_text.strip():
            raise AnalysisError("The CSV content is empty.")
        text = csv_text.lstrip("\ufeff")
        delimiter = ","
        sample = text[:4096]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            pass
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        raw_rows = [row for row in reader if any(cell.strip() for cell in row)]
        if len(raw_rows) < 2:
            raise AnalysisError(
                "The content does not look like a CSV with a header row and at least one data row."
            )
        headers = [header.strip() for header in raw_rows[0]]
        width = len(headers)
        if len(set(headers)) != width:
            raise AnalysisError("The CSV has duplicate column names.")
        records: list[dict] = []
        for line_no, row in enumerate(raw_rows[1:], start=2):
            if len(row) < width:
                row = row + [""] * (width - len(row))
            elif len(row) > width:
                raise AnalysisError(
                    f"Line {line_no} has {len(row)} cells but the header has {width} columns."
                )
            records.append({header: (cell.strip() or None) for header, cell in zip(headers, row)})

        col_types: dict[str, str] = {}
        for header in headers:
            non_empty = [r[header] for r in records if r[header] is not None]
            col_types[header] = _infer_column_type(non_empty)

        parsed_rows: list[dict] = []
        for record in records:
            parsed: dict = {}
            for header in headers:
                raw = record[header]
                parsed[header] = None if raw is None else _coerce(raw, col_types[header])
            parsed_rows.append(parsed)
        return cls(name=name, headers=headers, rows=parsed_rows, col_types=col_types)

    def column(self, name: str) -> str:
        if name in self.headers:
            return name
        lowered = {h.lower(): h for h in self.headers}
        if name.lower() in lowered:
            return lowered[name.lower()]
        raise AnalysisError(
            f"No column {name!r}. Available columns: {', '.join(self.headers)}."
        )

    def kind(self, column: str) -> str:
        return self.col_types[column]

    def values(self, column: str) -> list[object]:
        return [row[column] for row in self.rows if row[column] is not None]


FILTER_OPS = (
    "eq", "ne", "gt", "gte", "lt", "lte",
    "contains", "startswith", "endswith", "in", "not_in",
    "between", "is_null", "not_null",
)


def _matches(row_value: object, op: str, value: object, value2: object, kind: str) -> bool:
    if op == "is_null":
        return row_value is None
    if op == "not_null":
        return row_value is not None
    if op in {"in", "not_in"}:
        if not isinstance(value, (list, tuple)):
            raise AnalysisError(f"Operator '{op}' needs a list of values.")
        hits = any(_matches(row_value, "eq", item, None, kind) for item in value)
        return hits if op == "in" else not hits

    if row_value is None:
        return False
    coerced = _coerce(value, kind) if isinstance(value, str) or value is not None else None

    if op == "eq":
        if isinstance(row_value, float) and isinstance(coerced, (int, float)):
            return math.isclose(row_value, coerced, rel_tol=1e-9, abs_tol=1e-12)
        return row_value == coerced
    if op == "ne":
        return not _matches(row_value, "eq", value, value2, kind)
    if op in {"gt", "gte", "lt", "lte"}:
        if coerced is None:
            raise AnalysisError(f"Operator '{op}' needs a value.")
        try:
            if op == "gt":
                return row_value > coerced
            if op == "gte":
                return row_value >= coerced
            if op == "lt":
                return row_value < coerced
            return row_value <= coerced
        except TypeError as exc:
            raise AnalysisError(
                f"Cannot compare {row_value!r} with {coerced!r} in column of type {kind!r}."
            ) from exc
    if op == "between":
        low = _coerce(value, kind) if value is not None else None
        high = _coerce(value2, kind) if value2 is not None else None
        if low is None or high is None:
            raise AnalysisError("Operator 'between' needs both 'value' and 'value2'.")
        return low <= row_value <= high
    if op in {"contains", "startswith", "endswith"}:
        haystack = str(row_value).lower()
        needle = str(value).lower()
        if op == "contains":
            return needle in haystack
        if op == "startswith":
            return haystack.startswith(needle)
        return haystack.endswith(needle)
    raise AnalysisError(f"Unknown operator {op!r}. Valid operators: {', '.join(FILTER_OPS)}.")


def apply_filters(table: Table, filters: list[dict]) -> list[dict]:
    checked: list[tuple[str, str, object, object]] = []
    for f in filters:
        column = table.column(f["column"])
        op = f["op"]
        if op not in FILTER_OPS:
            raise AnalysisError(f"Unknown operator {op!r}. Valid operators: {', '.join(FILTER_OPS)}.")
        checked.append((column, op, f.get("value"), f.get("value2")))
    result = []
    for row in table.rows:
        if all(
            _matches(row[column], op, value, value2, table.kind(column))
            for column, op, value, value2 in checked
        ):
            result.append(row)
    return result


AGGREGATIONS = ("count", "sum", "mean", "median", "min", "max", "nunique", "std")


def _aggregate(values: list[object], function: str) -> object:
    if function == "count":
        return len(values)
    if function == "nunique":
        return len({str(_format_value(v)) for v in values})
    numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if function in {"sum", "mean", "median", "min", "max", "std"}:
        if not numbers:
            if function == "sum":
                return 0.0
            raise AnalysisError(
                f"Aggregation '{function}' needs at least one numeric value, "
                "but the group has none (the column may be text or the values all missing)."
            )
        if function == "sum":
            return float(sum(numbers))
        if function == "mean":
            return statistics.fmean(numbers)
        if function == "median":
            return statistics.median(numbers)
        if function == "min":
            return float(min(numbers))
        if function == "max":
            return float(max(numbers))
        return statistics.stdev(numbers) if len(numbers) >= 2 else None
    raise AnalysisError(f"Unknown aggregation {function!r}. Valid: {', '.join(AGGREGATIONS)}.")


def group_and_aggregate(
    table: Table,
    group_by: list[str],
    metrics: list[dict],
    filters: list[dict] | None,
    order_by: str | None,
    order_desc: bool,
    limit: int,
) -> dict:
    rows = apply_filters(table, filters) if filters else table.rows
    keys = [table.column(g) for g in group_by]
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = tuple(row[k] for k in keys)
        groups.setdefault(key, []).append(row)

    aliases: list[str] = []
    computed: list[dict] = []
    for key, group_rows in groups.items():
        entry: dict = {}
        for k, v in zip(keys, key):
            entry[k] = _format_value(v)
        for metric in metrics:
            function = metric["function"]
            if function not in AGGREGATIONS:
                raise AnalysisError(
                    f"Unknown aggregation {function!r}. Valid: {', '.join(AGGREGATIONS)}."
                )
            alias = metric.get("alias") or (
                function if metric.get("column") is None else f"{function}_{metric['column']}"
            )
            if alias in entry or alias in keys:
                raise AnalysisError(
                    f"Alias {alias!r} collides with a group column; pass a different 'alias'."
                )
            if metric.get("column") is None:
                if function != "count":
                    raise AnalysisError(f"Aggregation '{function}' needs a 'column'.")
                entry[alias] = len(group_rows)
            else:
                column = table.column(metric["column"])
                if function in {"sum", "mean", "median", "min", "max", "std"} and table.kind(column) != "number":
                    raise AnalysisError(
                        f"Aggregation '{function}' needs a numeric column, "
                        f"but {column!r} is of type {table.kind(column)!r}."
                    )
                values = [r[column] for r in group_rows if r[column] is not None]
                entry[alias] = _round(_aggregate(values, function))
            aliases.append(alias)
        computed.append(entry)

    if order_by:
        order_column = order_by
        if order_column not in keys and order_column not in aliases:
            order_column = table.column(order_by)
        computed.sort(
            key=lambda e: (e.get(order_column) is None, e.get(order_column)),
            reverse=order_desc,
        )
    elif aliases:
        computed.sort(key=lambda e: (e.get(aliases[0]) is None, e.get(aliases[0])), reverse=True)

    total = len(computed)
    limited = computed[: max(1, min(limit, MAX_GROUP_ROWS))]
    return {"groups": limited, "group_count": total, "returned": len(limited)}


def _round(value: object) -> object:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 6)
    return value


def describe(table: Table, column: str | None) -> dict:
    if column is not None:
        name = table.column(column)
        return {name: _describe_column(table, name)}
    return {name: _describe_column(table, name) for name in table.headers}


def _describe_column(table: Table, name: str) -> dict:
    kind = table.kind(name)
    values = table.values(name)
    missing = len(table.rows) - len(values)
    info: dict = {"type": kind, "non_empty": len(values), "missing": missing}
    if kind == "number":
        numbers = [float(v) for v in values]
        if numbers:
            info.update(
                {
                    "unique": len(set(numbers)),
                    "min": _round(min(numbers)),
                    "max": _round(max(numbers)),
                    "mean": _round(statistics.fmean(numbers)),
                    "median": _round(statistics.median(numbers)),
                    "std": _round(statistics.stdev(numbers)) if len(numbers) >= 2 else None,
                    "p25": _round(statistics.quantiles(numbers, n=4)[0]) if len(numbers) >= 4 else None,
                    "p75": _round(statistics.quantiles(numbers, n=4)[2]) if len(numbers) >= 4 else None,
                }
            )
    elif kind == "date":
        if values:
            info.update({"earliest": min(values).isoformat(sep=" "), "latest": max(values).isoformat(sep=" ")})
    elif kind == "bool":
        info.update(
            {
                "true": sum(1 for v in values if v is True),
                "false": sum(1 for v in values if v is False),
            }
        )
    else:
        counts: dict[str, int] = {}
        for v in values:
            key = str(v)
            counts[key] = counts.get(key, 0) + 1
        info["unique"] = len(counts)
        info["top_values"] = sorted(counts.items(), key=lambda kv: -kv[1])[:TOP_VALUES]
    return info


def preview(table: Table, limit: int) -> list[dict]:
    return [
        {h: _format_value(row[h]) for h in table.headers}
        for row in table.rows[: max(1, min(limit, MAX_FILTER_ROWS))]
    ]
