"use strict";

const $ = (id) => document.getElementById(id);
const pathParts = window.location.pathname.split("/").filter(Boolean);
const kind = pathParts[1];
const identity = decodeURIComponent(pathParts.slice(2).join("/"));
const apiPath = `/v1/ui/${kind === "provider" ? "providers" : "skills"}/${encodeURIComponent(identity)}`;

function pretty(value) {
  return JSON.stringify(value ?? null, null, 2);
}

function stateText(value) {
  if (value == null || value === "") {
    return "Unknown";
  }
  return String(value);
}

function renderStreams(streams) {
  const container = $("streamCards");
  container.replaceChildren();
  if (!streams.length) {
    const empty = document.createElement("p");
    empty.className = "empty-cell";
    empty.textContent = "No matching Fabric streams are currently published.";
    container.append(empty);
    $("streamFreshness").textContent = "No streams";
    return;
  }
  let fresh = 0;
  for (const stream of streams) {
    const card = document.createElement("article");
    const name = document.createElement("strong");
    const timing = document.createElement("span");
    const identityLine = document.createElement("span");
    card.className = "stream-card";
    name.title = stream.stream || "";
    name.textContent = stream.stream || "unnamed stream";
    const age = stream.age_ms == null ? "age unknown" : `${stream.age_ms} ms old`;
    timing.textContent = `${stream.stale ? "STALE" : "FRESH"} · ${age}`;
    identityLine.textContent = `${stream.schema || "unknown schema"} · seq ${stream.latest_sequence ?? "?"}`;
    if (!stream.stale) {
      fresh += 1;
    }
    card.append(name, timing, identityLine);
    container.append(card);
  }
  $("streamFreshness").textContent = `${fresh}/${streams.length} fresh`;
}

function capabilityEntries(data) {
  const readiness = data.report?.details?.capability_readiness;
  if (readiness && typeof readiness === "object" && !Array.isArray(readiness)) {
    return Object.entries(readiness);
  }
  const required = data.manifest?.required_capabilities || [];
  const provided = data.manifest?.capabilities || [];
  return [...provided, ...required].map((name) => [name, "DECLARED"]);
}

function renderCapabilities(data) {
  const container = $("capabilityList");
  container.replaceChildren();
  const entries = capabilityEntries(data);
  if (!entries.length) {
    const empty = document.createElement("p");
    empty.className = "empty-cell";
    empty.textContent = "No capability declaration is available.";
    container.append(empty);
    return;
  }
  for (const [name, readiness] of entries) {
    const row = document.createElement("div");
    const label = document.createElement("span");
    const value = document.createElement("span");
    row.className = "capability-item";
    label.textContent = name;
    value.textContent = readiness === true ? "READY" : readiness === false ? "NOT READY" : String(readiness);
    row.append(label, value);
    container.append(row);
  }
}

function renderProvider(data) {
  const report = data.report || {};
  $("lifecycleValue").textContent = stateText(report.residency || data.process?.state);
  $("lifecycleDetail").textContent = data.process?.pid
    ? `PID ${data.process.pid}`
    : (data.process?.last_exit || "No managed process");
  $("readinessValue").textContent = report.ready === true ? "Ready" : report.ready === false ? "Not ready" : "Unknown";
  $("readinessDetail").textContent = report.health || "No heartbeat health";
  $("identityValue").textContent = report.instance_id ? report.instance_id.slice(0, 12) : "Unavailable";
  $("identityDetail").textContent = report.boot_id ? `boot ${report.boot_id.slice(0, 12)}` : "No boot identity";
  $("reportData").textContent = pretty({
    process: data.process,
    registry: data.registry,
    report: data.report,
  });
}

function renderSkill(data) {
  const availability = data.availability || {};
  $("lifecycleValue").textContent = data.status || "IDLE";
  $("lifecycleDetail").textContent = data.manifest?.lifecycle || "FINITE";
  $("readinessValue").textContent = availability.runtime_ready ? "Runtime ready" : "Not running";
  $("readinessDetail").textContent = availability.adapter_kind || "No execution adapter";
  $("identityValue").textContent = data.manifest?.version || "Unversioned";
  $("identityDetail").textContent = data.manifest?.agent_discovery?.tool_name || "Not agent-discoverable";
  $("reportData").textContent = pretty({
    availability,
    agent_discovery: data.manifest?.agent_discovery || null,
    route_policy: data.manifest?.route_policy || null,
  });
}

async function refresh() {
  try {
    const response = await fetch(apiPath, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Component observation returned ${response.status}`);
    }
    const data = await response.json();
    const streams = data.streams || [];
    document.title = `${data.display_name || data.id} · Midbrain Observation`;
    $("componentKind").textContent = `${String(data.kind || kind).toUpperCase()} · OBSERVATION`;
    $("componentName").textContent = data.display_name || data.id;
    $("componentId").textContent = data.id;
    $("componentStatus").textContent = data.status || "UNKNOWN";
    $("componentStatus").className = `large-status ${data.tone || "muted"}`;
    $("streamValue").textContent = `${streams.length} observed`;
    $("streamDetail").textContent = streams.length
      ? `${streams.filter((stream) => !stream.stale).length} currently fresh`
      : "No matching Fabric streams";
    const developerAvailable = Boolean(data.developer?.available);
    $("developerLink").hidden = !developerAvailable;
    if (developerAvailable) {
      $("developerLink").href = `/developer/${data.kind}/${encodeURIComponent(data.id)}`;
    }

    if (data.kind === "provider") {
      renderProvider(data);
    } else {
      renderSkill(data);
    }
    renderStreams(streams);
    renderCapabilities(data);
    $("latestData").textContent = pretty(data.latest);
    $("observedAt").textContent = `Observed ${new Date(data.observed_at).toLocaleString()}`;
  } catch (error) {
    $("componentStatus").textContent = "UNAVAILABLE";
    $("componentStatus").className = "large-status danger";
    $("reportData").textContent = error.message;
  }
}

refresh();
window.setInterval(refresh, 2000);
