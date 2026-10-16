"""Lab 12 publisher console: one button that puts the Lab 10 & 11 agents in Gemini Enterprise.

The participant opens this page, optionally names business users, and presses
"Publish ADK Agents to Gemini Enterprise". ge.publish() runs every step and the
page renders the checklist and the link to hand out.

Endpoints:
  GET  /              the page
  GET  /api/status    read-only snapshot (what is deployed / registered)
  POST /api/publish   run the publish; body {"users": "a@x.com, b@x.com"}
  POST /api/test      one A2A round trip to an agent; body {"service", "question"}
  GET  /healthz       liveness

Runs as ge-publisher-sa. Nothing from the browser reaches an API except the
validated email list and a question chosen from a fixed list.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

import ge

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("publisher")

HERE = Path(__file__).resolve().parent

# The only questions the "Try it" buttons may send. Fixed text, like every other
# lab console: nothing typed in the browser becomes a prompt.
TEST_QUESTIONS: dict[str, list[str]] = {
    ge.AGENTS[0]["service"]: ["What is our revenue?", "Who are our top 5 high-value customers?"],
    ge.AGENTS[1]["service"]: [
        "What is our revenue by product category?",
        "Insert a new order for customer 42.",
    ],
}

app = FastAPI(title="Gemini Enterprise publisher", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def _labs() -> list[dict[str, Any]]:
    """Lab switcher entries, same shape as the Lab 10/11 consoles' tabs."""
    return [
        {"num": 10, "title": "From Data to Agent", "url": os.getenv("LAB10_CONSOLE_URL", "").rstrip("/"),
         "current": False, "hint": "Deploy it: From Data to Agent guide, Step 6"},
        {"num": 11, "title": "Zero-ETL Federation", "url": os.getenv("LAB11_CONSOLE_URL", "").rstrip("/"),
         "current": False, "hint": "Deploy it: Zero-ETL Federation guide, Step 6"},
        {"num": 12, "title": "Publish to Gemini Enterprise", "url": "/", "current": True, "hint": ""},
    ]


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Renders the publisher page."""
    return templates.TemplateResponse(request, "index.html", {
        "project": ge.PROJECT_ID or "(unset)",
        "region": ge.REGION,
        "participant": ge.PARTICIPANT_EMAIL,
        "agents": ge.AGENTS,
        "questions": TEST_QUESTIONS,
        "engine_display_name": ge.ENGINE_DISPLAY_NAME,
        "labs": _labs(),
    })


@app.get("/api/status")
async def api_status() -> JSONResponse:
    """Returns what is deployed and registered right now."""
    return JSONResponse(await run_in_threadpool(ge.status))


@app.post("/api/publish")
async def api_publish(request: Request) -> JSONResponse:
    """Runs the full publish and returns the checklist."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    try:
        users = ge.parse_users((body or {}).get("users", ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "steps": []}, status_code=400)
    report = await run_in_threadpool(ge.publish, users)
    log.info("publish ok=%s agents=%d", report.ok, len(report.agents))
    return JSONResponse(report.as_dict())


@app.post("/api/test")
async def api_test(request: Request) -> JSONResponse:
    """Sends one fixed question to one agent over A2A."""
    try:
        body: dict[str, Any] = await request.json()
    except ValueError:
        body = {}
    service = str(body.get("service", ""))
    question = str(body.get("question", ""))
    if question not in TEST_QUESTIONS.get(service, []):
        raise HTTPException(status_code=400, detail="unknown service or question")
    try:
        return JSONResponse(await run_in_threadpool(ge.test_agent, service, question))
    except ge.PublishError as exc:
        return JSONResponse({"service": service, "question": question, "error": str(exc)}, status_code=502)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
