"use strict";

const views = {
  today: ["Today", "Highest-priority unresolved work relevant now."],
  "needs-you": ["Needs You", "Decisions, approvals and genuine human actions."],
  prepared: ["Prepared", "Work Virgil has prepared for review."],
  activity: ["Activity", "Sanitized operational changes, newest first."]
};
const emptyViews = {
  today: "You’re clear for now. New Gmail and Virgil events will appear here automatically.",
  "needs-you": "No decisions are waiting on you.",
  prepared: "Virgil has no prepared work waiting for review.",
  activity: "No operational activity has been recorded yet."
};

const state = { view: "today", project: "all", csrf: "", items: [], active: null };
const $ = id => document.getElementById(id);

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}

function label(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());
}

function age(value) {
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function chip(value, extra = "") {
  return node("span", label(value), `chip ${extra}`.trim());
}

function showConnection(message, connected = false) {
  const el = $("connection");
  el.textContent = message;
  el.classList.toggle("connected", connected);
  el.hidden = false;
}

function showOffline(show) {
  if (show) showConnection("Virgil is offline. Live operational items are unavailable.");
}

function renderConnection(connection) {
  const ingestion = connection.last_successful_queue_ingestion_at
    ? new Date(connection.last_successful_queue_ingestion_at).toLocaleString()
    : "none yet";
  showConnection(
    `Virgil connected · Gmail watcher ${connection.gmail_watcher} · Last successful queue ingestion: ${ingestion}`,
    true
  );
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = state.csrf;
  }
  const response = await fetch(path, { ...options, headers, cache: "no-store", credentials: "same-origin" });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  return response.json();
}

function empty(message) {
  const el = node("p", message, "empty");
  $("items").replaceChildren(el);
}

function renderItems(items) {
  $("count").textContent = String(items.length);
  if (!items.length) {
    empty(emptyViews[state.view]);
    return;
  }
  const cards = items.map(item => {
    const card = node("button", undefined, `card ${item.priority}`);
    card.type = "button";
    card.dataset.itemId = item.item_id;
    card.setAttribute("aria-label", `Open ${item.title}`);

    const meta = node("div", undefined, "meta");
    meta.append(chip(item.project), chip(item.item_type), chip(item.priority, item.priority), chip(item.status), chip(`waiting: ${item.waiting_on}`));
    if (item.confidence !== null) meta.append(chip(`${Math.round(item.confidence * 100)}% confidence`));
    meta.append(node("span", item.due_at ? `Due ${new Date(item.due_at).toLocaleString()}` : age(item.updated_at), "age"));
    card.append(meta, node("h3", item.title), node("p", item.safe_summary));
    if (item.recommended_action) card.append(node("p", `Next: ${item.recommended_action}`, "recommendation"));
    return card;
  });
  $("items").replaceChildren(...cards);
}

function renderActivity(events) {
  $("count").textContent = String(events.length);
  if (!events.length) {
    empty(emptyViews.activity);
    return;
  }
  $("items").replaceChildren(...events.map(event => {
    const el = node("article", undefined, "activity");
    const title = node("strong", label(event.event_type));
    const time = node("time", new Date(event.timestamp).toLocaleString());
    time.dateTime = event.timestamp;
    el.append(title, document.createElement("br"), time, node("p", event.safe_description));
    return el;
  }));
}

async function load() {
  $("items").setAttribute("aria-busy", "true");
  const [title, note] = views[state.view];
  $("view-title").textContent = title;
  $("view-note").textContent = note;
  try {
    const session = await api("/api/session");
    state.csrf = session.csrf_token;
    const project = state.project === "all" ? "" : `&project=${encodeURIComponent(state.project)}`;
    if (state.view === "activity") {
      renderActivity((await api(`/api/activity?limit=100${project}`)).events);
    } else {
      state.items = (await api(`/api/items?view=${state.view}&limit=100${project}`)).items;
      renderItems(state.items);
    }
    renderConnection(session.connection);
  } catch (error) {
    if (navigator.onLine) showConnection("Virgil connection problem.");
    else showOffline(true);
    empty(navigator.onLine ? `Virgil could not load live items: ${error.message}` : "Live items will return when Virgil reconnects.");
  } finally {
    $("items").setAttribute("aria-busy", "false");
  }
}

function detailBlock(term, value) {
  const box = node("dl", undefined, "detail-block");
  box.append(node("dt", term), node("dd", value || "—"));
  return box;
}

function nextMonday(date) {
  const out = new Date(date);
  const days = (8 - out.getDay()) % 7 || 7;
  out.setDate(out.getDate() + days);
  out.setHours(9, 0, 0, 0);
  return out;
}

function deferTime(choice) {
  const now = new Date();
  if (choice === "hour") return new Date(now.getTime() + 3600000);
  if (choice === "today") {
    const out = new Date(now);
    out.setHours(17, 0, 0, 0);
    if (out <= now) out.setTime(now.getTime() + 3600000);
    return out;
  }
  if (choice === "tomorrow") {
    const out = new Date(now);
    out.setDate(out.getDate() + 1);
    out.setHours(9, 0, 0, 0);
    return out;
  }
  return nextMonday(now);
}

async function mutate(action, deferredUntil) {
  const item = state.active;
  if (!item) return;
  try {
    const body = { action, expected_row_version: item.row_version };
    if (deferredUntil) body.deferred_until = deferredUntil.toISOString();
    state.active = (await api(`/api/items/${item.item_id}/action`, { method: "POST", body: JSON.stringify(body) })).item;
    $("detail").close();
    await load();
  } catch (error) {
    alert(error.message);
  }
}

function actionButton(text, action, className = "") {
  const button = node("button", text, className);
  button.type = "button";
  button.dataset.action = action;
  return button;
}

function renderDetail(item, events) {
  state.active = item;
  const reopenable = ["resolved", "dismissed", "stale", "deferred"].includes(item.status);
  const content = $("detail-content");
  const meta = node("div", undefined, "meta");
  meta.append(chip(item.project), chip(item.item_type), chip(item.priority, item.priority), chip(item.status));
  const title = node("h2", item.title);
  title.id = "detail-title";

  const grid = node("div", undefined, "detail-grid");
  grid.append(
    meta,
    title,
    detailBlock("What happened", item.safe_summary),
    detailBlock("What Virgil decided", label(item.status)),
    detailBlock("Why", label(item.reason_code)),
    detailBlock("Recommended action", item.recommended_action),
    detailBlock("Waiting on", label(item.waiting_on)),
    detailBlock("Source system", label(item.source_type)),
    detailBlock("Confidence", item.confidence === null ? "Not provided" : `${Math.round(item.confidence * 100)}%`)
  );

  const actions = node("div", undefined, "actions");
  if (item.source_deep_link) {
    const open = node("a", "Open Source");
    open.href = item.source_deep_link;
    open.rel = "noopener noreferrer";
    open.target = "_blank";
    actions.append(open);
  }
  if (item.prepared_artifact_deep_link && item.prepared_artifact_deep_link !== item.source_deep_link) {
    const artifact = node("a", "Open Prepared Work");
    artifact.href = item.prepared_artifact_deep_link;
    artifact.rel = "noopener noreferrer";
    artifact.target = "_blank";
    actions.append(artifact);
  }
  if (reopenable) {
    actions.append(actionButton("Reopen", "reopen", "primary"));
  } else {
    actions.append(actionButton("Resolve", "resolve", "primary"), actionButton("Defer", "show-defer"), actionButton("Dismiss", "dismiss"));
  }
  grid.append(actions);

  if (!reopenable) {
    const defer = node("section", undefined, "defer-panel");
    defer.hidden = true;
    defer.id = "defer-panel";
    defer.setAttribute("aria-label", "Defer item");
    [["1 hour", "hour"], ["Later today", "today"], ["Tomorrow", "tomorrow"], ["Next Monday", "monday"]]
      .forEach(([text, value]) => defer.append(actionButton(text, `defer:${value}`)));
    const labelEl = node("label", "Custom date and time", "full");
    labelEl.htmlFor = "custom-defer";
    const input = document.createElement("input");
    input.id = "custom-defer";
    input.type = "datetime-local";
    const custom = actionButton("Defer to custom time", "defer:custom", "full");
    defer.append(labelEl, input, custom);
    grid.append(defer);
  }

  if (events.length) {
    grid.append(node("h3", "Activity"));
    events.forEach(event => grid.append(detailBlock(
      `${label(event.event_type)} · ${new Date(event.timestamp).toLocaleString()}`,
      event.safe_description
    )));
  }
  content.replaceChildren(grid);
}

async function openItem(id) {
  try {
    const data = await api(`/api/items/${encodeURIComponent(id)}`);
    renderDetail(data.item, data.events);
    $("detail").showModal();
    history.replaceState(null, "", `/item/${id}`);
  } catch (error) {
    alert(error.message);
  }
}

document.addEventListener("click", event => {
  const view = event.target.closest("[data-view]");
  if (view) {
    state.view = view.dataset.view;
    document.querySelectorAll("[data-view]").forEach(el => el.removeAttribute("aria-current"));
    view.setAttribute("aria-current", "page");
    void load();
    return;
  }
  const project = event.target.closest("[data-project]");
  if (project) {
    state.project = project.dataset.project;
    document.querySelectorAll("[data-project]").forEach(el => el.setAttribute("aria-pressed", String(el === project)));
    void load();
    return;
  }
  const card = event.target.closest("[data-item-id]");
  if (card) {
    void openItem(card.dataset.itemId);
    return;
  }
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (!action) return;
  if (action === "show-defer") {
    $("defer-panel").hidden = false;
    $("defer-panel").scrollIntoView({ block: "nearest" });
  } else if (action.startsWith("defer:")) {
    const choice = action.split(":")[1];
    const custom = $("custom-defer").value;
    const until = choice === "custom" ? new Date(custom) : deferTime(choice);
    if (Number.isNaN(until.getTime()) || until <= new Date()) {
      alert("Choose a future date and time.");
    } else {
      void mutate("defer", until);
    }
  } else {
    void mutate(action);
  }
});

$("refresh").addEventListener("click", () => void load());
$("close-detail").addEventListener("click", () => $("detail").close());
$("detail").addEventListener("close", () => history.replaceState(null, "", "/"));
window.addEventListener("offline", () => showOffline(true));
window.addEventListener("online", () => void load());

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
void load().then(() => {
  const match = location.pathname.match(/^\/item\/([0-9a-f]{32})$/);
  if (match) void openItem(match[1]);
});
