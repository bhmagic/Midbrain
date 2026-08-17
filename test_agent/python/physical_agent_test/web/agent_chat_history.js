(() => {
  "use strict";

  const DEFAULT_MAXIMUM_TURNS = 40;
  const DEFAULT_MAXIMUM_PROGRESS_ITEMS = 80;
  const DEFAULT_MAXIMUM_DETAILED_EVENTS = 2048;
  const DEFAULT_MAXIMUM_VISUAL_EVIDENCES = 32;

  function text(value, fallback = "") {
    const candidate = String(value ?? "").trim();
    return candidate || fallback;
  }

  function timestamp(value = null) {
    const date = value ? new Date(value) : new Date();
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit"
    });
  }

  function serialized(value) {
    try {
      return JSON.stringify(value);
    } catch (_error) {
      return "";
    }
  }

  function turnKey(state) {
    const runId = text(state?.run_id);
    return runId ? `run:${runId}` : `turn:${text(state?.turn_id)}`;
  }

  function eventKey(event) {
    const eventId = text(event?.event_id);
    if (eventId) return eventId;
    return [
      Number.isInteger(event?.sequence) ? event.sequence : "?",
      text(event?.type, "unknown"),
      text(event?.occurred_at)
    ].join(":");
  }

  function eventListRevision(events) {
    const values = Array.isArray(events) ? events : [];
    const latest = values.at(-1);
    return `${values.length}:${latest ? eventKey(latest) : ""}`;
  }

  function visualEvidenceKey(evidence) {
    return text(evidence?.evidence_id);
  }

  function visualEvidenceList(state) {
    const values = Array.isArray(state?.visual_evidences)
      ? state.visual_evidences
      : (state?.visual_evidence ? [state.visual_evidence] : []);
    const retained = [];
    const evidenceIds = new Set();
    for (const evidence of values) {
      const evidenceId = visualEvidenceKey(evidence);
      if (!evidenceId || evidenceIds.has(evidenceId)) continue;
      evidenceIds.add(evidenceId);
      retained.push(evidence);
    }
    return retained;
  }

  function visualEvidenceRevision(state) {
    return visualEvidenceList(state).map(visualEvidenceKey).join(":");
  }

  function serverStateRevision(state) {
    const progress = Array.isArray(state?.progress) ? state.progress : [];
    const latestProgress = progress.at(-1) || {};
    const answer = String(state?.answer ?? "");
    const reasoning = String(state?.reasoning ?? "");
    return [
      text(state?.status),
      text(state?.activity),
      answer.length,
      answer.slice(-64),
      reasoning.length,
      reasoning.slice(-64),
      progress.length,
      text(latestProgress.label),
      text(latestProgress.occurred_at),
      eventListRevision(state?.event_details),
      visualEvidenceRevision(state)
    ].join("|");
  }

  function createVisualEvidenceElements() {
    const panel = document.createElement("section");
    panel.className = "visual-evidence";
    panel.hidden = true;

    const head = document.createElement("div");
    head.className = "visual-evidence-head";
    const headingGroup = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = "Visual evidence";
    const meta = document.createElement("p");
    headingGroup.append(title, meta);
    head.appendChild(headingGroup);

    const toolbar = document.createElement("div");
    toolbar.className = "visual-toolbar";
    const channelButtons = document.createElement("span");
    const overlayLabel = document.createElement("label");
    const overlayEnabled = document.createElement("input");
    overlayEnabled.type = "checkbox";
    overlayEnabled.checked = true;
    overlayLabel.append(overlayEnabled, "Overlay");
    const annotationColorControls = document.createElement("span");
    annotationColorControls.className = "visual-annotation-colors";
    annotationColorControls.setAttribute("aria-label", "Annotation colors");
    const resetColorsButton = document.createElement("button");
    resetColorsButton.type = "button";
    resetColorsButton.textContent = "Reset colors";
    const copyButton = document.createElement("button");
    copyButton.type = "button";
    copyButton.textContent = "Copy annotated";
    const downloadButton = document.createElement("button");
    downloadButton.type = "button";
    downloadButton.textContent = "Download PNG";
    toolbar.append(
      channelButtons,
      overlayLabel,
      annotationColorControls,
      resetColorsButton,
      copyButton,
      downloadButton
    );

    const canvas = document.createElement("div");
    canvas.className = "visual-canvas";
    const image = document.createElement("img");
    image.alt = "Exact camera evidence used by the visual Skill";
    const overlay = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "svg"
    );
    overlay.setAttribute("viewBox", "0 0 1000 1000");
    overlay.setAttribute("aria-label", "Agent visual annotations");
    canvas.append(image, overlay);
    panel.append(head, toolbar, canvas);
    return {
      panel,
      title,
      meta,
      channelButtons,
      overlayEnabled,
      annotationColorControls,
      resetColorsButton,
      canvas,
      image,
      overlay,
      copyButton,
      downloadButton
    };
  }

  class AgentChatTurn {
    constructor(history, state, {attachmentUrl = null, restoring = false} = {}) {
      this.history = history;
      this.state = state;
      this.localOwner = !restoring;
      this.serverRevision = restoring ? serverStateRevision(state) : "";
      this.attachmentUrl = attachmentUrl;
      this.article = document.createElement("article");
      this.article.className = "chat-turn";
      this.article.dataset.status = state.status;
      this.article.dataset.runId = text(state.run_id);

      const userMessage = document.createElement("section");
      userMessage.className = "chat-message chat-message-user";
      const userHead = this.messageHead("You", state.started_at);
      this.prompt = document.createElement("p");
      this.prompt.className = "chat-message-text";
      this.prompt.textContent = state.prompt;
      userMessage.append(userHead, this.prompt);
      if (state.attachment_name) {
        const attachment = document.createElement("div");
        attachment.className = "chat-attachment";
        if (attachmentUrl) {
          const preview = document.createElement("img");
          preview.src = attachmentUrl;
          preview.alt = "Attached user image";
          attachment.appendChild(preview);
        }
        const attachmentName = document.createElement("span");
        attachmentName.textContent = state.attachment_name;
        attachment.appendChild(attachmentName);
        userMessage.appendChild(attachment);
      }

      const assistantMessage = document.createElement("section");
      assistantMessage.className = "chat-message chat-message-assistant";
      const assistantHead = this.messageHead("Agent", state.started_at);
      this.activity = document.createElement("p");
      this.activity.className = "chat-turn-activity";
      this.activity.textContent = state.activity;
      this.answer = document.createElement("pre");
      this.answer.className = "chat-answer";
      this.answer.textContent = state.answer;

      this.summaryPanel = document.createElement("details");
      this.summaryPanel.className = "reasoning-panel chat-execution-summary";
      this.summaryTitle = document.createElement("summary");
      this.progressList = document.createElement("ol");
      this.progressList.className = "chat-progress-list";
      this.reasoningLabel = document.createElement("h4");
      this.reasoningLabel.textContent = "Model-provided reasoning summary";
      this.reasoning = document.createElement("pre");
      this.reasoning.className = "chat-reasoning-summary";
      this.summaryPanel.append(
        this.summaryTitle,
        this.progressList,
        this.reasoningLabel,
        this.reasoning
      );

      this.eventPanel = document.createElement("details");
      this.eventPanel.className = "chat-event-details";
      this.eventTitle = document.createElement("summary");
      this.eventList = document.createElement("div");
      this.eventList.className = "chat-event-list";
      this.eventPanel.append(this.eventTitle, this.eventList);

      this.visualEvidenceList = document.createElement("div");
      this.visualEvidenceList.className = "visual-evidence-list";
      this.visualViewers = new Map();
      assistantMessage.append(
        assistantHead,
        this.activity,
        this.summaryPanel,
        this.eventPanel,
        this.visualEvidenceList,
        this.answer
      );
      this.article.append(userMessage, assistantMessage);
      this.history.container.appendChild(this.article);
      this.renderState();
      if (!restoring) this.addProgress("Run requested");
    }

    messageHead(role, value) {
      const head = document.createElement("div");
      head.className = "chat-message-head";
      const roleLabel = document.createElement("strong");
      roleLabel.textContent = role;
      const timeLabel = document.createElement("time");
      timeLabel.dateTime = value;
      timeLabel.textContent = timestamp(value);
      head.append(roleLabel, timeLabel);
      if (role === "Agent") {
        const model = document.createElement("span");
        model.className = "chat-model-label";
        model.textContent = [
          this.state.agent_model,
          this.state.reasoning_effort
            ? `${this.state.reasoning_effort} reasoning`
            : ""
        ].filter(Boolean).join(" | ");
        if (model.textContent) head.appendChild(model);
      }
      return head;
    }

    setRunId(runId) {
      this.state.run_id = text(runId);
      this.article.dataset.runId = this.state.run_id;
    }

    setActivity(value) {
      this.state.activity = text(value, this.state.activity);
      this.activity.textContent = this.state.activity;
      this.history.followIfNearBottom(this.state.status === "RUNNING");
    }

    appendAnswer(value) {
      this.state.answer += String(value ?? "");
      this.answer.textContent = this.state.answer;
      this.history.followIfNearBottom(this.state.status === "RUNNING");
    }

    setAnswer(value) {
      this.state.answer = String(value ?? "");
      this.answer.textContent = this.state.answer;
      this.history.followIfNearBottom(this.state.status === "RUNNING");
    }

    appendReasoning(value) {
      this.state.reasoning += String(value ?? "");
      this.reasoning.textContent = this.state.reasoning;
      this.renderSummary();
      this.history.followIfNearBottom(this.state.status === "RUNNING");
    }

    addProgress(value) {
      const label = text(value);
      if (!label) return;
      const previous = this.state.progress.at(-1);
      if (previous?.label === label) return;
      this.state.progress.push({
        label,
        occurred_at: new Date().toISOString()
      });
      if (this.state.progress.length > this.history.maximumProgressItems) {
        this.state.progress.splice(
          0,
          this.state.progress.length - this.history.maximumProgressItems
        );
      }
      this.renderSummary();
      this.history.followIfNearBottom(this.state.status === "RUNNING");
    }

    addEvent(event) {
      if (!this.history.detailedEvents || !event || typeof event !== "object") {
        return;
      }
      let normalized;
      try {
        normalized = JSON.parse(JSON.stringify(event));
      } catch (_error) {
        return;
      }
      this.state.event_details.push(normalized);
      if (
        this.state.event_details.length >
        this.history.maximumDetailedEvents
      ) {
        this.state.event_details.splice(
          0,
          this.state.event_details.length -
            this.history.maximumDetailedEvents
        );
        this.eventList.firstElementChild?.remove();
      }
      this.eventList.appendChild(this.createEventDetail(normalized));
      this.renderEventTitle();
      this.history.followIfNearBottom(this.state.status === "RUNNING");
    }

    createEventDetail(event) {
      const details = document.createElement("details");
      details.className = "chat-event-record";
      details.dataset.eventKey = eventKey(event);
      const summary = document.createElement("summary");
      const sequence = Number.isInteger(event.sequence)
        ? `#${event.sequence}`
        : "#?";
      summary.textContent = [
        sequence,
        text(event.type, "unknown event"),
        text(event.source, "unknown source"),
        timestamp(event.occurred_at)
      ].filter(Boolean).join(" · ");
      const raw = document.createElement("pre");
      raw.textContent = JSON.stringify(event, null, 2);
      details.append(summary, raw);
      return details;
    }

    renderEventsPreservingExpansion(previousEvents = []) {
      const nextEvents = Array.isArray(this.state.event_details)
        ? this.state.event_details
        : [];
      if (!this.history.detailedEvents) {
        this.eventList.replaceChildren();
        this.renderEventTitle();
        return;
      }
      const previous = Array.isArray(previousEvents) ? previousEvents : [];
      const isAppendOnly = previous.length <= nextEvents.length &&
        previous.every(
          (event, index) => eventKey(event) === eventKey(nextEvents[index])
        );
      if (isAppendOnly) {
        for (const event of nextEvents.slice(previous.length)) {
          this.eventList.appendChild(this.createEventDetail(event));
        }
        this.renderEventTitle();
        return;
      }
      const expanded = new Set(
        Array.from(
          this.eventList.querySelectorAll("details[open][data-event-key]")
        ).map((details) => details.dataset.eventKey)
      );
      this.eventList.replaceChildren();
      for (const event of nextEvents) {
        const details = this.createEventDetail(event);
        details.open = expanded.has(details.dataset.eventKey);
        this.eventList.appendChild(details);
      }
      this.renderEventTitle();
    }

    updateFromServer(nextState) {
      const previousState = this.state;
      const progressChanged =
        serialized(previousState.progress) !== serialized(nextState.progress);
      const reasoningChanged = previousState.reasoning !== nextState.reasoning;
      const statusChanged = previousState.status !== nextState.status;
      const eventsChanged = eventListRevision(previousState.event_details) !==
        eventListRevision(nextState.event_details);
      const visualChanged = serialized(visualEvidenceList(previousState)) !==
        serialized(visualEvidenceList(nextState));

      this.state = nextState;
      this.localOwner = false;
      this.serverRevision = serverStateRevision(nextState);
      this.article.dataset.status = nextState.status;
      this.article.dataset.runId = text(nextState.run_id);
      this.prompt.textContent = nextState.prompt;
      this.activity.textContent = nextState.activity;
      this.answer.textContent = nextState.answer;
      this.answer.classList.toggle("error", nextState.status === "FAILED");
      this.reasoning.textContent = nextState.reasoning;
      if (progressChanged || reasoningChanged || statusChanged) {
        this.renderSummary();
      }
      if (eventsChanged) {
        this.renderEventsPreservingExpansion(previousState.event_details);
      } else {
        this.renderEventTitle();
      }
      if (visualChanged) {
        this.syncVisualEvidences(visualEvidenceList(nextState));
      }
    }

    renderEventTitle() {
      const events = Array.isArray(this.state.event_details)
        ? this.state.event_details
        : [];
      this.eventPanel.hidden = !this.history.detailedEvents || !events.length;
      this.eventTitle.textContent = `Normalized event record | ${events.length} event${events.length === 1 ? "" : "s"}`;
    }

    syncVisualEvidences(evidences) {
      const values = Array.isArray(evidences) ? evidences : [];
      const desired = new Set();
      for (const evidence of values) {
        const evidenceId = visualEvidenceKey(evidence);
        if (!evidenceId || desired.has(evidenceId)) continue;
        desired.add(evidenceId);
        let entry = this.visualViewers.get(evidenceId);
        if (!entry) {
          const elements = createVisualEvidenceElements();
          entry = {
            elements,
            viewer: new window.MidbrainVisualEvidenceViewer({
              elements,
              onStatus: (message) => this.setActivity(message)
            })
          };
          this.visualViewers.set(evidenceId, entry);
        }
        entry.viewer.show(evidence);
        this.visualEvidenceList.appendChild(entry.elements.panel);
      }
      for (const [evidenceId, entry] of this.visualViewers) {
        if (desired.has(evidenceId)) continue;
        entry.elements.panel.remove();
        this.visualViewers.delete(evidenceId);
      }
    }

    showVisualEvidence(evidence) {
      const evidenceId = visualEvidenceKey(evidence);
      if (!evidenceId) return;
      const values = visualEvidenceList(this.state);
      const existingIndex = values.findIndex(
        (candidate) => visualEvidenceKey(candidate) === evidenceId
      );
      if (existingIndex >= 0) {
        values[existingIndex] = evidence;
      } else {
        values.push(evidence);
      }
      if (values.length > this.history.maximumVisualEvidences) {
        values.splice(0, values.length - this.history.maximumVisualEvidences);
      }
      this.state.visual_evidences = values;
      this.state.visual_evidence = values.at(-1) || null;
      this.syncVisualEvidences(values);
      this.history.followIfNearBottom(this.state.status === "RUNNING");
    }

    complete(answer = null) {
      if (answer !== null) this.setAnswer(answer);
      this.state.status = "COMPLETED";
      this.state.activity = "Completed";
      this.article.dataset.status = this.state.status;
      this.activity.textContent = this.state.activity;
      this.renderSummary();
      this.history.persist();
      this.history.followIfNearBottom(true);
    }

    cancel(message = "Run stopped by operator.") {
      this.state.status = "CANCELLED";
      this.state.activity = "Stopped";
      this.state.answer = text(message, "Run stopped by operator.");
      this.article.dataset.status = this.state.status;
      this.activity.textContent = this.state.activity;
      this.answer.textContent = this.state.answer;
      this.answer.classList.remove("error");
      this.addProgress("Run stopped; background Providers preserved");
      this.renderSummary();
      this.history.persist();
      this.history.followIfNearBottom(true);
    }

    fail(error) {
      const message = text(error?.message || error, "Agent run failed");
      this.state.status = "FAILED";
      this.state.activity = "Failed";
      this.state.answer = `Error: ${message}`;
      this.article.dataset.status = this.state.status;
      this.activity.textContent = this.state.activity;
      this.answer.textContent = this.state.answer;
      this.answer.classList.add("error");
      this.addProgress("Run failed");
      this.renderSummary();
      this.history.persist();
      this.history.followIfNearBottom(true);
    }

    renderState() {
      this.article.dataset.status = this.state.status;
      this.activity.textContent = this.state.activity;
      this.answer.textContent = this.state.answer;
      this.reasoning.textContent = this.state.reasoning;
      this.answer.classList.toggle("error", this.state.status === "FAILED");
      this.renderSummary();
      this.eventList.replaceChildren();
      if (this.history.detailedEvents) {
        for (const event of this.state.event_details) {
          this.eventList.appendChild(this.createEventDetail(event));
        }
      }
      this.renderEventTitle();
      this.syncVisualEvidences(visualEvidenceList(this.state));
    }

    renderSummary() {
      const progress = Array.isArray(this.state.progress)
        ? this.state.progress
        : [];
      const hasReasoning = Boolean(this.state.reasoning);
      this.summaryPanel.hidden = progress.length === 0 && !hasReasoning;
      const prefix = this.state.status === "RUNNING"
        ? "In-progress execution summary"
        : "Execution summary";
      const count = progress.length;
      this.summaryTitle.textContent = `${prefix} | ${count} update${count === 1 ? "" : "s"}`;
      this.progressList.replaceChildren();
      for (const entry of progress) {
        const item = document.createElement("li");
        const label = document.createElement("span");
        label.textContent = entry.label;
        const time = document.createElement("time");
        time.dateTime = entry.occurred_at;
        time.textContent = timestamp(entry.occurred_at);
        item.append(label, time);
        this.progressList.appendChild(item);
      }
      this.reasoningLabel.hidden = !hasReasoning;
      this.reasoning.hidden = !hasReasoning;
    }

    release() {
      if (this.attachmentUrl) URL.revokeObjectURL(this.attachmentUrl);
      this.article.remove();
    }
  }

  class MidbrainAgentChatHistory {
    constructor({
      container,
      emptyState = null,
      maximumTurns = DEFAULT_MAXIMUM_TURNS,
      maximumProgressItems = DEFAULT_MAXIMUM_PROGRESS_ITEMS,
      detailedEvents = false,
      maximumDetailedEvents = DEFAULT_MAXIMUM_DETAILED_EVENTS,
      maximumVisualEvidences = DEFAULT_MAXIMUM_VISUAL_EVIDENCES,
      onStatus = () => {}
    }) {
      this.container = container;
      this.emptyState = emptyState;
      this.maximumTurns = maximumTurns;
      this.maximumProgressItems = maximumProgressItems;
      this.detailedEvents = Boolean(detailedEvents);
      this.maximumDetailedEvents = maximumDetailedEvents;
      this.maximumVisualEvidences = maximumVisualEvidences;
      this.onStatus = onStatus;
      this.turns = [];
    }

    startTurn({
      prompt,
      attachmentFile = null,
      agentModel = "",
      reasoningEffort = "",
      vlmModel = ""
    }) {
      this.emptyState?.setAttribute("hidden", "");
      const attachmentUrl = attachmentFile
        ? URL.createObjectURL(attachmentFile)
        : null;
      const state = {
        schema: "midbrain.agent_chat_turn.v1",
        turn_id: globalThis.crypto?.randomUUID?.() ||
          `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        run_id: "",
        prompt: String(prompt ?? ""),
        attachment_name: attachmentFile?.name || "",
        agent_model: text(agentModel),
        reasoning_effort: text(reasoningEffort),
        vlm_model: text(vlmModel),
        started_at: new Date().toISOString(),
        status: "RUNNING",
        activity: "Starting agent run...",
        answer: "",
        reasoning: "",
        progress: [],
        event_details: [],
        visual_evidence: null,
        visual_evidences: []
      };
      const turn = new AgentChatTurn(this, state, {attachmentUrl});
      this.turns.push(turn);
      this.prune();
      this.followIfNearBottom(true);
      return turn;
    }

    prune() {
      while (this.turns.length > this.maximumTurns) {
        this.turns.shift().release();
      }
    }

    nearBottom() {
      return this.container.scrollHeight - this.container.scrollTop -
        this.container.clientHeight < 120;
    }

    followIfNearBottom(force = false) {
      if (!force && !this.nearBottom()) return;
      window.requestAnimationFrame(() => {
        this.container.scrollTop = this.container.scrollHeight;
      });
    }

    persist() {
      // Completed turns are persisted by the robot-local journal, not the tab.
    }

    hydrate(states) {
      if (!Array.isArray(states)) return;
      const wasNearBottom = this.nearBottom();
      const normalizedStates = states
        .slice(-this.maximumTurns)
        .filter(
          (state) =>
            state?.schema === "midbrain.agent_chat_turn.v1" &&
            [
              "RUNNING",
              "COMPLETED",
              "FAILED",
              "CANCELLED",
              "INTERRUPTED"
            ].includes(
              state.status
            )
        )
        .map((state) => {
          const visualEvidences = visualEvidenceList(state).slice(
            -this.maximumVisualEvidences
          );
          return {
            ...state,
            prompt: String(state.prompt ?? ""),
            answer: String(state.answer ?? ""),
            reasoning: String(state.reasoning ?? ""),
            progress: Array.isArray(state.progress)
              ? state.progress.slice(-this.maximumProgressItems)
              : [],
            event_details: Array.isArray(state.event_details)
              ? state.event_details.slice(-this.maximumDetailedEvents)
              : [],
            visual_evidence: visualEvidences.at(-1) || null,
            visual_evidences: visualEvidences
          };
        });

      const serverRunIds = new Set(
        normalizedStates
          .map((state) => text(state.run_id))
          .filter(Boolean)
      );
      const currentTurns = [...this.turns];

      for (const state of normalizedStates) {
        const runId = text(state.run_id);
        if (!runId || currentTurns.some(
          (turn) => text(turn.state.run_id) === runId
        )) continue;
        const pending = currentTurns.find(
          (turn) =>
            turn.localOwner &&
            turn.state.status === "RUNNING" &&
            !text(turn.state.run_id) &&
            turn.state.prompt === state.prompt
        );
        if (pending) pending.setRunId(runId);
      }

      const currentByKey = new Map(
        currentTurns.map((turn) => [turnKey(turn.state), turn])
      );
      const claimed = new Set();
      const reconciled = [];
      for (const state of normalizedStates) {
        const key = turnKey(state);
        const existing = currentByKey.get(key);
        if (!existing) {
          reconciled.push(
            new AgentChatTurn(this, state, {restoring: true})
          );
          continue;
        }
        claimed.add(existing);
        const preserveLocal = existing.localOwner && (
          existing.state.status === "RUNNING" || state.status === "RUNNING"
        );
        if (
          !preserveLocal &&
          existing.serverRevision !== serverStateRevision(state)
        ) {
          existing.updateFromServer(state);
        }
        reconciled.push(existing);
      }

      for (const turn of currentTurns) {
        if (claimed.has(turn)) continue;
        const runId = text(turn.state.run_id);
        const preserveLocal = turn.localOwner && (
          turn.state.status === "RUNNING" || !serverRunIds.has(runId)
        );
        if (preserveLocal) {
          reconciled.push(turn);
        } else {
          turn.release();
        }
      }

      reconciled.sort((left, right) => {
        const leftTime = Date.parse(left.state.started_at) || 0;
        const rightTime = Date.parse(right.state.started_at) || 0;
        if (leftTime !== rightTime) return leftTime - rightTime;
        return turnKey(left.state).localeCompare(turnKey(right.state));
      });
      this.turns = reconciled;
      this.prune();
      for (const turn of this.turns) {
        this.container.appendChild(turn.article);
      }
      if (this.turns.length) {
        this.emptyState?.setAttribute("hidden", "");
        if (wasNearBottom) this.followIfNearBottom(true);
      } else {
        this.emptyState?.removeAttribute("hidden");
      }
    }
  }

  window.MidbrainAgentChatHistory = MidbrainAgentChatHistory;
})();
