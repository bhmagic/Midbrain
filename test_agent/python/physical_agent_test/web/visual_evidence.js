(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const ANNOTATION_PALETTE = [
    "#ffcc33",
    "#3bc9db",
    "#ff6b6b",
    "#69db7c",
    "#b197fc",
    "#ffa94d",
    "#f783ac",
    "#74c0fc"
  ];
  const ANNOTATION_LABEL_HALO = "rgba(0, 0, 0, 0.72)";
  const ANNOTATION_LABEL_WEIGHT = 500;

  class MidbrainVisualEvidenceViewer {
    constructor({onStatus = () => {}, elements = {}} = {}) {
      this.onStatus = onStatus;
      this.evidence = null;
      this.channelId = null;
      const element = (name, id) =>
        elements[name] || document.getElementById(id);
      this.panel = element("panel", "visualEvidencePanel");
      this.title = element("title", "visualEvidenceTitle");
      this.meta = element("meta", "visualEvidenceMeta");
      this.channelButtons = element(
        "channelButtons",
        "visualChannelButtons"
      );
      this.overlayEnabled = element(
        "overlayEnabled",
        "visualOverlayEnabled"
      );
      this.annotationColorControls = element(
        "annotationColorControls",
        "visualAnnotationColors"
      );
      this.resetColorsButton = element(
        "resetColorsButton",
        "resetAnnotationColors"
      );
      this.canvas = element("canvas", "visualCanvas");
      this.image = element("image", "visualEvidenceImage");
      this.overlay = element("overlay", "visualEvidenceOverlay");
      this.copyButton = element("copyButton", "copyAnnotatedImage");
      this.downloadButton = element(
        "downloadButton",
        "downloadAnnotatedImage"
      );
      this.annotationColors = new Map();
      this.available = Boolean(
        this.panel && this.title && this.meta && this.channelButtons &&
        this.overlayEnabled && this.annotationColorControls &&
        this.resetColorsButton && this.canvas && this.image && this.overlay &&
        this.copyButton && this.downloadButton
      );
      if (!this.available) return;
      this.overlayEnabled.addEventListener("change", () => this.render());
      this.resetColorsButton.addEventListener("click", () => {
        this.assignAnnotationColors();
        this.render();
      });
      this.copyButton.addEventListener("click", () => this.copy());
      this.downloadButton.addEventListener("click", () => this.download());
    }

    clear() {
      if (!this.available) return;
      this.evidence = null;
      this.channelId = null;
      this.panel.hidden = true;
      this.image.removeAttribute("src");
      this.overlay.replaceChildren();
      this.channelButtons.replaceChildren();
      this.annotationColorControls.replaceChildren();
      this.annotationColors.clear();
    }

    show(evidence) {
      if (!this.available || !evidence || !Array.isArray(evidence.channels)) {
        return;
      }
      const channels = evidence.channels.filter(
        (channel) => channel && channel.id && channel.url &&
          Number(channel.width) > 0 && Number(channel.height) > 0
      );
      if (!channels.length) return;
      this.evidence = {...evidence, channels};
      this.assignAnnotationColors();
      this.title.textContent = evidence.title || "Visual evidence";
      this.panel.hidden = false;
      this.channelButtons.replaceChildren();
      for (const channel of channels) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = channel.label || channel.id;
        button.dataset.channelId = channel.id;
        button.addEventListener("click", () => this.selectChannel(channel.id));
        this.channelButtons.appendChild(button);
      }
      const requested = channels.some(
        (channel) => channel.id === evidence.default_channel
      ) ? evidence.default_channel : channels[0].id;
      this.selectChannel(requested);
    }

    selectChannel(channelId) {
      const channel = this.channel(channelId);
      if (!channel) return;
      this.channelId = channel.id;
      this.image.src = channel.url;
      this.canvas.style.aspectRatio = `${channel.width} / ${channel.height}`;
      this.overlay.setAttribute(
        "viewBox",
        `0 0 ${Number(channel.width)} ${Number(channel.height)}`
      );
      for (const button of this.channelButtons.querySelectorAll("button")) {
        button.classList.toggle(
          "active",
          button.dataset.channelId === channel.id
        );
      }
      this.render();
    }

    channel(channelId = this.channelId) {
      return this.evidence?.channels.find(
        (candidate) => candidate.id === channelId
      ) || null;
    }

    applicableAnnotations() {
      const annotations = Array.isArray(this.evidence?.annotations)
        ? this.evidence.annotations
        : [];
      return annotations.filter(
        (annotation) => Array.isArray(annotation.applies_to_channels) &&
          annotation.applies_to_channels.includes(this.channelId)
      );
    }

    assignAnnotationColors() {
      this.annotationColors.clear();
      const annotations = Array.isArray(this.evidence?.annotations)
        ? this.evidence.annotations
        : [];
      annotations.forEach((annotation, index) => {
        this.annotationColors.set(
          this.annotationKey(annotation, index),
          ANNOTATION_PALETTE[index % ANNOTATION_PALETTE.length]
        );
      });
    }

    annotationKey(annotation, index = 0) {
      const annotationId = String(annotation?.id || "").trim();
      return annotationId || `annotation-${index + 1}`;
    }

    colorFor(annotation, index = 0) {
      return this.annotationColors.get(this.annotationKey(annotation, index)) ||
        ANNOTATION_PALETTE[index % ANNOTATION_PALETTE.length];
    }

    render() {
      if (!this.available || !this.evidence) return;
      const annotations = this.applicableAnnotations();
      const model = this.evidence.model || "unknown model";
      const confidence = this.evidence.confidence || "unknown";
      this.meta.textContent = `${model} | ${confidence} confidence | ` +
        `${annotations.length} annotation${annotations.length === 1 ? "" : "s"}`;
      this.renderColorControls(annotations);
      this.renderOverlay(annotations);
    }

    renderColorControls(annotations) {
      this.annotationColorControls.replaceChildren();
      this.resetColorsButton.hidden = annotations.length === 0;
      annotations.forEach((annotation, index) => {
        const control = document.createElement("label");
        control.className = "visual-color-control";
        const input = document.createElement("input");
        input.type = "color";
        input.value = this.colorFor(annotation, index);
        input.setAttribute(
          "aria-label",
          `Color for ${String(annotation.label || annotation.type)}`
        );
        const text = document.createElement("span");
        text.textContent = String(annotation.label || annotation.type);
        control.append(input, text);
        input.addEventListener("input", () => {
          this.annotationColors.set(
            this.annotationKey(annotation, index),
            input.value
          );
          this.renderOverlay(this.applicableAnnotations());
        });
        this.annotationColorControls.appendChild(control);
      });
    }

    renderOverlay(annotations) {
      this.overlay.replaceChildren();
      if (!this.overlayEnabled.checked) return;
      annotations.forEach((annotation, index) => {
        const color = this.colorFor(annotation, index);
        if (annotation.type === "point") {
          this.renderPoint(annotation, color);
        } else if (annotation.type === "box") {
          this.renderBox(annotation, color);
        }
      });
    }

    renderPoint(annotation, color) {
      const channel = this.channel();
      if (!channel) return;
      const width = Number(channel.width);
      const height = Number(channel.height);
      const x = Number(annotation.x) * width;
      const y = Number(annotation.y) * height;
      const radius = Math.max(7, width / 80);
      const strokeWidth = Math.max(2, width / 320);
      this.overlay.appendChild(this.svg("circle", {
        cx: x,
        cy: y,
        r: radius,
        fill: "none",
        stroke: color,
        "stroke-width": strokeWidth
      }));
      this.overlay.appendChild(this.svg("line", {
        x1: x - radius * 1.6,
        y1: y,
        x2: x + radius * 1.6,
        y2: y,
        stroke: color,
        "stroke-width": strokeWidth
      }));
      this.overlay.appendChild(this.svg("line", {
        x1: x,
        y1: y - radius * 1.6,
        x2: x,
        y2: y + radius * 1.6,
        stroke: color,
        "stroke-width": strokeWidth
      }));
      this.renderLabel(
        annotation.label,
        x + radius * 1.3,
        Math.max(radius * 1.8, y - radius * 1.3),
        color,
        width
      );
    }

    renderBox(annotation, color) {
      const channel = this.channel();
      if (!channel) return;
      const width = Number(channel.width);
      const height = Number(channel.height);
      const x = Number(annotation.x) * width;
      const y = Number(annotation.y) * height;
      const strokeWidth = Math.max(2, width / 320);
      this.overlay.appendChild(this.svg("rect", {
        x,
        y,
        width: Number(annotation.width) * width,
        height: Number(annotation.height) * height,
        fill: "none",
        stroke: color,
        "stroke-width": strokeWidth
      }));
      this.renderLabel(
        annotation.label,
        x + strokeWidth * 2,
        Math.max(width / 45, y - strokeWidth * 2),
        color,
        width
      );
    }

    renderLabel(label, x, y, color, width) {
      const fontSize = this.labelFontSize(width);
      const text = this.svg("text", {
        x,
        y,
        fill: color,
        stroke: ANNOTATION_LABEL_HALO,
        "stroke-width": this.labelHaloWidth(width),
        "stroke-linejoin": "round",
        "paint-order": "stroke",
        "font-size": fontSize,
        "font-family": "system-ui, sans-serif",
        "font-weight": ANNOTATION_LABEL_WEIGHT
      });
      text.textContent = String(label || "annotation");
      this.overlay.appendChild(text);
    }

    labelFontSize(width) {
      return Math.max(12, Number(width) / 60);
    }

    labelHaloWidth(width) {
      return Math.max(2, Number(width) / 360);
    }

    svg(name, attributes) {
      const element = document.createElementNS(SVG_NS, name);
      for (const [key, value] of Object.entries(attributes)) {
        element.setAttribute(key, String(value));
      }
      return element;
    }

    async flattenedBlob() {
      const channel = this.channel();
      if (!channel) throw new Error("No visual evidence channel is selected");
      if (!this.image.complete) {
        await new Promise((resolve, reject) => {
          this.image.addEventListener("load", resolve, {once: true});
          this.image.addEventListener("error", reject, {once: true});
        });
      }
      const canvas = document.createElement("canvas");
      canvas.width = Number(channel.width);
      canvas.height = Number(channel.height);
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Canvas rendering is unavailable");
      context.drawImage(this.image, 0, 0, canvas.width, canvas.height);
      if (this.overlayEnabled.checked) {
        this.drawCanvasAnnotations(context, canvas.width, canvas.height);
      }
      return await new Promise((resolve, reject) => {
        canvas.toBlob(
          (blob) => blob ? resolve(blob) : reject(new Error("PNG export failed")),
          "image/png"
        );
      });
    }

    drawCanvasAnnotations(context, width, height) {
      const lineWidth = Math.max(2, width / 320);
      context.lineWidth = lineWidth;
      context.font = `${ANNOTATION_LABEL_WEIGHT} ` +
        `${this.labelFontSize(width)}px system-ui, sans-serif`;
      this.applicableAnnotations().forEach((annotation, index) => {
        const color = this.colorFor(annotation, index);
        context.strokeStyle = color;
        context.fillStyle = color;
        const x = Number(annotation.x) * width;
        const y = Number(annotation.y) * height;
        if (annotation.type === "point") {
          const radius = Math.max(7, width / 80);
          context.beginPath();
          context.arc(x, y, radius, 0, Math.PI * 2);
          context.moveTo(x - radius * 1.6, y);
          context.lineTo(x + radius * 1.6, y);
          context.moveTo(x, y - radius * 1.6);
          context.lineTo(x, y + radius * 1.6);
          context.stroke();
        } else if (annotation.type === "box") {
          context.strokeRect(
            x,
            y,
            Number(annotation.width) * width,
            Number(annotation.height) * height
          );
        }
        this.drawCanvasLabel(
          context,
          String(annotation.label || "annotation"),
          x + lineWidth * 2,
          Math.max(context.measureText("M").actualBoundingBoxAscent, y - lineWidth * 2),
          color
        );
      });
    }

    drawCanvasLabel(context, label, x, y, color) {
      context.save();
      context.lineWidth = this.labelHaloWidth(context.canvas.width);
      context.lineJoin = "round";
      context.strokeStyle = ANNOTATION_LABEL_HALO;
      context.strokeText(label, x, y);
      context.fillStyle = color;
      context.fillText(label, x, y);
      context.restore();
    }

    async copy() {
      try {
        const blob = await this.flattenedBlob();
        if (!navigator.clipboard?.write || typeof ClipboardItem === "undefined") {
          this.downloadBlob(blob);
          this.onStatus("Clipboard image copy is unavailable; downloaded PNG instead.");
          return;
        }
        await navigator.clipboard.write([new ClipboardItem({"image/png": blob})]);
        this.onStatus("Annotated image copied to the clipboard.");
      } catch (error) {
        this.onStatus(`Could not copy annotated image: ${error.message || error}`);
      }
    }

    async download() {
      try {
        this.downloadBlob(await this.flattenedBlob());
        this.onStatus("Annotated PNG downloaded.");
      } catch (error) {
        this.onStatus(`Could not export annotated image: ${error.message || error}`);
      }
    }

    downloadBlob(blob) {
      const link = document.createElement("a");
      const evidenceId = String(this.evidence?.evidence_id || "evidence")
        .replace(/[^A-Za-z0-9._-]/g, "_");
      const channelId = String(this.channelId || "image")
        .replace(/[^A-Za-z0-9._-]/g, "_");
      const objectUrl = URL.createObjectURL(blob);
      link.href = objectUrl;
      link.download = `midbrain-${evidenceId}-${channelId}-annotated.png`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    }
  }

  window.MidbrainVisualEvidenceViewer = MidbrainVisualEvidenceViewer;
})();
