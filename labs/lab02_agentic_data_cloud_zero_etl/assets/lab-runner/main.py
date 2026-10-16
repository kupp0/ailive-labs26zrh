"""Lab 11 console: one screen per browser step of user_guide.md.

user_guide.md is the script; this app is the instrument panel. Each screen's
number and title match a guide heading exactly (tests/test_lab_console_contract.py
enforces it), buttons call fixed server-side actions, and results come back as
HTML fragments swapped in by static/console.js.

Runs as lab11-runner-sa, which holds no write role on AlloyDB or Spanner. Every
write in this lab is performed by the transactional service, reached only
through the dispatch agent.
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
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.api_core import exceptions as gexc

import actions
import clients as c
from validation import allowed_engines, parse_write

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("console")

HERE = Path(__file__).resolve().parent
LAB_NUMBER = 11


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
    Screen(6, "Deploy the 3 Multi-Identity Services to Cloud Run", "BOTH"),
    Screen(7, "Live Query Resolution Across 3 Engines"),
    Screen(8, "Execute Cross-Identity Transactional Writes"),
    Screen(9, "Benchmark Zero-ETL Federation Latency & Overhead"),
    Screen(10, "Operational (Spanner) vs. Analytical (BigQuery) Graph Engines"),
    Screen(11, "Stress-Test Security & Multi-Layer IAM Boundaries", "BOTH"),
    Screen(12, "Re-verify Knowledge Catalog Grounding", "BOTH"),
    Screen(13, "Re-verify PII Column Masking"),
]
# TERMINAL_STEPS: shown greyed in the route so the participant sees where the
# console sits in the guide. No pages.
TERMINAL_STEPS: list[Screen] = [
    Screen(1, "Bootstrap & Verify Day-0 Multi-Engine Infrastructure", "TERMINAL"),
    Screen(2, "Query Live Zero-ETL Federation Across 3 Engines", "TERMINAL"),
    Screen(3, "Implement the 7-Way Intent Router (TODO_A)", "TERMINAL"),
    Screen(4, "Wire the Cross-Service Dispatch Table (TODO_B)", "TERMINAL"),
    Screen(5, "Implement 2-Phase Spanner Transactional Writes (TODO_C)", "TERMINAL"),
]
SCREEN_BY_STEP = {s.step: s for s in SCREENS}
ROUTE: list[Screen] = sorted(SCREENS + TERMINAL_STEPS, key=lambda s: s.step)
LAST_GUIDE_STEP = ROUTE[-1].step

app = FastAPI(title="Zero-ETL Federation console", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
# Cache-busting: static URLs carry a hash of the files, so a redeploy never
# pairs new HTML with a browser-cached stylesheet or script from before it.
templates.env.globals["ASSET_V"] = hashlib.sha256(
    b"".join(f.read_bytes() for f in sorted((HERE / "static").iterdir()) if f.is_file())
).hexdigest()[:10]
# Info tips for every button, readable from imported macros (macros.html).
templates.env.globals["TIPS"] = actions.TIPS


def _labs() -> list[dict[str, Any]]:
    """Returns the lab switcher entries: Lab 10 if deployed, this lab, Lab 12 if deployed."""
    return [
        {
            "num": 10,
            "title": "From Data to Agent",
            "url": c.LAB10_CONSOLE_URL,
            "current": False,
            "hint": "Deploy it: From Data to Agent guide, Step 6",
        },
        {"num": 11, "title": "Zero-ETL Federation", "url": "/", "current": True, "hint": ""},
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
            ("This console", c.RUNNER_SA.split("@")[0], "reads, never writes"),
            ("Dispatch agent", c.DISPATCH_SA.split("@")[0], "no write role"),
            ("Transactional agent", c.TRANSACT_SA.split("@")[0], "writes AlloyDB + Spanner, no BigQuery"),
        ],
        "agent_url": c.AGENT_URL,
        "transact_url": c.TRANSACT_URL,
        "alloydb_enabled": c.ALLOYDB_ENABLED,
        "engines_chips": [
            ("BigQuery", "#4285F4", True),
            ("Spanner", "#EA4335", True),
            ("AlloyDB", "#34A853", c.ALLOYDB_ENABLED),
        ],
        "engines": allowed_engines(),
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
            f"FROM `{c.PROJECT_ID}.{c.SOURCE_DATASET}.orders` WHERE status = 'Complete'"
        ).first("revenue")
        label = f"${float(revenue):,.2f}"
    except (gexc.GoogleAPIError, TypeError, ValueError) as exc:
        log.warning("reference: governed revenue unavailable: %s", exc)
        label = "your project's governed revenue"
    return HTMLResponse(page.replace("__GOVERNED_REVENUE__", label))


@app.post("/api/{slug}", response_class=HTMLResponse)
async def run_action(request: Request, slug: str) -> HTMLResponse:
    """Runs one fixed action and returns its result fragment.

    Only `s8-write` (four fields, validation.parse_write) and the slugs in
    actions.BODY_ACTIONS (Step 10's seed customer, graph_story.validate_customer_id)
    read a request body. Actions block on BigQuery and agent calls for up to
    minutes, so they run in the threadpool: one slow agent answer must not stall
    every other request.
    """
    try:
        if slug == "s8-write":
            body = await request.json()
            req = await run_in_threadpool(
                parse_write,
                str(body.get("engine", "")),
                str(body.get("customer_id", "")),
                str(body.get("product_id", "")),
                str(body.get("amount", "")),
            )
            blocks = await run_in_threadpool(actions.s8_write, req)
        elif slug in actions.BODY_ACTIONS:
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
