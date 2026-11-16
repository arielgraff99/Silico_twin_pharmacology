"""Local web app for the data-analyst agent.

Serves a validated form (patient + medications + observations) that replaces
hand-written CSV files. On submit, the form data is serialized into a clean
CSV and sent to the deployed Mistral agent ("data-analyst"), which loads it
with its fixed tools. Follow-up questions go to the same conversation, so the
agent keeps full memory of the data and everything discussed.

Run from the repository root:
    uv run --extra webapp --env-file .env uvicorn webapp.server:app --port 8000
Then open http://127.0.0.1:8000

The Mistral API key is read from the environment (.env) - it never reaches the
browser. The deployed agent is not modified by this app.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
import mistralai.agents as agents
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

AGENT_NAME = "data-analyst"
STATIC_DIR = Path(__file__).parent / "static"

CSV_COLUMNS = [
    "patient_id", "age_years", "sex", "weight_kg", "height_cm",
    "renal_function_egfr", "pregnant",
    "medication", "dose", "dose_unit", "route", "frequency",
    "treatment_start", "duration_days",
    "sample_time_h", "measured_concentration", "concentration_unit",
    "adverse_event", "adherence_pct",
]

app = FastAPI(title="data-analyst form")

_agent: agents.Agent | None = None
_sessions: dict[str, dict[str, Any]] = {}


def _agent_client() -> agents.Agent:
    global _agent
    if _agent is None:
        _agent = agents.Agent.load_from(agent_name=AGENT_NAME)
    return _agent


# --------------------------------------------------------------------------- #
# Form model
# --------------------------------------------------------------------------- #

class MedicationRow(BaseModel):
    medication: str = Field(min_length=1)
    dose: float = Field(gt=0)
    dose_unit: Literal["mg", "µg", "g", "IU", "mL", "mg/kg", "µg/h"] = "mg"
    route: Literal[
        "oral", "IV", "IM", "SC", "sublingual", "transdermal",
        "rectal", "inhaled",
    ] = "oral"
    frequency: Literal[
        "once daily", "q4h", "q6h", "q8h", "q12h", "TID", "BID",
        "weekly", "PRN",
    ] = "once daily"
    treatment_start: str | None = Field(default=None, description="YYYY-MM-DD")
    duration_days: float | None = Field(default=None, ge=0)


class Observation(BaseModel):
    sample_time_h: float | None = Field(default=None, ge=0)
    measured_concentration: float | None = Field(default=None, gt=0)
    concentration_unit: Literal["µg/L", "mg/L", "ng/mL"] | None = None
    adverse_event: str | None = None
    adherence_pct: float | None = Field(default=None, ge=0, le=100)


class FormData(BaseModel):
    patient_id: str = Field(min_length=1)
    age_years: float = Field(ge=0, le=120)
    sex: Literal["male", "female", "other"] = "female"
    weight_kg: float = Field(gt=0, le=350)
    height_cm: float | None = Field(default=None, ge=30, le=250)
    renal_function_egfr: float | None = Field(default=None, ge=0, le=200)
    pregnant: Literal["yes", "no", "unknown"] = "unknown"
    medications: list[MedicationRow] = Field(min_length=1)
    observation: Observation | None = None


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def form_to_csv(data: FormData) -> str:
    """Serialize the validated form into one clean CSV dataset."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for i, med in enumerate(data.medications):
        row = {column: "" for column in CSV_COLUMNS}
        # Patient fields only on the first row; the agent sees one dataset.
        if i == 0:
            row.update({
                "patient_id": data.patient_id,
                "age_years": _fmt(data.age_years),
                "sex": data.sex,
                "weight_kg": _fmt(data.weight_kg),
                "height_cm": _fmt(data.height_cm),
                "renal_function_egfr": _fmt(data.renal_function_egfr),
                "pregnant": data.pregnant,
            })
        row.update({
            "medication": med.medication,
            "dose": _fmt(med.dose),
            "dose_unit": med.dose_unit,
            "route": med.route,
            "frequency": med.frequency,
            "treatment_start": med.treatment_start or "",
            "duration_days": _fmt(med.duration_days),
        })
        if data.observation:
            row.update({
                "sample_time_h": _fmt(data.observation.sample_time_h),
                "measured_concentration": _fmt(data.observation.measured_concentration),
                "concentration_unit": data.observation.concentration_unit or "",
                "adverse_event": data.observation.adverse_event or "",
                "adherence_pct": _fmt(data.observation.adherence_pct),
            })
        writer.writerow([row[c] for c in CSV_COLUMNS])
    return buffer.getvalue().strip()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/medications.json")
async def medications() -> FileResponse:
    return FileResponse(STATIC_DIR / "medications.json")


@app.post("/api/session")
async def create_session(data: FormData) -> dict:
    """Validate the form, serialize it to CSV, and start an agent conversation."""
    csv_text = form_to_csv(data)
    message = (
        "Here is a new patient dataset as CSV (first row is the header, one row "
        "per medication):\n\n"
        f"{csv_text}\n\n"
        "Load it with load_csv, then confirm the row count, the columns with "
        "their types, and summarize the medication list (drug, dose, unit, "
        "route, frequency)."
    )
    session = await _agent_client().start_session()
    session_id = uuid.uuid4().hex
    _sessions[session_id] = {"session": session, "lock": asyncio.Lock()}
    try:
        state = await session.run(message)
    except Exception as exc:
        await session.cancel()
        _sessions.pop(session_id, None)
        raise HTTPException(status_code=502, detail=f"Agent failed: {exc}") from exc
    if not isinstance(state, agents.Completed):
        await session.cancel()
        _sessions.pop(session_id, None)
        raise HTTPException(status_code=502, detail=f"Agent ended in state: {state}")
    return {"session_id": session_id, "csv": csv_text, "reply": state.text}


@app.post("/api/chat")
async def chat(session_id: str, message: str) -> dict:
    """Send a follow-up question to the same conversation (full memory)."""
    entry = _sessions.get(session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown session. Submit the form again.")
    async with entry["lock"]:
        try:
            state = await entry["session"].run(message)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Agent failed: {exc}") from exc
    if not isinstance(state, agents.Completed):
        raise HTTPException(status_code=502, detail=f"Agent ended in state: {state}")
    return {"reply": state.text}


@app.post("/api/reset")
async def reset(session_id: str) -> dict:
    """End the conversation. The next form submit starts a fresh one."""
    entry = _sessions.pop(session_id, None)
    if entry is not None:
        await entry["session"].cancel()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Medication list maintenance (web search)
# --------------------------------------------------------------------------- #

MEDICATIONS_FILE = STATIC_DIR / "medications.json"

UNITS = ("mg", "µg", "g", "IU", "mL", "mg/kg", "µg/h")
ROUTES = ("oral", "IV", "IM", "SC", "sublingual", "transdermal", "rectal", "inhaled")
FREQUENCIES = ("once daily", "q4h", "q6h", "q8h", "q12h", "TID", "BID", "weekly", "PRN")

_ROUTE_ALIASES = {
    "intravenous": "IV", "iv": "IV", "subcutaneous": "SC", "sc": "SC", "sq": "SC",
    "intramuscular": "IM", "im": "IM", "p.o.": "oral", "po": "oral", "per os": "oral",
    "inhalation": "inhaled", "inhaler": "inhaled", "topical": "transdermal",
}
_FREQ_ALIASES = {
    "od": "once daily", "daily": "once daily", "once a day": "once daily",
    "qd": "once daily", "bid": "BID", "twice daily": "BID", "twice a day": "BID",
    "tid": "TID", "three times daily": "TID", "qid": "q6h",
    "every 4 hours": "q4h", "every 6 hours": "q6h", "every 8 hours": "q8h",
    "every 12 hours": "q12h", "every 24 hours": "once daily",
}
_UNIT_ALIASES = {"mcg": "µg", "microgram": "µg", "micrograms": "µg", "milligram": "mg",
                 "milligrams": "mg", "iu": "IU", "units": "IU", "unit": "IU"}


def _normalize_med(raw: dict) -> dict | None:
    """Coerce one LLM/search result into the schema; None if unusable."""
    name = str(raw.get("name", "")).strip()
    try:
        dose = float(raw.get("dose"))
    except (TypeError, ValueError):
        return None
    if not name or not dose > 0:
        return None
    unit = str(raw.get("unit", "mg")).strip().lower()
    unit = _UNIT_ALIASES.get(unit, unit)
    if unit not in UNITS:
        unit = "mg"
    route = str(raw.get("route", "oral")).strip().lower()
    route = _ROUTE_ALIASES.get(route, route)
    if route not in ROUTES:
        route = "oral"
    frequency = str(raw.get("frequency", "once daily")).strip().lower()
    frequency = _FREQ_ALIASES.get(frequency, frequency)
    if frequency not in FREQUENCIES:
        frequency = "once daily"
    return {"name": name, "dose": dose, "unit": unit, "route": route, "frequency": frequency}


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return json.loads(text[start : end + 1])


class SearchRequest(BaseModel):
    query: str = Field(
        min_length=3,
        description="What to look up, e.g. 'antihypertensives' or 'WHO essential medicines 2025'.",
    )


@app.get("/api/medications")
async def get_medications() -> dict:
    data = json.loads(MEDICATIONS_FILE.read_text(encoding="utf-8"))
    return {"medications": data["medications"]}


@app.post("/api/medications/update")
async def update_medications(request: SearchRequest) -> dict:
    """Search the web via Mistral's hosted web_search tool and merge the results
    (name, typical adult dose, unit, route, frequency) into medications.json."""
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="MISTRAL_API_KEY is not set in the environment.")

    prompt = (
        f"Search the web for: {request.query}\n\n"
        "Based on current prescribing information (e.g. WHO Essential Medicines "
        "list, drug labels, BNF), list common medications with their typical ADULT "
        "maintenance dose per administration. Return ONLY a JSON object, no prose:\n"
        '{"medications": [{"name": "<generic name>", "dose": <number>, '
        '"unit": "mg|µg|g|IU|mL|mg/kg|µg/h", "route": "oral|IV|IM|SC|sublingual|'
        'transdermal|rectal|inhaled", "frequency": "once daily|q4h|q6h|q8h|q12h|TID|BID|weekly|PRN"}]}\n'
        "Return between 10 and 30 entries that best match the query."
    )
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                "https://api.mistral.ai/v1/conversations",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "mistral-large-4",
                    "inputs": prompt,
                    "tools": [{"type": "web_search"}],
                    "store": False,
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Mistral API unreachable: {exc}") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Mistral API error {response.status_code}: {response.text[:300]}")

    # Collect the text of the final assistant message (content is a chunk list).
    content = ""
    for entry in reversed(response.json().get("outputs", [])):
        if entry.get("type") != "message.output":
            continue
        chunks = entry.get("content") or []
        if isinstance(chunks, str):
            content = chunks
        else:
            content = "".join(c.get("text", "") for c in chunks if isinstance(c, dict) and c.get("type") == "text")
        if content.strip():
            break
    try:
        parsed = _extract_json(content)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not parse search result: {exc}") from exc

    existing = json.loads(MEDICATIONS_FILE.read_text(encoding="utf-8"))
    by_key = {m["name"].casefold(): m for m in existing["medications"]}
    added: list[dict] = []
    updated: list[dict] = []
    for raw in parsed.get("medications", []):
        med = _normalize_med(raw)
        if med is None:
            continue
        key = med["name"].casefold()
        if key in by_key:
            if by_key[key] != med:
                by_key[key] = med
                updated.append(med)
        else:
            by_key[key] = med
            added.append(med)

    merged = sorted(by_key.values(), key=lambda m: m["name"].casefold())
    backup = MEDICATIONS_FILE.with_suffix(".json.bak")
    backup.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    MEDICATIONS_FILE.write_text(
        json.dumps({"comment": existing.get("comment", ""), "medications": merged}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"added": added, "updated": updated, "total": len(merged), "medications": merged}
