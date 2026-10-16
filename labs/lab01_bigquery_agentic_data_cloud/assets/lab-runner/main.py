"""Lab 10 console: one screen per browser step of user_guide.md.

user_guide.md is the script; this app is the instrument panel. Each screen's
number and title match a guide heading exactly (tests/test_lab_console_contract.py
enforces it), buttons call fixed server-side actions, and results come back as
HTML fragments swapped in by static/console.js.

Runs as lab-runner-sa (Fine-Grained Reader). The agent it calls runs as
ecommerce-analyst-agent (Masked Reader). That split is the whole point of
Step 8 and is a property of the deployment, not of this code.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from google.api_core import exceptions as gexc

import actions
import clients as c
import graph_story

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("console")

HERE = Path(__file__).resolve().parent
LAB_NUMBER = 10


@dataclass(frozen=True)
class Screen:
    """A user_guide.md step, as the console lists it.

    Attributes:
        step: Guide step number.
        title: Guide step title, verbatim.
        where: The step's guide badge: TERMINAL, CONSOLE or BOTH.
    """

    step: int
    title: str
    where: str = "CONSOLE"


# The contract with user_guide.md. Change a title here and in the guide together.
# SCREENS: CONSOLE/BOTH steps, one page each.
SCREENS: list[Screen] = [
    Screen(6, "Deploy the Agent and the Lab Console", "BOTH"),
    Screen(7, "The Glossary Is the Control Surface", "BOTH"),
    Screen(8, "Column-Level Security and PII Masking"),
    Screen(9, "Relationships, Not Rows with BigQuery Property Graphs", "BOTH"),
]
# TERMINAL_STEPS: shown greyed in the route so the participant sees where the
# console sits in the guide. No pages.
TERMINAL_STEPS: list[Screen] = [
    Screen(1, "Bootstrap and Verify the Pre-provisioned Infrastructure", "TERMINAL"),
    Screen(2, "The Business Problem in Two Numbers", "TERMINAL"),
    Screen(3, "Inspect the Governed Glossary with agy", "TERMINAL"),
    Screen(4, "Connect the Two MCP Toolsets (TODO_2)", "TERMINAL"),
    Screen(5, "Write the Catalog-First Instruction (TODO_3)", "TERMINAL"),
    Screen(10, "Advanced Challenges", "TERMINAL"),
]
SCREEN_BY_STEP = {s.step: s for s in SCREENS}
ROUTE: list[Screen] = sorted(SCREENS + TERMINAL_STEPS, key=lambda s: s.step)
LAST_GUIDE_STEP = ROUTE[-1].step

app = FastAPI(title="From Data to Agent console", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
# Cache-busting: static URLs carry a hash of the files, so a redeploy never
# pairs new HTML with a browser-cached stylesheet or script from before it.
templates.env.globals["ASSET_V"] = hashlib.sha256(
    b"".join(f.read_bytes() for f in sorted((HERE / "static").iterdir()) if f.is_file())
).hexdigest()[:10]
# Info tips for every button, readable from imported macros (macros.html).
templates.env.globals["TIPS"] = actions.TIPS
# Step 9's schema mini-graph (three node labels, two edge labels).
templates.env.globals["SCHEMA_GRAPH"] = graph_story.schema_graph()


def _labs() -> list[dict[str, Any]]:
    """Returns the lab switcher entries: this lab, then Lab 11 and Lab 12 if deployed."""
    return [
        {"num": 10, "title": "From Data to Agent", "url": "/", "current": True, "hint": ""},
        {
            "num": 11,
            "title": "Zero-ETL Federation",
            "url": c.LAB11_CONSOLE_URL,
            "current": False,
            "hint": "Deploy it: Zero-ETL Federation guide, Step 6",
        },
        {
            "num": 12,
            "title": "Publish to Gemini Enterprise",
            "url": c.GE_PUBLISHER_URL,
            "current": False,
            "hint": "Deploy it: Publish to Gemini Enterprise guide, Step 3",
        },
    ]


def _next_step(step: int) -> Screen | None:
    """Returns the guide step that follows `step`, console or terminal."""
    later = [s for s in ROUTE if s.step > step]
    return later[0] if later else None


def _context(request: Request, **extra: Any) -> dict[str, Any]:
    """Returns the template context shared by every page."""
    return {
        "request": request,
        "lab_number": LAB_NUMBER,
        "labs": _labs(),
        "route": ROUTE,
        "screen_steps": set(SCREEN_BY_STEP),
        "last_step": LAST_GUIDE_STEP,
        "project": c.PROJECT_ID or "(unset)",
        "identities": [
            ("This console", c.RUNNER_SA.split("@")[0], "Fine-Grained Reader · sees real email"),
            ("The agent", c.AGENT_SA.split("@")[0], "Masked Reader · sees XXXXX@ · no write role"),
        ],
        "engines_chips": [("BigQuery", "#4285F4", True), ("Knowledge Catalog", "#FBBC04", True)],
        "agent_url": c.AGENT_URL,
        **extra,
    }


@app.get("/")
def index() -> RedirectResponse:
    """Opens the first console screen."""
    return RedirectResponse(url=f"/step/{SCREENS[0].step}")


@app.get("/step/{step}", response_class=HTMLResponse)
def step_page(request: Request, step: int) -> HTMLResponse:
    """Renders one step screen."""
    screen = SCREEN_BY_STEP.get(step)
    if screen is None:
        raise HTTPException(status_code=404, detail="No console screen for this step")
    return templates.TemplateResponse(
        request, f"step_{step}.html", _context(request, screen=screen, next_step=_next_step(step))
    )


@app.get("/reference", response_class=HTMLResponse)
def reference() -> HTMLResponse:
    """Serves the architecture diagrams, with this project's governed revenue filled in."""
    page = (HERE / "diagrams.html").read_text(encoding="utf-8")
    try:
        revenue = c.run_query(
            f"SELECT ROUND(SUM(total_amount), 2) AS revenue "
            f"FROM `{c.PROJECT_ID}.{c.DATASET}.orders` WHERE status = 'Complete'"
        ).first("revenue")
        label = f"${float(revenue):,.2f}"
    except (gexc.GoogleAPIError, TypeError, ValueError) as exc:
        log.warning("reference: governed revenue unavailable: %s", exc)
        label = "your project's governed revenue"
    return HTMLResponse(page.replace("__GOVERNED_REVENUE__", label))


@app.post("/api/{slug}", response_class=HTMLResponse)
async def run_action(request: Request, slug: str) -> HTMLResponse:
    """Runs one fixed action and returns its result fragment.

    Only slugs in actions.BODY_ACTIONS read a request body, and each validates
    every field before use. Actions block on BigQuery and agent calls, so they
    run in the threadpool: one slow answer must not stall every other request.
    """
    try:
        if slug in actions.BODY_ACTIONS:
            try:
                body = await request.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            blocks = await run_in_threadpool(actions.BODY_ACTIONS[slug], body)
        elif slug in actions.ACTIONS:
            blocks = await run_in_threadpool(actions.ACTIONS[slug])
        else:
            raise HTTPException(status_code=404, detail="Unknown action")
        log.info("action=%s ok", slug)
    except HTTPException:
        raise
    except ValueError as exc:
        log.info("action=%s rejected: %s", slug, exc)
        blocks = [actions.status("error", f"Rejected: {exc}")]
    except (gexc.GoogleAPIError, requests.RequestException) as exc:
        log.warning("action=%s failed: %s", slug, exc)
        blocks = [actions.status("error", f"{type(exc).__name__}: {exc}")]
    return templates.TemplateResponse(request, "partials/result.html", {"blocks": blocks})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
