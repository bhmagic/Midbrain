(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const state = {
    sessions: [],
    runsBySessionId: new Map(),
    detailsByRunId: new Map(),
    expandedSessionIds: new Set(),
    selectedSessionId: "",
    selectedRunId: "",
    requestSequence: 0
  };

  function text(value, fallback = "") {
    const normalized = String(value ?? "").trim();
    return normalized || fallback;
  }

  function localTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return text(value, "Unknown time");
    return date.toLocaleString([], {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit"
    });
  }

  function statusClass(value) {
    const normalized = text(value).toLowerCase();
    return ["active", "closed", "historical", "completed", "failed", "interrupted"]
      .includes(normalized) ? normalized : "";
  }

  function statusChip(element, value) {
    element.className = "status-chip";
    const cssClass = statusClass(value);
    if (cssClass) element.classList.add(cssClass);
    element.textContent = text(value, "UNKNOWN");
  }

  function metadataItem(label, value) {
    const wrapper = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = text(value, "—");
    wrapper.append(term, description);
    return wrapper;
  }

  async function fetchJson(url) {
    const response = await fetch(url, {
      cache: "no-store",
      headers: {"Accept": "application/json"}
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch (_error) {
      payload = {};
    }
    if (!response.ok) {
      throw new Error(text(payload.detail, `Request failed (${response.status})`));
    }
    return payload;
  }

  function queryMatches(values, query) {
    if (!query) return true;
    return values.some((value) =>
      String(value ?? "").toLowerCase().includes(query)
    );
  }

  function visibleRuns(sessionId, query) {
    const detail = state.runsBySessionId.get(sessionId);
    if (!detail) return [];
    if (!query) return detail.runs;
    return detail.runs.filter((run) => queryMatches([
      run.run_id,
      run.status,
      run.surface,
      run.user_prompt,
      run.assistant_answer,
      run.agent_model
    ], query));
  }

  function visibleSessions() {
    const query = byId("recordFilter").value.trim().toLowerCase();
    if (!query) return state.sessions;
    return state.sessions.filter((session) => {
      if (queryMatches([
        session.session_id,
        session.status,
        session.started_at,
        session.updated_at
      ], query)) return true;
      return visibleRuns(session.session_id, query).length > 0;
    });
  }

  function runCard(session, run) {
    const card = document.createElement("details");
    card.className = "record-card";
    card.open = run.run_id === state.selectedRunId;
    if (card.open) card.classList.add("selected");

    const summary = document.createElement("summary");
    summary.className = "record-summary";
    const titleGroup = document.createElement("div");
    const title = document.createElement("div");
    title.className = "record-title";
    title.textContent = `Agent run · ${localTime(run.started_at)}`;
    const subtitle = document.createElement("div");
    subtitle.className = "record-subtitle";
    subtitle.textContent = text(run.user_prompt, text(run.run_id, "Unknown run"));
    titleGroup.append(title, subtitle);
    const chip = document.createElement("span");
    statusChip(chip, run.status);
    summary.append(titleGroup, chip);

    const body = document.createElement("div");
    body.className = "record-body";
    const meta = document.createElement("dl");
    meta.className = "record-meta";
    meta.append(
      metadataItem("Events", run.event_count),
      metadataItem("Model", run.agent_model),
      metadataItem("Updated", localTime(run.updated_at))
    );
    const runId = document.createElement("p");
    runId.className = "record-run-id";
    runId.textContent = text(run.run_id, "Unknown run");
    const viewButton = document.createElement("button");
    viewButton.type = "button";
    viewButton.textContent = "View complete run record";
    viewButton.addEventListener("click", () => loadRun(session.session_id, run.run_id));
    body.append(meta, runId, viewButton);
    card.append(summary, body);
    summary.addEventListener("click", () => {
      window.setTimeout(() => {
        if (card.open) loadRun(session.session_id, run.run_id);
      }, 0);
    });
    return card;
  }

  function sessionCard(session, query) {
    const card = document.createElement("details");
    card.className = "session-card";
    card.open = state.expandedSessionIds.has(session.session_id);
    if (session.session_id === state.selectedSessionId) {
      card.classList.add("selected");
    }

    const summary = document.createElement("summary");
    summary.className = "session-summary";
    const titleGroup = document.createElement("div");
    const title = document.createElement("div");
    title.className = "session-title";
    title.textContent = `Midbrain session · ${localTime(session.started_at)}`;
    const subtitle = document.createElement("div");
    subtitle.className = "session-subtitle";
    subtitle.textContent = `${text(session.session_id).slice(0, 16)} · ${session.run_count ?? 0} run${session.run_count === 1 ? "" : "s"}`;
    titleGroup.append(title, subtitle);
    const chip = document.createElement("span");
    statusChip(chip, session.status);
    summary.append(titleGroup, chip);

    const body = document.createElement("div");
    body.className = "session-body";
    const detail = state.runsBySessionId.get(session.session_id);
    if (detail) {
      const runs = visibleRuns(session.session_id, query);
      if (runs.length) {
        for (const run of [...runs].reverse()) {
          body.appendChild(runCard(session, run));
        }
      } else {
        const empty = document.createElement("p");
        empty.className = "record-event-loading";
        empty.textContent = "No runs in this session match the filter.";
        body.appendChild(empty);
      }
    } else {
      const loading = document.createElement("p");
      loading.className = "record-event-loading";
      loading.textContent = "Expand this Midbrain session to load its Agent runs.";
      body.appendChild(loading);
    }
    card.append(summary, body);
    summary.addEventListener("click", () => {
      window.setTimeout(() => {
        if (card.open) {
          state.expandedSessionIds.add(session.session_id);
          loadSession(session.session_id, true);
        } else {
          state.expandedSessionIds.delete(session.session_id);
        }
      }, 0);
    });
    return card;
  }

  function renderSessionList() {
    const list = byId("recordList");
    const sessions = visibleSessions();
    const query = byId("recordFilter").value.trim().toLowerCase();
    const fragment = document.createDocumentFragment();
    list.replaceChildren();
    byId("recordEmpty").hidden = sessions.length > 0;
    byId("recordCount").textContent = `${sessions.length} of ${state.sessions.length} retained Midbrain session${state.sessions.length === 1 ? "" : "s"}`;
    for (const session of sessions) {
      fragment.appendChild(sessionCard(session, query));
    }
    list.appendChild(fragment);
  }

  function groupForEvent(event) {
    const type = text(event.type, "other");
    if (type.startsWith("run.")) return ["run", "Run lifecycle"];
    if (type.startsWith("assistant.reasoning")) return ["reasoning", "Reasoning summaries"];
    if (type.startsWith("assistant.")) return ["assistant", "Assistant output"];
    if (type.startsWith("tool.")) return ["tool", "Tool activity"];
    if (type.includes("approval")) return ["approval", "Approvals"];
    if (type.startsWith("skill.retry")) return ["retry", "Retries"];
    if (type.startsWith("visual.")) return ["visual", "Visual evidence"];
    if (type.startsWith("agent.") || type.startsWith("mcp.")) {
      return ["coordination", "Agent and MCP coordination"];
    }
    return ["other", "Other events"];
  }

  function eventDetail(event) {
    const details = document.createElement("details");
    details.className = "event-record";
    const summary = document.createElement("summary");
    const sequence = Number.isInteger(event.sequence) ? `#${event.sequence}` : "#?";
    summary.textContent = `${sequence} · ${text(event.type, "unknown event")} · ${text(event.source, "unknown source")} · ${localTime(event.occurred_at)}`;
    const raw = document.createElement("pre");
    raw.textContent = JSON.stringify(event, null, 2);
    details.append(summary, raw);
    return details;
  }

  function renderEventGroups(events) {
    const groups = new Map();
    for (const event of events) {
      const [key, label] = groupForEvent(event);
      if (!groups.has(key)) groups.set(key, {label, events: []});
      groups.get(key).events.push(event);
    }
    const container = byId("eventGroups");
    const fragment = document.createDocumentFragment();
    container.replaceChildren();
    for (const group of groups.values()) {
      const outer = document.createElement("details");
      outer.className = "event-group";
      const summary = document.createElement("summary");
      const title = document.createElement("strong");
      title.textContent = group.label;
      const count = document.createElement("span");
      count.className = "event-count";
      count.textContent = `${group.events.length} event${group.events.length === 1 ? "" : "s"}`;
      summary.append(title, count);
      const list = document.createElement("div");
      list.className = "event-list";
      for (const event of group.events) list.appendChild(eventDetail(event));
      outer.append(summary, list);
      fragment.appendChild(outer);
    }
    if (!groups.size) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "This retained run has no event envelopes.";
      fragment.appendChild(empty);
    }
    container.appendChild(fragment);
  }

  function message(roleText, bodyText, occurredAt, cssClass = "") {
    const article = document.createElement("article");
    article.className = `message ${cssClass}`.trim();
    const head = document.createElement("div");
    head.className = "message-head";
    const role = document.createElement("strong");
    role.textContent = roleText;
    const time = document.createElement("time");
    time.dateTime = occurredAt;
    time.textContent = localTime(occurredAt);
    head.append(role, time);
    const body = document.createElement("pre");
    body.textContent = bodyText;
    article.append(head, body);
    return article;
  }

  function renderOutcome(run, events) {
    const container = byId("runConversation");
    container.replaceChildren();
    if (run.user_prompt) {
      container.appendChild(message("You", String(run.user_prompt), run.started_at, "message-user"));
    }
    const terminal = [...events].reverse().find((event) =>
      ["run.completed", "run.failed"].includes(event.type)
    );
    const payload = terminal?.payload || {};
    const answer = text(
      run.assistant_answer || payload.answer || payload.error,
      run.status === "INTERRUPTED"
        ? "The prior process ended before this run reached a terminal event."
        : "No public terminal response was retained for this run."
    );
    container.appendChild(message(
      "Agent outcome",
      answer,
      terminal?.occurred_at || run.updated_at,
      terminal?.type === "run.failed" ? "error-copy" : ""
    ));
  }

  function renderRunDetail(run, events) {
    byId("detailEmpty").hidden = true;
    byId("detailContent").hidden = false;
    byId("detailHeading").textContent = text(run.run_id, "Unknown run");
    statusChip(byId("detailStatus"), run.status);
    const meta = byId("runMeta");
    meta.replaceChildren(
      metadataItem("Midbrain session", text(run.session_id).slice(0, 16)),
      metadataItem("Agent model", run.agent_model),
      metadataItem("Reasoning", run.reasoning_effort),
      metadataItem("Events", run.event_count),
      metadataItem("Started", localTime(run.started_at)),
      metadataItem("Updated", localTime(run.updated_at)),
      metadataItem("Terminal", run.terminal_at ? localTime(run.terminal_at) : "Active"),
      metadataItem("Retention", "Bounded local journal")
    );
    renderOutcome(run, events);
    renderEventGroups(events);
    byId("runDetailPane").scrollTop = 0;
  }

  async function loadRun(sessionId, runId) {
    const request = ++state.requestSequence;
    state.selectedSessionId = sessionId;
    state.selectedRunId = runId;
    state.expandedSessionIds.add(sessionId);
    const retained = state.detailsByRunId.get(runId);
    if (retained) {
      renderSessionList();
      renderRunDetail(retained.run, retained.events);
      return;
    }
    renderSessionList();
    byId("detailEmpty").hidden = false;
    byId("detailEmpty").textContent = "Loading complete normalized event record…";
    byId("detailContent").hidden = true;
    try {
      const payload = await fetchJson(`/api/run-journal/runs/${encodeURIComponent(runId)}`);
      if (request !== state.requestSequence) return;
      const detail = {
        run: payload.run || {},
        events: Array.isArray(payload.events) ? payload.events : []
      };
      state.detailsByRunId.set(runId, detail);
      renderSessionList();
      renderRunDetail(detail.run, detail.events);
    } catch (error) {
      if (request !== state.requestSequence) return;
      byId("detailEmpty").hidden = false;
      byId("detailEmpty").textContent = error.message;
    }
  }

  async function loadSession(sessionId, selectLatestRun = false) {
    state.selectedSessionId = sessionId;
    state.expandedSessionIds.add(sessionId);
    const retained = state.runsBySessionId.get(sessionId);
    if (retained) {
      renderSessionList();
      if (selectLatestRun && !state.selectedRunId && retained.runs.length) {
        const latest = retained.runs[retained.runs.length - 1];
        await loadRun(sessionId, latest.run_id);
      }
      return;
    }
    renderSessionList();
    try {
      const payload = await fetchJson(`/api/run-journal/sessions/${encodeURIComponent(sessionId)}`);
      const detail = {
        session: payload.session || {},
        runs: Array.isArray(payload.runs) ? payload.runs : []
      };
      state.runsBySessionId.set(sessionId, detail);
      renderSessionList();
      if (selectLatestRun && detail.runs.length) {
        const latest = detail.runs[detail.runs.length - 1];
        await loadRun(sessionId, latest.run_id);
      }
    } catch (error) {
      byId("journalHealth").textContent = error.message;
      byId("journalHealth").classList.add("error-copy");
    }
  }

  async function loadSessions() {
    byId("refreshRecords").disabled = true;
    byId("recordCount").textContent = "Loading retained Midbrain sessions…";
    try {
      const payload = await fetchJson("/api/run-journal/sessions?limit=100");
      state.sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
      state.runsBySessionId.clear();
      state.detailsByRunId.clear();
      const health = payload.journal || {};
      byId("journalHealth").textContent = [
        `${text(health.status, "unknown").toUpperCase()} journal`,
        text(health.database),
        `${health.maximum_runs ?? "?"} runs maximum`,
        `${health.retention_days ?? "?"} days`
      ].filter(Boolean).join(" · ");
      renderSessionList();
      if (state.sessions.length) {
        const current = state.sessions.find((session) => session.status === "ACTIVE") || state.sessions[0];
        state.expandedSessionIds.add(current.session_id);
        state.selectedRunId = "";
        await loadSession(current.session_id, true);
      } else {
        byId("detailContent").hidden = true;
        byId("detailEmpty").hidden = false;
        byId("detailEmpty").textContent = "No durable Midbrain sessions have been retained yet.";
      }
    } catch (error) {
      state.sessions = [];
      renderSessionList();
      byId("journalHealth").textContent = error.message;
      byId("journalHealth").classList.add("error-copy");
    } finally {
      byId("refreshRecords").disabled = false;
    }
  }

  byId("recordFilter").addEventListener("input", renderSessionList);
  byId("refreshRecords").addEventListener("click", loadSessions);
  loadSessions();
})();
