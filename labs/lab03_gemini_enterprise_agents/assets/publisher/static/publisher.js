// Lab 12 publisher console: status on load, one-click publish, A2A try-it.
// No framework; every response is JSON from main.py and rendered with
// textContent, so nothing returned by an API is ever parsed as HTML.

const ICONS = { ok: "✓", warn: "!", skipped: "–", error: "✗", pending: "…" };

async function getJSON(url, options = {}) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = {};
  try {
    data = await resp.json();
  } catch {
    data = { error: `HTTP ${resp.status}` };
  }
  return { ok: resp.ok, data };
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function refreshStatus() {
  const { data } = await getJSON("/api/status");
  for (const agent of data.agents || []) {
    const row = document.querySelector(`#agents li[data-service="${CSS.escape(agent.service)}"]`);
    if (!row) continue;
    const badge = row.querySelector('[data-role="state"]');
    if (agent.error) {
      badge.textContent = "error";
      badge.className = "badge err";
      badge.title = agent.error;
    } else if (agent.deployed) {
      badge.textContent = "deployed";
      badge.className = "badge ok";
      badge.title = agent.uri;
    } else {
      badge.textContent = "not deployed";
      badge.className = "badge missing";
    }
  }
  if (data.web_app_url) showResult(data.web_app_url, []);
}

function renderSteps(steps) {
  const list = document.getElementById("steps");
  list.replaceChildren();
  for (const step of steps) {
    const li = el("li", step.status);
    li.append(el("span", "icon", ICONS[step.status] || "•"));
    const body = el("div");
    body.append(el("span", "title", step.title));
    if (step.detail) body.append(el("span", "detail", step.detail));
    li.append(body);
    list.append(li);
  }
}

function showResult(url, users) {
  const section = document.getElementById("result");
  const link = document.getElementById("web-app-link");
  link.href = url;
  link.textContent = url;
  const licensed = document.getElementById("licensed");
  licensed.textContent = users.length ? `Licensed: ${users.join(", ")}` : "";
  section.hidden = false;
}

async function publish(event) {
  event.preventDefault();
  const button = document.getElementById("publish-btn");
  const steps = document.getElementById("steps");
  const formError = document.getElementById("form-error");
  formError.hidden = true;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  steps.setAttribute("aria-busy", "true");
  renderSteps([{ status: "pending", title: "Publishing… this takes up to a minute", detail: "" }]);
  try {
    const users = document.getElementById("users").value;
    const { data } = await getJSON("/api/publish", {
      method: "POST",
      body: JSON.stringify({ users }),
    });
    if (data.error) {
      formError.textContent = data.error;
      formError.hidden = false;
    }
    renderSteps(data.steps || []);
    if (data.web_app_url) showResult(data.web_app_url, data.licensed_users || []);
    await refreshStatus();
  } catch (err) {
    formError.textContent = `Publish failed: ${err}`;
    formError.hidden = false;
  } finally {
    button.disabled = false;
    button.setAttribute("aria-busy", "false");
    steps.setAttribute("aria-busy", "false");
  }
}

async function tryAgent(event) {
  const button = event.currentTarget;
  const answer = document.getElementById("answer");
  const { service, question } = button.dataset;
  document.querySelectorAll(".try-btn").forEach((b) => { b.disabled = true; });
  answer.hidden = false;
  answer.replaceChildren(el("span", "q", question), document.createTextNode("Asking over A2A…"));
  try {
    const { data } = await getJSON("/api/test", {
      method: "POST",
      body: JSON.stringify({ service, question }),
    });
    answer.replaceChildren(
      el("span", "q", `${service}: ${question}`),
      document.createTextNode(data.error ? `Error: ${data.error}` : data.answer),
    );
  } finally {
    document.querySelectorAll(".try-btn").forEach((b) => { b.disabled = false; });
  }
}

async function copyLink() {
  const link = document.getElementById("web-app-link").href;
  const button = document.getElementById("copy-btn");
  try {
    await navigator.clipboard.writeText(link);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Select and copy the link";
  }
  setTimeout(() => { button.textContent = "Copy link"; }, 2000);
}

document.getElementById("publish-form").addEventListener("submit", publish);
document.getElementById("copy-btn").addEventListener("click", copyLink);
document.querySelectorAll(".try-btn").forEach((b) => b.addEventListener("click", tryAgent));
refreshStatus();
