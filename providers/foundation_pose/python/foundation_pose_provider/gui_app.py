"""Native Midbrain VLM + SAM2 + FoundationPose tracking GUI."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from .bounding_box import BoundingBoxMask
from .midbrain_client import MidbrainClient, load_env_file
from .openai_detector import DEFAULT_MODEL as DEFAULT_OPENAI_MODEL
from .openai_detector import OpenAIVisionDetector
from .sam2_segmenter import MaskResult, Sam2Segmenter
from .vlm_detection import Detection, NormalizedPoint, TARGETS


COLORS = {
    "robot_arm_root": (255, 186, 48),
    "robot_gripper_slider_support": (53, 208, 255),
}
SHORT_NAMES = {
    "robot_arm_root": "Base",
    "robot_gripper_slider_support": "Gripper",
}


def quaternion_matrix(quaternion_xyzw: list[float]) -> np.ndarray:
    x, y, z, w = (float(value) for value in quaternion_xyzw)
    norm = x * x + y * y + z * z + w * w
    if norm <= 1e-12:
        return np.eye(3, dtype=np.float64)
    scale = 2.0 / norm
    return np.array(
        [
            [1.0 - scale * (y * y + z * z), scale * (x * y - z * w), scale * (x * z + y * w)],
            [scale * (x * y + z * w), 1.0 - scale * (x * x + z * z), scale * (y * z - x * w)],
            [scale * (x * z - y * w), scale * (y * z + x * w), 1.0 - scale * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def draw_pose_axes(
    image: np.ndarray,
    pose: dict[str, Any],
    calibration: dict[str, Any] | None,
    *,
    axis_length_m: float = 0.08,
) -> None:
    intrinsic = (calibration or {}).get("rgb_intrinsic")
    translation = pose.get("translation_m")
    quaternion = pose.get("quaternion_xyzw")
    if not isinstance(intrinsic, dict) or not isinstance(translation, list) or not isinstance(quaternion, list):
        return
    if len(translation) != 3 or len(quaternion) != 4:
        return
    origin = np.asarray(translation, dtype=np.float64)
    rotation = quaternion_matrix(quaternion)
    points = np.vstack([origin, origin + rotation[:, 0] * axis_length_m, origin + rotation[:, 1] * axis_length_m, origin + rotation[:, 2] * axis_length_m])
    if np.any(points[:, 2] <= 1e-4):
        return
    fx, fy = float(intrinsic["fx"]), float(intrinsic["fy"])
    cx, cy = float(intrinsic["cx"]), float(intrinsic["cy"])
    pixels = np.column_stack(
        [
            fx * points[:, 0] / points[:, 2] + cx,
            fy * points[:, 1] / points[:, 2] + cy,
        ]
    )
    pixels = np.rint(pixels).astype(int)
    origin_px = tuple(pixels[0])
    for endpoint, color in zip(pixels[1:], ((255, 70, 70), (70, 255, 100), (80, 140, 255))):
        cv2.line(image, origin_px, tuple(endpoint), color, 4, cv2.LINE_AA)
    cv2.circle(image, origin_px, 6, (255, 255, 255), -1, cv2.LINE_AA)


class TrackingGui:
    def __init__(
        self,
        root: Any,
        client: MidbrainClient,
        provider_root: Path,
        openai_api_key: str,
        openai_model: str,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.client = client
        self.provider_root = provider_root
        self.openai_api_key = openai_api_key
        self.openai_model = openai_model
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="foundation-pose-gui")
        self.shutdown_event = threading.Event()
        self.workspace_started = False
        self.last_frame: np.ndarray | None = None
        self.review_frame: np.ndarray | None = None
        self.review_frame_number = -1
        self.last_frame_number = -1
        self.calibration: dict[str, Any] | None = None
        self.poses: dict[str, dict[str, Any]] = {}
        self.boxes: dict[str, BoundingBoxMask] = {}
        self.points: dict[str, tuple[NormalizedPoint, NormalizedPoint]] = {}
        self.masks: dict[str, np.ndarray] = {}
        self.sessions: dict[str, str] = {}
        self.cad_references: dict[str, bytes] | None = None
        self.photo: Any = None
        self.poll_future: concurrent.futures.Future[Any] | None = None
        self.drag_start: tuple[int, int] | None = None
        self.point_capture: list[tuple[int, int]] | None = None
        self.display_transform = (1.0, 0, 0, 1, 1)

        self.root.title("Midbrain FoundationPose — VLM + SAM2 Initialization")
        self.root.geometry("1320x820")
        self.root.minsize(1080, 680)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.status_var = tk.StringVar(value="Workspace stopped")
        self.target_var = tk.StringVar(value="robot_arm_root")
        self.duration_var = tk.StringVar(value="3600")
        self.base_rate_var = tk.StringVar(value="10.0")
        self.gripper_rate_var = tk.StringVar(value="3.0")
        self.crop_expansion_var = tk.StringVar(value="0.50")
        self.box_text = {
            model_id: tk.StringVar(value=f"{SHORT_NAMES[model_id]}: not selected")
            for model_id in TARGETS
        }
        self.pose_text = {
            model_id: tk.StringVar(value=f"{SHORT_NAMES[model_id]}: idle")
            for model_id in TARGETS
        }

        self._build_ui()
        self.root.after(150, self._poll_tick)

    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(toolbar, text="1  Start Midbrain + Providers", command=self.start_stack).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="2  Ask VLM", command=self.detect_boxes).pack(side=tk.LEFT, padx=6)
        ttk.Button(toolbar, text="3  Make SAM2 Masks", command=self.generate_masks).pack(side=tk.LEFT, padx=6)
        ttk.Button(toolbar, text="4  Start Tracking", command=self.start_tracking).pack(side=tk.LEFT, padx=6)
        ttk.Button(toolbar, text="Stop Tracking", command=self.stop_tracking).pack(side=tk.LEFT, padx=6)
        ttk.Button(toolbar, text="Stop Workspace", command=self.stop_workspace).pack(side=tk.LEFT, padx=6)
        ttk.Label(toolbar, textvariable=self.status_var).pack(side=tk.RIGHT)

        content = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        content.pack(fill=tk.BOTH, expand=True)
        camera_panel = ttk.Frame(content)
        side = ttk.Frame(content, width=315)
        content.add(camera_panel, weight=4)
        content.add(side, weight=1)

        self.canvas = tk.Canvas(camera_panel, width=960, height=540, background="#111827", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)

        detector = ttk.LabelFrame(side, text="OpenAI Luna proposal", padding=10)
        detector.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            detector,
            text="GPT-5.6 Luna returns one box and two foreground points per target.",
            wraplength=280,
        ).pack(anchor=tk.W)

        manual = ttk.LabelFrame(side, text="Box + positive-point review", padding=10)
        manual.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            manual,
            text="Drag to replace the selected box. SAM2 uses the two + points as guaranteed foreground.",
            wraplength=280,
        ).pack(anchor=tk.W, pady=(0, 6))
        for model_id in TARGETS:
            ttk.Radiobutton(manual, text=SHORT_NAMES[model_id], value=model_id, variable=self.target_var).pack(anchor=tk.W)
            ttk.Label(manual, textvariable=self.box_text[model_id], wraplength=280).pack(anchor=tk.W, padx=(20, 0), pady=(0, 5))
        manual_buttons = ttk.Frame(manual)
        manual_buttons.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(
            manual_buttons, text="Set 2 points", command=self.begin_point_capture
        ).pack(side=tk.LEFT)
        ttk.Button(
            manual_buttons, text="Clear target", command=self.clear_selected_box
        ).pack(side=tk.LEFT, padx=(6, 0))
        crop_row = ttk.Frame(manual)
        crop_row.pack(fill=tk.X, pady=(7, 0))
        ttk.Label(crop_row, text="SAM crop expansion").pack(side=tk.LEFT)
        ttk.Entry(crop_row, textvariable=self.crop_expansion_var, width=7).pack(side=tk.RIGHT)

        settings = ttk.LabelFrame(side, text="Tracking request", padding=10)
        settings.pack(fill=tk.X, pady=8)
        duration_row = ttk.Frame(settings)
        duration_row.pack(fill=tk.X, pady=2)
        ttk.Label(duration_row, text="Duration (s)").pack(side=tk.LEFT)
        ttk.Entry(duration_row, textvariable=self.duration_var, width=10).pack(side=tk.RIGHT)
        base_rate_values = ("0.5", "1.0", "2.0", "3.0", "5.0", "8.0", "10.0")
        gripper_rate_values = base_rate_values + ("15.0", "20.0", "30.0", "45.0", "60.0")
        for label, variable, rate_values in (
            ("Base rate limit (Hz)", self.base_rate_var, base_rate_values),
            ("Gripper rate limit (Hz)", self.gripper_rate_var, gripper_rate_values),
        ):
            row = ttk.Frame(settings)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label).pack(side=tk.LEFT)
            ttk.Combobox(
                row,
                textvariable=variable,
                state="readonly",
                values=rate_values,
                width=7,
            ).pack(side=tk.RIGHT)

        tracking = ttk.LabelFrame(side, text="Tracking results", padding=10)
        tracking.pack(fill=tk.X, pady=8)
        for model_id in TARGETS:
            ttk.Label(tracking, textvariable=self.pose_text[model_id], wraplength=280).pack(anchor=tk.W, pady=3)

        logs = ttk.LabelFrame(side, text="Activity", padding=6)
        logs.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.log_text = tk.Text(logs, height=12, wrap=tk.WORD, state=tk.DISABLED, background="#f8fafc")
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state=self.tk.NORMAL)
        self.log_text.insert(self.tk.END, f"[{stamp}] {message}\n")
        self.log_text.see(self.tk.END)
        self.log_text.configure(state=self.tk.DISABLED)

    def _run_task(
        self,
        label: str,
        work: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
    ) -> None:
        self.status_var.set(label)
        self.log(label)
        future = self.executor.submit(work)

        def check() -> None:
            if not future.done():
                self.root.after(100, check)
                return
            try:
                result = future.result()
            except Exception as error:
                self.status_var.set("Error")
                self.log(f"ERROR: {error}")
            else:
                self.status_var.set("Ready")
                if on_success:
                    on_success(result)

        self.root.after(100, check)

    def start_stack(self) -> None:
        def work() -> None:
            self.client.start_workspace()
            self.workspace_started = True
            self.client.start_tracking_stack()

        def done(_result: Any) -> None:
            self.workspace_started = True
            self.log("Fabric, Manager, RGB-D camera, and FoundationPose are HOT.")

        self._run_task("Starting Midbrain and providers…", work, done)

    def _build_cad_references(self) -> dict[str, bytes]:
        if self.cad_references is not None:
            return self.cad_references
        reference_root = (
            self.provider_root / "defaults" / "rebot_b601_dm" / "references"
        )
        paths = {
            "robot_arm_root": reference_root / "Base_reference_atlas.png",
            "robot_gripper_slider_support": reference_root
            / "Gripper_reference_atlas.png",
        }
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Bundled CAD reference images are missing: " + ", ".join(missing)
            )
        references = {model_id: path.read_bytes() for model_id, path in paths.items()}
        self.cad_references = references
        return references

    def detect_boxes(self) -> None:
        if self.last_frame is None:
            self.log("ERROR: No camera frame is available yet.")
            return
        frame = self.last_frame.copy()
        frame_number = self.last_frame_number
        def work() -> dict[str, Detection]:
            references = self._build_cad_references()
            detector = OpenAIVisionDetector(
                self.openai_api_key, model=self.openai_model
            )
            try:
                return detector.detect(frame, references)
            finally:
                detector.close()

        def done(detections: dict[str, Detection]) -> None:
            self.review_frame = frame
            self.review_frame_number = frame_number
            self.boxes = {
                model_id: detection.box for model_id, detection in detections.items()
            }
            self.points = {
                model_id: detection.positive_points
                for model_id, detection in detections.items()
            }
            self.masks.clear()
            details = "; ".join(
                f"{SHORT_NAMES[model_id]} box={list(map(lambda value: round(value), detection.box.box_2d))}"
                for model_id, detection in detections.items()
            )
            self.log(f"Luna proposal: {details}")
            self.log("Review the frozen frame, boxes, and two positive points before making masks.")
            self._refresh_box_labels()
            self._render()

        self._run_task(f"Asking {self.openai_model} for boxes and points…", work, done)

    def generate_masks(self) -> None:
        frame = self.review_frame
        if frame is None:
            self.log("ERROR: Ask a VLM first so segmentation uses a frozen review frame.")
            return
        missing = [
            SHORT_NAMES[model_id]
            for model_id in TARGETS
            if model_id not in self.boxes or model_id not in self.points
        ]
        if missing:
            self.log("ERROR: Missing a box or two positive points for " + ", ".join(missing))
            return
        try:
            expansion = float(self.crop_expansion_var.get())
            if not 0.0 <= expansion <= 2.0:
                raise ValueError
        except ValueError:
            self.log("ERROR: SAM crop expansion must be between 0 and 2.")
            return
        boxes, points = dict(self.boxes), dict(self.points)

        def work() -> dict[str, MaskResult]:
            with Sam2Segmenter(self.provider_root) as segmenter:
                return {
                    model_id: segmenter.segment(
                        frame,
                        boxes[model_id],
                        points[model_id],
                        crop_expansion=expansion,
                        color_refine=True,
                        color_refine_space=(
                            "lab" if model_id == "robot_arm_root" else "rgb"
                        ),
                        color_tolerance_fraction=0.10,
                        lab_distance_threshold=30.0,
                        dilation_radius=2,
                    )
                    for model_id in TARGETS
                }

        def done(results: dict[str, MaskResult]) -> None:
            self.masks = {model_id: result.mask for model_id, result in results.items()}
            capture_root = self.provider_root / "debug" / "mask_reviews"
            capture_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
            capture_dir = capture_root / capture_id
            capture_dir.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(
                str(capture_dir / "review_rgb.png"),
                cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
            )
            metadata: dict[str, Any] = {
                "capture_id": capture_id,
                "camera_frame_number": self.review_frame_number,
                "image_shape": list(frame.shape),
                "crop_expansion": expansion,
                "models": {},
            }
            for model_id, result in results.items():
                cv2.imwrite(
                    str(capture_dir / f"{model_id}_sam_seed.png"),
                    result.sam_mask.astype(np.uint8) * 255,
                )
                cv2.imwrite(
                    str(capture_dir / f"{model_id}_final_mask.png"),
                    result.mask.astype(np.uint8) * 255,
                )
                metadata["models"][model_id] = {
                    "box": self.boxes[model_id].public_payload(),
                    "positive_points_2d": [
                        point.public_payload() for point in self.points[model_id]
                    ],
                    "sam_predicted_iou": result.predicted_iou,
                    "sam_pixel_count": result.sam_pixel_count,
                    "final_pixel_count": int(result.mask.sum()),
                    "refinement_method": result.refinement_method,
                    "median_rgb": result.median_rgb,
                    "median_lab": result.median_lab,
                    "crop_yxyx_pixels": [
                        result.crop.y0,
                        result.crop.x0,
                        result.crop.y1,
                        result.crop.x1,
                    ],
                }
                self.log(
                    f"SAM2 {SHORT_NAMES[model_id]}: score={result.predicted_iou:.3f}, "
                    f"pixels={result.sam_pixel_count}→{int(result.mask.sum())}, "
                    f"refinement={result.refinement_method}"
                    + (f", median RGB={result.median_rgb}." if result.median_rgb else "")
                    + (f", median Lab={result.median_lab}." if result.median_lab else "")
                )
            (capture_dir / "metadata.json").write_text(
                json.dumps(metadata, indent=2), encoding="utf-8"
            )
            self.log(f"Saved exact mask-review bundle: {capture_dir}")
            self.log("Inspect the mask overlays. Keep the robot still, then start tracking.")
            self._refresh_box_labels()
            self._render()

        self._run_task("Generating cropped SAM2 masks…", work, done)

    def start_tracking(self) -> None:
        missing = [SHORT_NAMES[model_id] for model_id in TARGETS if model_id not in self.masks]
        if missing:
            self.log("ERROR: Missing reviewed SAM2 masks for " + ", ".join(missing))
            return
        try:
            duration = float(self.duration_var.get())
            rates = {
                "robot_arm_root": float(self.base_rate_var.get()),
                "robot_gripper_slider_support": float(self.gripper_rate_var.get()),
            }
            if duration <= 0 or any(rate <= 0 for rate in rates.values()):
                raise ValueError
        except ValueError:
            self.log("ERROR: Duration and update rate must be positive.")
            return

        masks = {model_id: mask.copy() for model_id, mask in self.masks.items()}
        old_sessions = dict(self.sessions)

        def work() -> dict[str, str]:
            for session_id in old_sessions.values():
                try:
                    self.client.pose_request("stop", {"session_id": session_id, "reason": "GUI replacing tracking session"})
                except Exception:
                    pass
            accepted: dict[str, str] = {}
            mask_dir = self.provider_root / "debug" / "gui_masks"
            mask_dir.mkdir(parents=True, exist_ok=True)
            run_id = uuid.uuid4().hex
            for model_id in TARGETS:
                session_id = f"sam2-{SHORT_NAMES[model_id].lower()}-{uuid.uuid4()}"
                mask_path = mask_dir / f"{run_id}_{model_id}.png"
                if not cv2.imwrite(str(mask_path), masks[model_id].astype(np.uint8) * 255):
                    raise RuntimeError(f"Could not write initialization mask: {mask_path}")
                payload = {
                    "session_id": session_id,
                    "model_id": model_id,
                    "target_id": model_id,
                    "mask_path": str(mask_path.resolve()),
                    "max_duration_s": duration,
                    "max_update_hz": rates[model_id],
                }
                response = self.client.pose_request("track", payload)
                accepted[model_id] = str(response["session_id"])
            return accepted

        def done(sessions: dict[str, str]) -> None:
            self.sessions = sessions
            self.review_frame = None
            for model_id, session_id in sessions.items():
                self.pose_text[model_id].set(f"{SHORT_NAMES[model_id]}: initializing ({session_id[:18]}…)")
            self.log("Base and Gripper TRACK sessions accepted with SAM2 mask PNGs. Initial registration is serialized and may take about 1–2 minutes total.")
            self._render()

        self._run_task("Submitting TRACK requests through Manager…", work, done)

    def stop_tracking(self) -> None:
        sessions = dict(self.sessions)
        if not sessions:
            self.log("No GUI-owned tracking sessions are active.")
            return

        def work() -> None:
            errors = []
            for session_id in sessions.values():
                try:
                    self.client.pose_request("stop", {"session_id": session_id, "reason": "Stopped from GUI"})
                except Exception as error:
                    errors.append(str(error))
            if errors:
                raise RuntimeError("; ".join(errors))

        def done(_result: Any) -> None:
            self.sessions.clear()
            for model_id in TARGETS:
                self.pose_text[model_id].set(f"{SHORT_NAMES[model_id]}: stopped")
            self.log("Tracking sessions stopped.")

        self._run_task("Stopping tracking sessions…", work, done)

    def stop_workspace(self) -> None:
        def done(_result: Any) -> None:
            self.workspace_started = False
            self.sessions.clear()
            self.status_var.set("Workspace stopped")
            self.log("Workspace stopped cleanly.")

        self._run_task("Stopping Midbrain workspace…", self.client.stop_workspace, done)

    def _poll_tick(self) -> None:
        if self.shutdown_event.is_set():
            return
        if self.poll_future is None:
            self.poll_future = self.executor.submit(self._fetch_snapshot)
        elif self.poll_future.done():
            try:
                snapshot = self.poll_future.result()
            except Exception:
                pass
            else:
                if snapshot.get("frame") is not None:
                    self.last_frame, self.last_frame_number = snapshot["frame"]
                if snapshot.get("calibration") is not None:
                    self.calibration = snapshot["calibration"]
                self.poses = snapshot.get("poses") or self.poses
                self._update_pose_labels()
                self._render()
            self.poll_future = None
        self.root.after(150, self._poll_tick)

    def _fetch_snapshot(self) -> dict[str, Any]:
        frame = None
        try:
            frame = self.client.camera_frame()
        except Exception:
            frame = None
        try:
            calibration = self.client.calibration()
        except Exception:
            calibration = None
        try:
            poses = self.client.latest_pose_by_model()
        except Exception:
            poses = {}
        return {"frame": frame, "calibration": calibration, "poses": poses}

    def _update_pose_labels(self) -> None:
        for model_id in TARGETS:
            pose = self.poses.get(model_id)
            expected_session = self.sessions.get(model_id)
            if (
                pose is None
                or not expected_session
                or pose.get("tracking_session_id") != expected_session
            ):
                continue
            translation = pose.get("translation_m") or []
            if len(translation) == 3:
                xyz = ", ".join(f"{float(value):+.3f}" for value in translation)
                latency = pose.get("latency_ms")
                self.pose_text[model_id].set(
                    f"{SHORT_NAMES[model_id]}: {pose.get('tracking_state', 'TRACKING')}\nxyz m: {xyz}\nlatency: {float(latency):.1f} ms" if latency is not None else f"{SHORT_NAMES[model_id]}: tracking\nxyz m: {xyz}"
                )

    def _render(self) -> None:
        if self.last_frame is None:
            return
        reviewing = self.review_frame is not None
        image = (self.review_frame if reviewing else self.last_frame).copy()
        height, width = image.shape[:2]
        if reviewing:
            overlay = image.copy()
            for model_id, mask in self.masks.items():
                if mask.shape == image.shape[:2]:
                    overlay[mask] = COLORS[model_id]
            if self.masks:
                image = cv2.addWeighted(image, 0.62, overlay, 0.38, 0.0)
            for model_id, box in self.boxes.items():
                box_mask = box.to_mask(height, width)
                ys, xs = np.where(box_mask)
                if len(xs):
                    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
                    color = COLORS[model_id]
                    cv2.rectangle(image, (x0, y0), (x1, y1), color, 4, cv2.LINE_AA)
                    cv2.putText(image, SHORT_NAMES[model_id], (x0 + 6, max(28, y0 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 3, cv2.LINE_AA)
                for point in self.points.get(model_id, ()):
                    y, x = point.to_pixel_yx(height, width)
                    center = (min(width - 1, max(0, round(x))), min(height - 1, max(0, round(y))))
                    cv2.drawMarker(image, center, (255, 255, 255), cv2.MARKER_CROSS, 22, 6, cv2.LINE_AA)
                    cv2.drawMarker(image, center, COLORS[model_id], cv2.MARKER_CROSS, 16, 3, cv2.LINE_AA)
        for model_id, pose in self.poses.items():
            expected_session = self.sessions.get(model_id)
            if expected_session and pose.get("tracking_session_id") == expected_session:
                draw_pose_axes(image, pose, self.calibration)

        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        scale = min(canvas_width / width, canvas_height / height)
        display_width = max(1, int(width * scale))
        display_height = max(1, int(height * scale))
        resized = cv2.resize(image, (display_width, display_height), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".png", cv2.cvtColor(resized, cv2.COLOR_RGB2BGR))
        if not ok:
            return
        self.photo = self.tk.PhotoImage(data=base64.b64encode(encoded.tobytes()).decode("ascii"))
        offset_x = (canvas_width - display_width) // 2
        offset_y = (canvas_height - display_height) // 2
        self.display_transform = (scale, offset_x, offset_y, width, height)
        self.canvas.delete("all")
        self.canvas.create_image(offset_x, offset_y, anchor=self.tk.NW, image=self.photo)
        frame_label = "Frozen VLM/SAM review frame" if reviewing else f"Camera frame {self.last_frame_number}"
        self.canvas.create_text(12, 12, anchor=self.tk.NW, fill="white", text=frame_label, font=("Segoe UI", 10, "bold"))

    def _drag_begin(self, event: Any) -> None:
        point = self._canvas_to_image(event.x, event.y)
        if self.point_capture is not None:
            if point is not None:
                self.point_capture.append(point)
                if len(self.point_capture) == 2:
                    self._finish_point_capture()
            return
        self.drag_start = point

    def _drag_move(self, event: Any) -> None:
        if self.drag_start is None:
            return
        self.canvas.delete("manual-drag")
        scale, offset_x, offset_y, _, _ = self.display_transform
        x0, y0 = self.drag_start
        color = "#ffba30" if self.target_var.get() == "robot_arm_root" else "#35d0ff"
        self.canvas.create_rectangle(offset_x + x0 * scale, offset_y + y0 * scale, event.x, event.y, outline=color, width=3, tags="manual-drag")

    def _drag_end(self, event: Any) -> None:
        if self.drag_start is None:
            return
        end = self._canvas_to_image(event.x, event.y)
        start = self.drag_start
        self.drag_start = None
        self.canvas.delete("manual-drag")
        if end is None or start is None:
            return
        x0, x1 = sorted((start[0], end[0]))
        y0, y1 = sorted((start[1], end[1]))
        _, _, _, width, height = self.display_transform
        if x1 - x0 < 5 or y1 - y0 < 5:
            return
        box = BoundingBoxMask((1000.0 * y0 / height, 1000.0 * x0 / width, 1000.0 * y1 / height, 1000.0 * x1 / width))
        model_id = self.target_var.get()
        self.boxes[model_id] = box
        self.points.pop(model_id, None)
        self.masks.pop(model_id, None)
        self.log(f"{SHORT_NAMES[model_id]} box replaced; set two positive points again.")
        self._refresh_box_labels()
        self._render()

    def _canvas_to_image(self, x: int, y: int) -> tuple[int, int] | None:
        scale, offset_x, offset_y, width, height = self.display_transform
        image_x = int((x - offset_x) / scale)
        image_y = int((y - offset_y) / scale)
        if not 0 <= image_x < width or not 0 <= image_y < height:
            return None
        return image_x, image_y

    def clear_selected_box(self) -> None:
        model_id = self.target_var.get()
        self.boxes.pop(model_id, None)
        self.points.pop(model_id, None)
        self.masks.pop(model_id, None)
        self._refresh_box_labels()
        self._render()

    def begin_point_capture(self) -> None:
        model_id = self.target_var.get()
        if model_id not in self.boxes:
            self.log(f"ERROR: Set the {SHORT_NAMES[model_id]} box first.")
            return
        self.point_capture = []
        self.log(f"Click two definitely-inside points on {SHORT_NAMES[model_id]}.")

    def _finish_point_capture(self) -> None:
        captured = self.point_capture
        self.point_capture = None
        if captured is None or len(captured) != 2:
            return
        _, _, _, width, height = self.display_transform
        model_id = self.target_var.get()
        points = tuple(
            NormalizedPoint(1000.0 * y / height, 1000.0 * x / width)
            for x, y in captured
        )
        ymin, xmin, ymax, xmax = self.boxes[model_id].box_2d
        if any(not (ymin <= point.y <= ymax and xmin <= point.x <= xmax) for point in points):
            self.log("ERROR: Both positive points must be inside the selected box.")
            return
        if abs(points[0].x - points[1].x) + abs(points[0].y - points[1].y) < 1.0:
            self.log("ERROR: Positive points must be distinct.")
            return
        self.points[model_id] = (points[0], points[1])
        self.masks.pop(model_id, None)
        self._refresh_box_labels()
        self._render()

    def _refresh_box_labels(self) -> None:
        for model_id in TARGETS:
            box = self.boxes.get(model_id)
            if box is None:
                self.box_text[model_id].set(f"{SHORT_NAMES[model_id]}: not selected")
            else:
                coordinates = ", ".join(str(int(round(value))) for value in box.box_2d)
                point_state = "2 points" if model_id in self.points else "points missing"
                mask_state = ", mask ready" if model_id in self.masks else ""
                self.box_text[model_id].set(f"{SHORT_NAMES[model_id]} yxyx/1000: [{coordinates}]\n{point_state}{mask_state}")

    def on_close(self) -> None:
        from tkinter import messagebox

        if self.workspace_started and messagebox.askyesno("Stop workspace?", "Stop the Midbrain workspace before closing?"):
            try:
                self.client.stop_workspace()
            except Exception:
                pass
        self.shutdown_event.set()
        self.client.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-url", default="http://127.0.0.1:7001")
    parser.add_argument("--fabric-url", default="http://127.0.0.1:7002")
    parser.add_argument("--openai-model")
    return parser.parse_args()


def main() -> int:
    import tkinter as tk

    args = parse_args()
    provider_root = Path(__file__).resolve().parents[2]
    workspace = provider_root.parents[1]
    env_values = load_env_file(workspace / "config" / "api_keys.env")
    openai_api_key = env_values.get("OPENAI_API_KEY", "")
    openai_model = (
        args.openai_model
        or env_values.get("OPENAI_VISION_MODEL")
        or DEFAULT_OPENAI_MODEL
    )
    client = MidbrainClient(workspace, manager_url=args.manager_url, fabric_url=args.fabric_url)
    root = tk.Tk()
    TrackingGui(
        root,
        client,
        provider_root,
        openai_api_key,
        openai_model,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
