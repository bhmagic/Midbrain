"use strict";

const $ = (id) => document.getElementById(id);

function show(id, value, fallback = "—") {
  $(id).textContent = value === null || value === undefined || value === "" ? fallback : String(value);
}

function evidencePath(source, key) {
  const value = source?.[key];
  if (typeof value === "string") return value;
  if (value && typeof value === "object") return value.path || value.mapping_name || JSON.stringify(value);
  return null;
}

function diagnostic(id, value) {
  show(id, value || "Healthy");
  $(id).className = value ? "error" : "";
}

async function refresh() {
  try {
    const response = await fetch("/v1/status", { cache: "no-store" });
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || `Status returned ${response.status}`);
    const details = status.details || {};
    const estimate = details.last_estimate || null;
    const source = estimate?.source_evidence || {};
    show("residency", status.residency);
    show("health", status.health);
    show("capability", status.ready ? "READY" : "NOT READY");
    show("backend", details.resource_profile?.gpu);
    show("version", details.provider_version);
    show("process", `PID ${status.pid} · ${status.instance_id}`);
    show("measurement", estimate?.measurement_id);
    show("meshFilename", estimate?.mesh_filename);
    show("meshPath", estimate?.mesh_path);
    show("meshSha", estimate?.mesh_sha256);
    show("score", estimate?.score);
    show("elapsed", estimate ? `${Number(estimate.native_elapsed_ms).toFixed(2)} ms` : null);
    show("rgbEvidence", evidencePath(source, "rgb"));
    show("depthEvidence", evidencePath(source, "depth"));
    show("maskEvidence", estimate?.mask_path || evidencePath(source, "mask"));
    $("estimateState").textContent = estimate ? "ESTIMATE AVAILABLE" : "NO ESTIMATE YET";
    $("estimateState").className = estimate ? "status-chip ok" : "status-chip";
    diagnostic("managerError", details.manager_error);
    diagnostic("fabricError", details.fabric_error);
    diagnostic("providerError", details.last_error);
    $("rawStatus").textContent = JSON.stringify(status, null, 2);
    $("observedAt").textContent = `Observed ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    $("observedAt").textContent = "Unavailable";
    diagnostic("providerError", error.message);
  }
}

$("refreshButton").addEventListener("click", refresh);
refresh();
window.setInterval(refresh, 2000);
