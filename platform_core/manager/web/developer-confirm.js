"use strict";

const $ = (id) => document.getElementById(id);
const pathParts = window.location.pathname.split("/").filter(Boolean);
const kind = pathParts[1];
const identity = decodeURIComponent(pathParts.slice(2).join("/"));
const apiPath = `/v1/ui/${kind === "provider" ? "providers" : "skills"}/${encodeURIComponent(identity)}`;
const observationPath = `/observe/${kind}/${encodeURIComponent(identity)}`;
let developer = null;
let component = null;
let activationAccepted = false;
let activationResult = null;

$("backLink").href = observationPath;
$("cancelButton").href = observationPath;

async function loadBoundary() {
  try {
    const response = await fetch(apiPath, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Component lookup returned ${response.status}`);
    }
    const data = await response.json();
    component = data;
    developer = data.developer || {};
    $("componentName").textContent = `${data.display_name || data.id} (${data.id})`;
    $("componentState").textContent = data.status || "UNKNOWN";
    if (data.kind === "provider") {
      $("activationEffect").textContent =
        "Request HOT residency through Manager, then start and open the advertised development UI.";
      $("confirmationText").textContent =
        "I understand that this will activate the Provider, may initialize or energize attached hardware, and will enter a development surface outside the ordinary agent workflow.";
      $("continueButton").textContent = "Activate Provider and open UI";
    } else {
      $("activationEffect").textContent =
        "Start the Skill development UI. The finite Skill itself remains idle until explicitly run from that UI.";
      $("confirmationText").textContent =
        "I understand that this will start the Skill development UI and enter a development surface outside the ordinary agent workflow.";
      $("continueButton").textContent = "Start Skill development UI";
    }
    if (developer.url) {
      $("developerTarget").textContent = developer.url;
    } else if (developer.launch_command) {
      $("developerTarget").textContent = "Separate local development process";
      $("launchCommand").textContent = developer.launch_command;
    } else {
      $("developerTarget").textContent = "No development UI is advertised for this component.";
      $("confirmCheck").disabled = true;
    }
  } catch (error) {
    $("developerTarget").textContent = error.message;
    $("confirmCheck").disabled = true;
  }
}

$("confirmCheck").addEventListener("change", () => {
  $("continueButton").disabled = !$("confirmCheck").checked || !developer?.available;
});

$("continueButton").addEventListener("click", async () => {
  if (!$("confirmCheck").checked || !developer?.available || !component) {
    return;
  }
  $("continueButton").disabled = true;
  $("confirmCheck").disabled = true;
  $("activationStatus").textContent =
    component.kind === "provider"
      ? "Activating Provider through Manager…"
      : "Starting Skill development UI…";
  try {
    const response = await fetch(
      `/v1/ui/developer/${component.kind}/${encodeURIComponent(component.id)}/activate`,
      {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({confirmation: "ACTIVATE_DEVELOPER_SURFACE"})
      }
    );
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || JSON.stringify(result));
    }
    activationAccepted = true;
    activationResult = result;
    if (developer.url) {
      $("activationStatus").textContent =
        "Provider activation was accepted. Waiting for the separate development UI…";
      await waitForDeveloperUrl(developer.url);
      window.location.assign(developer.url);
      return;
    }
    $("activationStatus").textContent =
      "Development UI started as a separate local application.";
    $("launchCommandPanel").hidden = false;
    $("launchCommandMessage").textContent =
      "Manager accepted the launcher. If no window appears, inspect the launcher log or run:";
    $("continueButton").textContent = "Development UI started";
  } catch (error) {
    $("activationStatus").textContent = activationAccepted
      ? `Provider activation succeeded, but the separate development UI is not reachable yet: ${error.message}`
      : `Activation failed: ${error.message}`;
    if (activationAccepted) {
      const launcher = activationResult?.development_launcher || {};
      $("launchCommandPanel").hidden = false;
      $("launchCommandMessage").textContent =
        "The Provider is active. You may retry this page; launcher diagnostics:";
      $("launchCommand").textContent = [
        developer.url || "",
        launcher.stdout_log ? `Output: ${launcher.stdout_log}` : "",
        launcher.stderr_log ? `Errors: ${launcher.stderr_log}` : ""
      ].filter(Boolean).join("\n");
    }
    $("confirmCheck").disabled = false;
    $("continueButton").disabled = !$("confirmCheck").checked;
  }
});

async function waitForDeveloperUrl(url) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 700);
    try {
      await fetch(url, {
        mode: "no-cors",
        cache: "no-store",
        signal: controller.signal
      });
      return;
    } catch (_error) {
      await new Promise((resolve) => window.setTimeout(resolve, 300));
    } finally {
      window.clearTimeout(timer);
    }
  }
  throw new Error("development UI did not become reachable within 30 seconds");
}

loadBoundary();
