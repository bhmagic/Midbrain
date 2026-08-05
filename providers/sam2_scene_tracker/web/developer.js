"use strict";

const descriptions = {
  composite: "Composite: red declared objects, orange arm exclusion",
  rgb: "RGB camera image",
  depth: "Aligned metric depth color map",
};
let mode = "composite";

function pretty(value) {
  return JSON.stringify(value ?? null, null, 2);
}

function refreshImage() {
  const image = document.getElementById("sceneImage");
  const error = document.getElementById("imageError");
  image.onload = () => { error.hidden = true; image.hidden = false; };
  image.onerror = () => { error.hidden = false; image.hidden = true; };
  image.src = `/v1/visualization/${mode}.png?t=${Date.now()}`;
}

async function refreshStatus() {
  try {
    const response = await fetch("/v1/status", {cache: "no-store"});
    if (!response.ok) throw new Error(`status ${response.status}`);
    const status = await response.json();
    const diagnostics = status.details?.diagnostics || {};
    const badge = document.getElementById("health");
    badge.textContent = `${status.residency} / ${status.health}`;
    document.getElementById("coverage").textContent = pretty({
      ready: status.ready,
      coverage: diagnostics.coverage,
      policy: diagnostics.policy,
      assertion_count: diagnostics.assertion_count,
    });
    document.getElementById("diagnostics").textContent = pretty({
      status: diagnostics.status,
      mask_partition: diagnostics.mask_partition,
      sam2_scores: diagnostics.sam2_scores,
      segmentation_errors: diagnostics.segmentation_errors,
      fusion_updates: diagnostics.fusion_updates,
      semantic_map: diagnostics.semantic_map,
      tick_elapsed_ms: diagnostics.tick_elapsed_ms,
    });
  } catch (error) {
    document.getElementById("health").textContent = "UNAVAILABLE";
    document.getElementById("diagnostics").textContent = String(error);
  }
}

for (const button of document.querySelectorAll("button[data-mode]")) {
  button.addEventListener("click", () => {
    mode = button.dataset.mode;
    for (const candidate of document.querySelectorAll("button[data-mode]")) {
      candidate.classList.toggle("active", candidate === button);
    }
    document.getElementById("imageMode").textContent = descriptions[mode];
    refreshImage();
  });
}

refreshStatus();
refreshImage();
window.setInterval(refreshStatus, 1000);
window.setInterval(refreshImage, 1000);
