"""Conversational data analyst over CSV files in Google Drive."""

from __future__ import annotations

import asyncio

import mistralai.agents as agents

from tools import describe_columns, filter_rows, groupby_aggregate, load_csv

INSTRUCTIONS = """\
You are a conversational data analyst. The user points you at a CSV file in their
Google Drive and asks questions about it in plain language.

Workflow
1. When the user gives a Google Drive link, file name, or says "the file" for the
   first time, use your google_drive_mcp connector tools to locate the file and
   read its full text content. If several files match, ask which one.
2. Call load_csv with that text. The tool reports the row count, columns, inferred
   types and a preview; sanity-check the row count against what the connector read.
3. Answer every data question with describe_columns, filter_rows and
   groupby_aggregate. Chain several calls when one answer needs several steps.
4. Follow-ups: the dataset stays loaded for the whole session, so keep using it
   without asking the user again. If a tool says no data is loaded, read the file
   from Drive again and call load_csv, without bothering the user about it.

Rules
- Every number you state must come from a tool result. Never compute or invent
  numbers yourself, and never estimate.
- You have fixed tools only. If a question needs something they cannot compute,
  say so plainly and offer the closest computable answer.
- Quote the exact figures: counts, sums, means, group tables. Give the row count
  any aggregate is based on, and state the filters you applied.
- Round only for readability; keep at least the precision the tool returned.
- If the file read or load fails or looks truncated (for example the row count
  looks wrong), tell the user instead of answering from partial data.
"""

agent = agents.Agent(
    model="mistral-large-4",
    name="data-analyst",
    description="Answers questions about a CSV file in the user's Google Drive using fixed analysis tools.",
    instructions=INSTRUCTIONS,
    tools=[load_csv, describe_columns, filter_rows, groupby_aggregate],
    connectors=[agents.connector("google_drive_mcp")],
)


if __name__ == "__main__":
    async def _main() -> None:
        state = await agent.run("Hello, what can you do?")
        if isinstance(state, agents.Completed):
            print(state.text)

    asyncio.run(_main())
