"""Fixed analysis tools for the data-analyst agent.

The model can only call these functions; it cannot run arbitrary code.
Each tool operates on the dataset loaded in the current session.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field

import mistralai.agents as agents

import engines

_READONLY = agents.ToolAnnotations(read_only_hint=True)

_TABLES: dict[str, engines.Table] = {}


def _table_for(ctx: agents.ToolCallbackContext) -> engines.Table:
    table = _TABLES.get(ctx.session.id)
    if table is None:
        raise agents.ToolError(
            "No CSV is loaded in this session yet. Read the file with the google_drive_mcp "
            "connector tools, then call load_csv with its text content."
        )
    return table


class FilterCondition(BaseModel):
    column: str = Field(description="Column name to filter on.")
    op: str = Field(
        description=(
            "One of: eq, ne, gt, gte, lt, lte, contains, startswith, endswith, "
            "in, not_in, between, is_null, not_null."
        )
    )
    value: Any = Field(default=None, description="Comparison value; a list for in/not_in.")
    value2: Any = Field(default=None, description="Second bound, only for the between operator.")


class Metric(BaseModel):
    column: str | None = Field(
        default=None,
        description="Column to aggregate; omit only for function 'count'.",
    )
    function: str = Field(
        description="One of: count, sum, mean, median, min, max, nunique, std."
    )
    alias: str | None = Field(default=None, description="Name of the result column.")


@agents.tool(model_access="direct", annotations=_READONLY)
async def load_csv(
    ctx: agents.ToolCallbackContext,
    /,
    csv_text: Annotated[str, Field(description="The full text content of the CSV file.")],
    file_name: Annotated[str, Field(description="The file's name, for reference in answers.")] = "data.csv",
) -> dict:
    """Load a CSV file's content as the session's dataset and summarize its structure."""
    try:
        table = engines.Table.from_csv_text(csv_text, file_name)
    except engines.AnalysisError as exc:
        raise agents.ToolError(str(exc)) from exc
    _TABLES[ctx.session.id] = table
    columns = [
        {"name": name, "type": table.kind(name), "missing": sum(1 for r in table.rows if r[name] is None)}
        for name in table.headers
    ]
    return {
        "file": file_name,
        "rows": len(table.rows),
        "columns": columns,
        "preview": engines.preview(table, 5),
    }


@agents.tool(model_access="direct", annotations=_READONLY)
async def describe_columns(
    ctx: agents.ToolCallbackContext,
    /,
    column: Annotated[
        str | None,
        Field(description="One column for detailed stats, or omit for a summary of every column."),
    ] = None,
) -> dict:
    """Describe the loaded dataset's columns: types, missing values, and summary statistics."""
    table = _table_for(ctx)
    try:
        return engines.describe(table, column)
    except engines.AnalysisError as exc:
        raise agents.ToolError(str(exc)) from exc


@agents.tool(model_access="direct", annotations=_READONLY)
async def filter_rows(
    ctx: agents.ToolCallbackContext,
    /,
    filters: Annotated[list[FilterCondition], Field(description="All conditions must match (AND).")],
    limit: Annotated[int, Field(description="Max rows to return (1-200).")] = 20,
) -> dict:
    """Filter rows with fixed conditions and return the count plus sample matching rows."""
    table = _table_for(ctx)
    try:
        matching = engines.apply_filters(table, [f.model_dump() for f in filters])
    except engines.AnalysisError as exc:
        raise agents.ToolError(str(exc)) from exc
    limit = max(1, min(limit, engines.MAX_FILTER_ROWS))
    return {
        "matching_rows": len(matching),
        "returned": min(len(matching), limit),
        "rows": [
            {h: engines._format_value(row[h]) for h in table.headers}
            for row in matching[:limit]
        ],
    }


@agents.tool(model_access="direct", annotations=_READONLY)
async def groupby_aggregate(
    ctx: agents.ToolCallbackContext,
    /,
    group_by: Annotated[list[str], Field(description="Columns to group by (1-3).")],
    metrics: Annotated[list[Metric], Field(description="Aggregations to compute per group.")],
    filters: Annotated[
        list[FilterCondition] | None, Field(description="Optional filters applied before grouping.")
    ] = None,
    order_by: Annotated[
        str | None, Field(description="Group column or metric alias to sort by.")
    ] = None,
    order_desc: Annotated[bool, Field(description="Sort descending.")] = True,
    limit: Annotated[int, Field(description="Max groups to return (1-500).")] = 50,
) -> dict:
    """Group the rows and compute fixed aggregates (count, sum, mean, median, min, max, nunique, std) per group."""
    table = _table_for(ctx)
    if not 1 <= len(group_by) <= 3:
        raise agents.ToolError("Group by 1 to 3 columns.")
    try:
        return engines.group_and_aggregate(
            table,
            group_by,
            [m.model_dump() for m in metrics],
            [f.model_dump() for f in filters] if filters else None,
            order_by,
            order_desc,
            limit,
        )
    except engines.AnalysisError as exc:
        raise agents.ToolError(str(exc)) from exc
