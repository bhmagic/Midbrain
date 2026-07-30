"use strict";

const shutdownCheck = document.getElementById("shutdownCheck");
const shutdownButton = document.getElementById("shutdownButton");
const shutdownStatus = document.getElementById("shutdownStatus");

shutdownCheck.addEventListener("change", () => {
  shutdownButton.disabled = !shutdownCheck.checked;
});

shutdownButton.addEventListener("click", async () => {
  if (!shutdownCheck.checked) {
    return;
  }
  shutdownButton.disabled = true;
  shutdownCheck.disabled = true;
  shutdownStatus.textContent = "Requesting dependency-aware shutdown…";
  try {
    const response = await fetch("/v1/ui/shutdown", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({confirmation: "SHUT_DOWN_MIDBRAIN"})
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || JSON.stringify(result));
    }
    shutdownStatus.textContent =
      "Shutdown accepted. This page will remain visible after Midbrain stops.";
  } catch (error) {
    shutdownStatus.textContent = `Shutdown request failed: ${error.message}`;
    shutdownCheck.disabled = false;
    shutdownButton.disabled = !shutdownCheck.checked;
  }
});
