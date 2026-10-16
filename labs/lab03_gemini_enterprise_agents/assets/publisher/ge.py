"""Gemini Enterprise publishing: every REST call the "Publish" button makes.

One function per step, each idempotent, so pressing the button twice converges
instead of duplicating anything. The order mirrors what an administrator would
click through in the Cloud console:

  1. provision        accept the Gemini Enterprise terms for this project
  2. identity         use Google identity (the project's own domain) for sign-in
  3. licence          reuse an active licence, else start the 30-day trial
  4. app              create the Gemini Enterprise app (engine), no data stores
  5. agents           for each deployed Lab 10/11 agent service:
                        a. grant run.invoker on THAT service to the Discovery
                           Engine service agent (never project-wide)
                        b. read its A2A agent card
                        c. register or update it in the app
  6. seats            assign licence seats to the participant and any business
                      users they named
  7. link             the web app URL to hand to business users

Runs as the console's own service account (ge-publisher-sa), with Application
Default Credentials. No user credential ever reaches this code.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import google.auth
import google.auth.transport.requests
import requests
from google.oauth2 import id_token

log = logging.getLogger("ge")

DE_HOST = "https://discoveryengine.googleapis.com"
DE_VER = "v1alpha"
RUN_HOST = "https://run.googleapis.com"
WEB_APP_BASE = "https://vertexaisearch.cloud.google.com/home/cid"

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "")
PROJECT_NUMBER = os.getenv("PROJECT_NUMBER", "")
REGION = os.getenv("REGION", "us-central1")
ENGINE_ID = os.getenv("GE_ENGINE_ID", "data-forge-agents")
ENGINE_DISPLAY_NAME = os.getenv("GE_ENGINE_DISPLAY_NAME", "Data Forge Agents")
COMPANY_NAME = os.getenv("GE_COMPANY_NAME", "Data Forge Lab")
PARTICIPANT_EMAIL = os.getenv("PARTICIPANT_EMAIL", "")
TRIAL_SEATS = os.getenv("GE_TRIAL_SEATS", "50")
DATA_USE_TERMS_VERSION = "2022-11-23"
# Who sees a registered agent. ALL_USERS = every user of THIS app (licensed in
# this project), not the organisation. Unshared agents are invisible to them.
AGENT_SHARING_SCOPE = "ALL_USERS"

# The two agents this lab publishes, by the Cloud Run service names their labs'
# init.sh write into .deploy.env. Lab 11 is optional: absent means "skipped".
AGENTS: list[dict[str, str]] = [
    {"lab": "Governed analyst", "guide": "From Data to Agent", "service": os.getenv("LAB10_AGENT_SERVICE", "ecommerce-agent")},
    {"lab": "Zero-ETL dispatch", "guide": "Zero-ETL Federation", "service": os.getenv("LAB11_AGENT_SERVICE", "lab11-agent")},
]

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_TIMEOUT = 60


class PublishError(RuntimeError):
    """A step failed in a way the participant can act on."""


@dataclass
class Step:
    """One line of the publish checklist shown in the console."""

    key: str
    title: str
    status: str = "pending"  # ok | warn | error | skipped | pending
    detail: str = ""


@dataclass
class Report:
    """Everything the console renders after a publish or status run."""

    steps: list[Step] = field(default_factory=list)
    web_app_url: str = ""
    agents: list[dict[str, Any]] = field(default_factory=list)
    licensed_users: list[str] = field(default_factory=list)

    def add(self, key: str, title: str, status: str, detail: str = "") -> Step:
        """Appends a step and returns it."""
        step = Step(key, title, status, detail)
        self.steps.append(step)
        return step

    @property
    def ok(self) -> bool:
        """True if no step failed."""
        return all(s.status != "error" for s in self.steps)

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable form for the API."""
        return {
            "ok": self.ok,
            "web_app_url": self.web_app_url,
            "agents": self.agents,
            "licensed_users": self.licensed_users,
            "steps": [s.__dict__ for s in self.steps],
        }


# ------------------------------------------------------------------------------
# HTTP plumbing
# ------------------------------------------------------------------------------
_credentials = None


def _token() -> str:
    """Returns a fresh OAuth access token for this service's identity."""
    global _credentials
    if _credentials is None:
        _credentials, _ = google.auth.default(scopes=_SCOPES)
    if not _credentials.valid:
        _credentials.refresh(google.auth.transport.requests.Request())
    return _credentials.token


def _call(method: str, url: str, body: dict[str, Any] | None = None, *, ok404: bool = False) -> dict[str, Any] | None:
    """Calls a Google REST API and returns the JSON body.

    Args:
        method: HTTP method.
        url: Full URL.
        body: Optional JSON body.
        ok404: Return None on 404 instead of raising.

    Returns:
        Parsed JSON (empty dict for an empty body), or None on an allowed 404.

    Raises:
        PublishError: On any other non-2xx response, with the API's message.
    """
    headers = {
        "Authorization": f"Bearer {_token()}",
        "X-Goog-User-Project": PROJECT_ID,
        "Content-Type": "application/json",
    }
    resp = requests.request(method, url, headers=headers, json=body, timeout=_TIMEOUT)
    if resp.status_code == 404 and ok404:
        return None
    if resp.status_code >= 400:
        try:
            message = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            message = resp.text
        raise PublishError(f"{method} {url.split('googleapis.com/')[-1]} → {resp.status_code}: {message[:400]}")
    return resp.json() if resp.content else {}


def _de(path: str) -> str:
    """Builds a Discovery Engine URL."""
    return f"{DE_HOST}/{DE_VER}/{path}"


def _wait(op: dict[str, Any], timeout_s: int = 180) -> dict[str, Any]:
    """Polls a Discovery Engine long-running operation until it is done."""
    deadline = time.time() + timeout_s
    while not op.get("done"):
        if time.time() > deadline:
            raise PublishError(f"operation {op.get('name')} still running after {timeout_s}s")
        time.sleep(3)
        op = _call("GET", _de(op["name"])) or {}
    if "error" in op:
        raise PublishError(f"operation failed: {op['error'].get('message', op['error'])}")
    return op.get("response", {})


def project_number() -> str:
    """Returns the project number (env, else the metadata server)."""
    global PROJECT_NUMBER
    if PROJECT_NUMBER:
        return PROJECT_NUMBER
    resp = requests.get(
        "http://metadata.google.internal/computeMetadata/v1/project/numeric-project-id",
        headers={"Metadata-Flavor": "Google"}, timeout=5,
    )
    resp.raise_for_status()
    PROJECT_NUMBER = resp.text.strip()
    return PROJECT_NUMBER


def service_agent() -> str:
    """The Discovery Engine service agent GE uses to call A2A agents."""
    return f"service-{project_number()}@gcp-sa-discoveryengine.iam.gserviceaccount.com"


def engine_name() -> str:
    """Full resource name of the lab's Gemini Enterprise app."""
    return f"projects/{PROJECT_ID}/locations/global/collections/default_collection/engines/{ENGINE_ID}"


def console_url() -> str:
    """Cloud console page of the Gemini Enterprise app, for administrators."""
    return f"https://console.cloud.google.com/gemini-enterprise/locations/global/engines/{ENGINE_ID}/overview?project={PROJECT_ID}"


# ------------------------------------------------------------------------------
# Steps
# ------------------------------------------------------------------------------
def provision() -> str:
    """Accepts the data-use terms, which onboards Gemini Enterprise in the project."""
    _wait(_call("POST", _de(f"projects/{project_number()}:provision"), {
        "acceptDataUseTerms": True,
        "dataUseTermsVersion": DATA_USE_TERMS_VERSION,
        "saasParams": {"acceptBizQos": True, "isBiz": False},
    }) or {})
    return "terms accepted"


def ensure_identity() -> str:
    """Selects Google identity for sign-in, unless an administrator chose another."""
    path = _de(f"projects/{PROJECT_ID}/locations/global/aclConfig")
    current = _call("GET", path, ok404=True) or {}
    idp = current.get("idpConfig", {}).get("idpType")
    if idp:
        return f"already set: {idp}"
    _call("PATCH", path, {"name": f"projects/{PROJECT_ID}/locations/global/aclConfig",
                          "idpConfig": {"idpType": "GSUITE"}})
    return "Google identity (GSUITE)"


def ensure_license() -> str:
    """Returns an active licence config, starting the free trial if there is none.

    Project-level purchase is deprecated for the ENTERPRISE tier; the free trial
    is created with SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT and a start date in
    the future (the API rejects today). The service names it free_trial_gemini.

    Returns:
        Full resource name of the licence config.
    """
    base = f"projects/{project_number()}/locations/global/licenseConfigs"
    configs = (_call("GET", _de(base)) or {}).get("licenseConfigs", [])
    active = [c for c in configs if c.get("state") in ("ACTIVE", "NOT_STARTED")]
    if active:
        return active[0]["name"]
    start = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    try:
        created = _call("POST", _de(f"{base}?licenseConfigId=ge-trial"), {
            "licenseCount": str(TRIAL_SEATS),
            "subscriptionTier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT",
            "subscriptionTerm": "SUBSCRIPTION_TERM_ONE_MONTH",
            "freeTrial": True,
            "autoRenew": False,
            "startDate": {"year": start.year, "month": start.month, "day": start.day},
        }) or {}
    except PublishError as exc:
        raise PublishError(
            f"Could not start the Gemini Enterprise trial automatically ({exc}). "
            f"Start it once in the console, then press Publish again: "
            f"https://console.cloud.google.com/gemini-enterprise?project={PROJECT_ID}"
        ) from exc
    return created["name"]


def ensure_app() -> str:
    """Creates the Gemini Enterprise app if it does not exist yet."""
    existing = _call("GET", _de(engine_name()), ok404=True)
    if existing:
        return "exists"
    op = _call(
        "POST",
        _de(f"projects/{PROJECT_ID}/locations/global/collections/default_collection/engines?engineId={ENGINE_ID}"),
        {
            "displayName": ENGINE_DISPLAY_NAME,
            "solutionType": "SOLUTION_TYPE_SEARCH",
            "industryVertical": "GENERIC",
            "appType": "APP_TYPE_INTRANET",
            "searchEngineConfig": {"searchTier": "SEARCH_TIER_ENTERPRISE", "searchAddOns": ["SEARCH_ADD_ON_LLM"]},
            "commonConfig": {"companyName": COMPANY_NAME},
        },
    ) or {}
    _wait(op)
    return "created"


def find_service(name: str) -> dict[str, Any] | None:
    """Returns a Cloud Run service (v2) in REGION, or None if it is not deployed."""
    return _call("GET", f"{RUN_HOST}/v2/projects/{PROJECT_ID}/locations/{REGION}/services/{name}", ok404=True)


def grant_invoker(service: str, members: list[str]) -> list[str]:
    """Adds run.invoker on ONE service for the given members (read-modify-write).

    Deliberately service-level. The GE docs suggest a project-level grant; that
    would let Gemini Enterprise invoke every service in the project, including
    Lab 11's transactional service, the only one allowed to write.

    Returns:
        The members that were newly added.
    """
    base = f"{RUN_HOST}/v2/projects/{PROJECT_ID}/locations/{REGION}/services/{service}"
    # IAM-EXCEPTION: the agent services are created by the Lab 10/11 deploy.sh at participant runtime; Terraform cannot reference them.
    policy = _call("GET", f"{base}:getIamPolicy") or {}
    bindings = policy.setdefault("bindings", [])
    binding = next((b for b in bindings if b.get("role") == "roles/run.invoker" and not b.get("condition")), None)
    if binding is None:
        binding = {"role": "roles/run.invoker", "members": []}
        bindings.append(binding)
    added = [m for m in members if m not in binding["members"]]
    if added:
        binding["members"].extend(added)
        # IAM-EXCEPTION: service-level run.invoker on a runtime-created Cloud Run service (see above).
        _call("POST", f"{base}:setIamPolicy", {"policy": policy})
    return added


def fetch_card(service_url: str, attempts: int = 6) -> dict[str, Any]:
    """Reads the agent's A2A card as this console (which holds invoker on it).

    IAM changes take a few seconds to reach Cloud Run, so a 403 right after the
    grant is retried before it is reported.
    """
    card_url = f"{service_url}/a2a/.well-known/agent-card.json"
    last = ""
    for i in range(attempts):
        token = id_token.fetch_id_token(google.auth.transport.requests.Request(), service_url)
        resp = requests.get(card_url, headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        last = f"{resp.status_code} {resp.text[:200]}"
        if resp.status_code == 404:
            raise PublishError(
                "this agent revision has no A2A surface (/a2a/ is 404). Redeploy it from its lab "
                "folder so it picks up a2a_surface.py:  bash deploy.sh --agent-only"
            )
        time.sleep(5 * (i + 1))
    raise PublishError(f"could not read the agent card at {card_url}: {last}")


def register_agent(card: dict[str, Any]) -> tuple[str, str]:
    """Registers (or updates) one A2A agent in the app; returns (name, action).

    The agent is shared with all users of THIS app (sharingConfig.scope =
    ALL_USERS). Without it, a registered agent is visible only to whoever
    created it, so licensed business users do not see it in the web app.
    The scope is the app, not the organisation: users of another project's
    Gemini Enterprise app never see it.
    """
    base = _de(f"{engine_name()}/assistants/default_assistant/agents")
    listing = _call("GET", base) or {}
    body = {
        "displayName": card["name"],
        "description": card.get("description", ""),
        "a2aAgentDefinition": {"jsonAgentCard": json.dumps(card)},
        "sharingConfig": {"scope": AGENT_SHARING_SCOPE},
    }
    for agent in listing.get("agents", []):
        if agent.get("displayName") == card["name"]:
            _call("PATCH", f"{DE_HOST}/{DE_VER}/{agent['name']}"
                           "?updateMask=displayName,description,a2aAgentDefinition,sharingConfig", body)
            return agent["name"], "updated"
    created = _call("POST", base, body) or {}
    name = created.get("name", "")
    # Belt and braces: if the create call ignored sharingConfig, set it explicitly.
    if name and created.get("sharingConfig", {}).get("scope") != AGENT_SHARING_SCOPE:
        _call("PATCH", f"{DE_HOST}/{DE_VER}/{name}?updateMask=sharingConfig",
              {"sharingConfig": {"scope": AGENT_SHARING_SCOPE}})
    return name, "registered"


def assign_seats(license_config: str, users: list[str]) -> list[str]:
    """Assigns licence seats; returns the users now holding one."""
    if not users:
        return []
    op = _call(
        "POST",
        _de(f"projects/{project_number()}/locations/global/userStores/default_user_store:batchUpdateUserLicenses"),
        {
            "inlineSource": {
                "userLicenses": [{"userPrincipal": u, "licenseConfig": license_config} for u in users],
                "updateMask": {"paths": ["userPrincipal", "licenseConfig"]},
            },
            "deleteUnassignedUserLicenses": False,
        },
    ) or {}
    response = _wait(op)
    return [u["userPrincipal"] for u in response.get("userLicenses", []) if u.get("licenseAssignmentState") == "ASSIGNED"]


def web_app_url() -> str:
    """The Gemini Enterprise web app URL business users open."""
    widget = _call("GET", _de(f"{engine_name()}/widgetConfigs/default_search_widget_config")) or {}
    config_id = widget.get("configId")
    if not config_id:
        raise PublishError("the app has no web app config yet; press Publish again in a minute")
    return f"{WEB_APP_BASE}/{config_id}"


def parse_users(raw: Any) -> list[str]:
    """Validates the optional business-user list from the browser.

    Raises:
        ValueError: If any entry is not an email address, or there are too many.
    """
    if not raw:
        return []
    items = raw if isinstance(raw, list) else re.split(r"[\s,;]+", str(raw))
    users = [u.strip().lower() for u in items if u and u.strip()]
    if len(users) > 25:
        raise ValueError("at most 25 business users per publish")
    bad = [u for u in users if not EMAIL_RE.match(u)]
    if bad:
        raise ValueError(f"not an email address: {', '.join(bad[:3])}")
    return users


# ------------------------------------------------------------------------------
# The button
# ------------------------------------------------------------------------------
def publish(extra_users: list[str] | None = None) -> Report:
    """Runs every step and returns the checklist. Never raises."""
    report = Report()
    me = _self_email()

    def run(key: str, title: str, fn: Any) -> Any:
        try:
            result = fn()
            report.add(key, title, "ok", str(result) if isinstance(result, str) else "")
            return result
        except PublishError as exc:
            report.add(key, title, "error", str(exc))
        except (requests.RequestException, google.auth.exceptions.GoogleAuthError) as exc:
            report.add(key, title, "error", f"{type(exc).__name__}: {exc}")
        return None

    if run("provision", "Gemini Enterprise enabled in this project", provision) is None:
        return report
    run("identity", "Sign-in with Google identity", ensure_identity)
    license_config = run("license", "Gemini Enterprise trial licence", ensure_license)
    if license_config:
        report.steps[-1].detail = f"{license_config.rsplit('/', 1)[-1]} (30-day trial)"
    app_state = run("app", f"Gemini Enterprise app “{ENGINE_DISPLAY_NAME}”", ensure_app)
    if app_state is None:
        return report

    for spec in AGENTS:
        title = f"{spec['lab']} agent ({spec['service']})"
        try:
            svc = find_service(spec["service"])
            if not svc or not svc.get("uri"):
                report.add(spec["service"], title, "skipped",
                           f"not deployed in {REGION} — deploy it in the {spec['guide']} lab first, then publish again")
                continue
            added = grant_invoker(spec["service"], [f"serviceAccount:{service_agent()}", f"serviceAccount:{me}"])
            card = fetch_card(svc["uri"])
            name, action = register_agent(card)
            report.agents.append({"lab": spec["lab"], "service": spec["service"], "name": card["name"],
                                  "url": card["url"], "resource": name, "action": action})
            grant = "invoker granted to the GE service agent" if added else "invoker already in place"
            report.add(spec["service"], title, "ok", f"{action} as “{card['name']}” · {grant}")
        except PublishError as exc:
            report.add(spec["service"], title, "error", str(exc))
        except (requests.RequestException, google.auth.exceptions.GoogleAuthError) as exc:
            report.add(spec["service"], title, "error", f"{type(exc).__name__}: {exc}")

    if not report.agents:
        report.add("agents", "At least one agent published", "error",
                   "no agent is deployed yet — deploy one, then press Publish again")

    users = list(dict.fromkeys(([PARTICIPANT_EMAIL] if PARTICIPANT_EMAIL else []) + (extra_users or [])))
    if license_config and users:
        seats = run("seats", "Licence seats for business users", lambda: assign_seats(license_config, users))
        if seats is not None:
            report.licensed_users = seats
            report.steps[-1].detail = ", ".join(seats) or "none assigned"
    elif not users:
        report.add("seats", "Licence seats for business users", "warn",
                   "no user given — add business users below, or assign seats in the GE console")

    url = run("link", "Web app link", web_app_url)
    if url:
        report.web_app_url = url
    return report


def status() -> dict[str, Any]:
    """Read-only snapshot for the page header: what exists right now."""
    out: dict[str, Any] = {"project": PROJECT_ID, "region": REGION, "participant": PARTICIPANT_EMAIL,
                           "engine": ENGINE_ID, "agents": [], "web_app_url": "", "console_url": console_url()}
    try:
        out["app_exists"] = bool(_call("GET", _de(engine_name()), ok404=True))
        if out["app_exists"]:
            listing = _call("GET", _de(f"{engine_name()}/assistants/default_assistant/agents")) or {}
            out["registered"] = [
                {"name": a.get("displayName"), "state": a.get("state", ""),
                 "sharing": a.get("sharingConfig", {}).get("scope", "")}
                for a in listing.get("agents", []) if "a2aAgentDefinition" in a
            ]
            try:
                out["web_app_url"] = web_app_url()
            except PublishError:
                pass
    except (PublishError, requests.RequestException, google.auth.exceptions.GoogleAuthError) as exc:
        out["error"] = str(exc)
    for spec in AGENTS:
        try:
            svc = find_service(spec["service"])
            out["agents"].append({**spec, "deployed": bool(svc and svc.get("uri")), "uri": (svc or {}).get("uri", "")})
        except (PublishError, requests.RequestException, google.auth.exceptions.GoogleAuthError) as exc:
            out["agents"].append({**spec, "deployed": False, "error": str(exc)})
    return out


def registered_agent_for(service_uri: str) -> dict[str, Any] | None:
    """Returns the GE agent registration whose card points at `service_uri`."""
    listing = _call("GET", _de(f"{engine_name()}/assistants/default_assistant/agents"), ok404=True) or {}
    for agent in listing.get("agents", []):
        card = agent.get("a2aAgentDefinition", {}).get("jsonAgentCard", "")
        if service_uri and service_uri in card:
            return agent
    return None


def ask_through_gemini_enterprise(agent_resource: str, question: str) -> str:
    """Asks a registered agent through Gemini Enterprise's own A2A proxy.

    This is the hop a business user's question takes: Gemini Enterprise calls the
    Cloud Run agent as its service agent, with the invoker grant made by publish().
    The proxy speaks A2A over HTTP+JSON on the Discovery Engine v1 endpoint.

    Raises:
        PublishError: If the proxy refuses the call or returns no text.
    """
    base = f"{DE_HOST}/v1/{agent_resource}/a2a"
    body = {"message": {"role": "ROLE_USER", "content": [{"text": question}],
                        "messageId": f"lab12-{int(time.time() * 1000)}"}}
    data = _call("POST", f"{base}/v1/message:send", body) or {}
    message = data.get("message") or data.get("task", {}).get("status", {}).get("message", {})
    texts = [c.get("text", "") for c in message.get("content", []) if c.get("text")]
    if not texts:
        raise PublishError(f"Gemini Enterprise returned no text: {json.dumps(data)[:300]}")
    return "\n".join(texts)


def ask_directly(service_uri: str, question: str) -> str:
    """Sends one A2A v0.3 message/send straight to the agent with this console's ID token."""
    token = id_token.fetch_id_token(google.auth.transport.requests.Request(), service_uri)
    body = {"jsonrpc": "2.0", "id": "lab12-test", "method": "message/send", "params": {"message": {
        "role": "user", "kind": "message", "messageId": f"lab12-{int(time.time() * 1000)}",
        "parts": [{"kind": "text", "text": question}]}}}
    resp = requests.post(f"{service_uri}/a2a/", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=300)
    if resp.status_code != 200:
        raise PublishError(f"A2A call returned {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    if payload.get("error"):
        raise PublishError(f"A2A error: {payload['error']}")
    result = payload.get("result", {})
    texts = [p.get("text", "") for a in result.get("artifacts", []) for p in a.get("parts", []) if p.get("text")]
    return "\n".join(texts) or "(no text)"


def test_agent(service: str, question: str) -> dict[str, Any]:
    """Asks one agent a question, preferably through Gemini Enterprise itself.

    Route 1 (end to end): Gemini Enterprise's A2A proxy -> the Cloud Run agent,
    called as the Discovery Engine service agent. Proves the registration, the
    invoker grant and the A2A surface together.
    Route 2 (fallback): straight to the agent's /a2a/ with this console's identity,
    used only if the agent is not registered yet or the proxy refuses this caller.

    Raises:
        PublishError: If the agent is not deployed or both routes fail.
    """
    svc = find_service(service)
    if not svc or not svc.get("uri"):
        raise PublishError(f"{service} is not deployed in {REGION}")
    proxy_error = ""
    agent = None
    try:
        agent = registered_agent_for(svc["uri"])
    except PublishError as exc:
        proxy_error = str(exc)
    if agent:
        try:
            answer = ask_through_gemini_enterprise(agent["name"], question)
            return {"service": service, "question": question, "answer": answer, "route": "gemini-enterprise"}
        except PublishError as exc:
            proxy_error = str(exc)
    answer = ask_directly(svc["uri"], question)
    note = "not registered yet — press Publish" if agent is None and not proxy_error else proxy_error
    return {"service": service, "question": question, "answer": answer, "route": "direct-a2a", "note": note}


def _self_email() -> str:
    """Email of the identity this console runs as."""
    email = os.getenv("PUBLISHER_SA", "")
    if email:
        return email
    try:
        resp = requests.get(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
            headers={"Metadata-Flavor": "Google"}, timeout=5,
        )
        return resp.text.strip()
    except requests.RequestException:
        return ""
