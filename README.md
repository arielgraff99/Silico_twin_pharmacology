# data-analyst

A conversational data analyst built with the [Mistral Agents SDK](https://docs.mistral.ai/studio/agents/overview).
You give it a link to a CSV file in your Google Drive and ask questions in plain
language; it answers with real numbers from your data.

- **Fixed tools only, no code execution.** The agent has exactly four tools and
  no sandbox (`NoSandbox`), so it cannot run shell commands or arbitrary code:
  - `load_csv` — loads the CSV text fetched from Drive into the session's dataset
  - `describe_columns` — types, missing values, summary statistics per column
  - `filter_rows` — fixed filter operators (eq, gt, contains, between, in, …),
    returns matching row count plus sample rows
  - `groupby_aggregate` — group by 1–3 columns and compute count, sum, mean,
    median, min, max, nunique, std per group, with optional pre-filters and ordering
- **Your Google Drive connection.** The agent mounts the `google_drive_mcp`
  Studio connector and acts as the user who runs the session (on-behalf-of, so
  the deployment is hardened).
- **Conversation memory.** Deployed, sessions are stored by Mistral: follow-up
  questions see the whole conversation, and the loaded dataset stays attached to
  the session. If the worker restarts, the agent re-reads the file from Drive
  and reloads it without asking you again.

## Project layout

| File | Purpose |
| --- | --- |
| `main.py` | The `agent` (model, instructions, tools, connector); entrypoint `main:agent` |
| `tools.py` | The four fixed tools (`@agents.tool`, `model_access="direct"`, read-only) |
| `engines.py` | Pure-Python CSV engine: parsing, type inference, filtering, aggregation |
| `tests/test_engines.py` | Unit tests for the engine |
| `Dockerfile` | Built by `agents deploy` from the pushed commit |

## Local development

```bash
uv sync
uv run pytest          # engine unit tests
uv run agents info     # shows the agent surface
MISTRAL_API_KEY=... uv run python main.py   # one-shot local run
MISTRAL_API_KEY=... uv run agents chat    # interactive local chat
```

## Deploy to Mistral Cloud (managed)

Prerequisites:

1. **GitHub connector** connected on your account in
   [Mistral Studio → Connectors](https://console.mistral.ai/build/connectors):
   `agents deploy` clones the project from GitHub.
2. **Google Drive connector** connected in the same place: the deployed agent
   reads your Drive with it.
3. A `MISTRAL_API_KEY` whose user has a workspace role with Workflows access
   (Developer or Workspace Contributor; assigned in the
   [admin console](https://admin.mistral.ai)).

Then:

```bash
git remote add origin <your-repository-url>
git push -u origin HEAD
uv run --env-file .env agents deploy
```

`.env` holds `MISTRAL_API_KEY=...`. The deploy builds this `Dockerfile` from the
pushed commit, registers the agent as `data-analyst`, and prints a
**"Try the agent in Vibe Code Web"** link of the form
`https://chat.mistral.ai/managed-agents/data-analyst`.

Because the agent uses a connector as the session user, `agents deploy` deploys
it **hardened** by default; keep that (do not pass `--no-hardened`).

## Using the agent

- **In Vibe Code Web**: open the link printed by the deploy (or find the agent
  under Managed Agents in [Mistral Studio](https://console.mistral.ai/build/managed-agents/sessions))
  and chat: paste a Drive link, then ask follow-ups in the same conversation.
- **From the CLI**: `uv run --env-file .env agents chat data-analyst`
- **From your app**:
  ```python
  import asyncio
  from mistralai import agents

  async def main():
      agent = agents.Agent.load_from(agent_name="data-analyst")
      session = await agent.start_session()
      try:
          first = await session.run("Analyse https://drive.google.com/... my sales CSV")
          print(first.text)
          second = await session.run("And per region?")  # same session, same data
          print(second.text)
      finally:
          await session.cancel()

  asyncio.run(main())
  ```

Sessions, traces (one span per model call and tool call) and logs are visible in
Mistral Studio; read logs with `uv run --env-file .env agents logs data-analyst --follow`.

## Notes and limits

- The CSV text travels through the Drive connector's read tool into the model
  context before `load_csv` parses it, so very large files may need exporting or
  trimming; the agent sanity-checks the loaded row count and tells you if the
  read looked truncated.
- The dataset lives in the worker's memory for the session (keyed by session id);
  it is never written to disk and never leaves the deployment.
