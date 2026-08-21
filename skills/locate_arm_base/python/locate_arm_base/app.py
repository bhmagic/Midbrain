from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import threading
from typing import Any

from .skill import LocateArmBaseSkill


DEVELOPER_SHUTDOWN_CONFIRMATION = "STOP_LOCATE_ARM_BASE_DEVELOPER_UI"


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Locate Arm Base Development</title><style>
:root{color-scheme:dark;--bg:#090909;--panel:#131313;--raised:#1c1c1c;--line:#3b3b3b;--muted:#a8a8a8;--text:#f2f2f2;--accent:#e5e5e5;--warn:#f2b84b}
*{box-sizing:border-box}body{font:14px/1.45 system-ui;margin:0;background:radial-gradient(circle at 78% -15%,rgba(255,255,255,.08),transparent 34rem),var(--bg);color:var(--text)}main{max-width:1440px;margin:auto;padding:26px}
h1{margin:0;font-size:28px}h2{margin:0 0 12px;font-size:18px}p{color:var(--muted);margin:7px 0 14px}.top{display:flex;justify-content:space-between;gap:20px;align-items:start}.badge{padding:6px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}
.grid{display:grid;grid-template-columns:minmax(360px,0.85fr) minmax(500px,1.5fr);gap:18px;margin-top:20px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:17px;min-width:0}.wide{grid-column:1/-1}
label{display:block;color:var(--muted);font-size:12px;margin:12px 0 5px}input,textarea,pre{width:100%;border:1px solid var(--line);border-radius:8px;background:#0d0d0d;color:var(--text);padding:10px;font:13px/1.45 ui-monospace,Consolas,monospace}textarea{min-height:110px;resize:vertical}#vlmSeedGuidance{min-height:145px}#appendix{min-height:360px}#result{min-height:170px;max-height:480px;overflow:auto;white-space:pre-wrap}
button{border:1px solid var(--line);border-radius:8px;background:var(--accent);color:#090909;font-weight:700;padding:10px 15px;margin:12px 8px 0 0;cursor:pointer}button.secondary{background:var(--raised);color:var(--text)}button:disabled{opacity:.45;cursor:wait}.warning{color:var(--warn)}.meta{display:grid;grid-template-columns:150px 1fr;gap:6px 12px}.meta span:nth-child(odd){color:var(--muted)}code{color:#d5d5d5;overflow-wrap:anywhere}
.images{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}.image{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#111}.image img{display:block;width:100%;height:230px;object-fit:contain;background:#050505}.caption{padding:10px}.caption b{display:block;overflow-wrap:anywhere;word-break:break-word}.caption small{display:block;color:var(--muted);overflow-wrap:anywhere;margin-top:3px}.empty{color:var(--muted);padding:24px;border:1px dashed var(--line);border-radius:9px}
details{margin-top:14px;border:1px solid var(--line);border-radius:8px;padding:10px;background:#0d0d0d}summary{cursor:pointer;color:var(--muted);font-weight:650}details[open] summary{margin-bottom:10px}
@media(max-width:900px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}.top{display:block}.badge{display:inline-block;margin-top:12px}}
</style></head><body><main>
<div class="top"><div><h1>Locate Arm Base</h1><p>Inspect the exact profile, mask candidates, CAD fits, VLM selections, and bounded orientation evidence used by one finite run.</p></div><div class="badge" id="runState">Loading…</div></div>
<div class="grid">
<section class="card"><h2>Selected arm profile</h2><div class="meta" id="profileMeta"></div><p class="warning">Saving changes updates the selected arm-model appendix. Restart the arm Provider before running so the active assembly digest can match.</p>
<label for="cadPath">FoundationPose CAD workspace path</label><input id="cadPath">
<label for="cadScale">CAD scale to metres</label><input id="cadScale" type="number" step="any">
<label for="references">VLM reference image workspace paths, one per line</label><textarea id="references"></textarea>
<label for="vlmSeedGuidance">Additional first-VLM target guidance</label><textarea id="vlmSeedGuidance" maxlength="2000"></textarea><p>This arm-specific text is sent to every independent VLM seed-localization attempt before SAM2. It is saved in this arm profile appendix.</p>
<details><summary>Advanced profile JSON</summary><label for="appendix">Flexible appendix JSON (unknown field names are preserved)</label><textarea id="appendix"></textarea></details>
<button id="saveProfile">Save arm-profile appendix</button><button class="secondary" id="reloadProfile">Reload</button><pre id="profileResult">Ready.</pre></section>
<section class="card"><h2>Finite run</h2><p>The default limited test runs independent VLM point prompts through independent SAM2 calls, retains every successfully acquired mask, pixel-votes the complete ensemble, dilates once, repeats FoundationPose on that one final mask, selects the best fit, and resolves bounded orientation from one coarse active-effector point plus timestamped FK without requiring a world axis or publishing calibration.</p><label for="maskCount">Independent VLM→SAM2 mask attempts</label><input id="maskCount" type="number" min="1" max="8" step="1" value="2"><label for="fitCount">FoundationPose fit count</label><input id="fitCount" type="number" min="1" max="8" step="1" value="2"><p>The two counts are independent and apply only to this run. Every pose fit reuses the single half-of-all-acquired-masks, once-dilated mask. No post-SAM2 VLM review is performed. Native FoundationPose scores and coarse effector confidence are retained only for audit. The orientation stage makes exactly one effector-point VLM request; one recognized point is sufficient because coded projection against FK selects only a profiled candidate. All individual masks, the vote, the final mask, all fits, the effector/FK comparison, and the selected post-rotation pose are retained as inspection evidence.</p><label for="request">Request JSON</label><textarea id="request">{"use_latest_camera":true,"diagnostic_only":true}</textarea><button id="run">Run limited visual test</button><button class="secondary" id="refresh">Refresh evidence</button><pre id="result">Ready.</pre></section>
<section class="card wide"><h2>Configured visual references</h2><p>These are the profile-owned images available to the VLM. Consumer labels show which stage receives each image.</p><div class="images" id="configuredImages"><div class="empty">Loading arm-profile references…</div></div></section>
<section class="card wide"><h2>Exact visual pipeline inputs</h2><p>This is the last retained run, including failed-run evidence. A new limited test replaces it as stages complete.</p><div class="meta" id="runMeta"></div><div class="images" id="images"><div class="empty">No completed or active run evidence yet.</div></div></section>
</div></main><script>
const $=id=>document.getElementById(id);let profile=null;
async function jsonFetch(url,options){const response=await fetch(url,{cache:'no-store',...options});const value=await response.json();if(!response.ok)throw new Error(value.error||JSON.stringify(value));return value}
function text(value){return value===null||value===undefined?'—':String(value)}
function meta(target,rows){target.innerHTML='';for(const [key,value] of rows){const a=document.createElement('span'),b=document.createElement('code');a.textContent=key;b.textContent=text(value);target.append(a,b)}}
async function loadProfile(){profile=await jsonFetch('/v1/profile');meta($('profileMeta'),[['Arm Provider',profile.arm_provider_id],['Arm model',`${profile.model_id}@${profile.model_revision}`],['Arm profile file',profile.arm_profile_path],['Appendix key',profile.appendix_key],['CAD sent to FoundationPose',profile.cad.filename],['Profile digest',profile.model_file_sha256]]);$('cadPath').value=profile.cad.workspace_path;$('cadScale').value=profile.cad.scale_to_m;$('references').value=profile.reference_images.map(x=>x.workspace_path).join('\\n');$('vlmSeedGuidance').value=profile.appendix.vlm_seed_guidance||'';$('appendix').value=JSON.stringify(profile.appendix,null,2);const configured=[...(profile.cad.preview?[{...profile.cad.preview,label:profile.cad.preview.role}]:[]),...profile.reference_images.map((item,index)=>({...item,label:item.role||`Reference ${index+1}`}))];renderImages('configuredImages',configured,'No visual references are configured.')}
async function saveProfile(){const button=$('saveProfile');button.disabled=true;try{const appendix=JSON.parse($('appendix').value);appendix.mesh=appendix.mesh||{};appendix.mesh.path=$('cadPath').value.trim();appendix.mesh.scale_to_m=Number($('cadScale').value);appendix.vlm_seed_guidance=$('vlmSeedGuidance').value.trim();const old=Array.isArray(appendix.reference_images)?appendix.reference_images:[];appendix.reference_images=$('references').value.split(/\\r?\\n/).map(x=>x.trim()).filter(Boolean).map((path,index)=>({...(old[index]||{}),path,role:(old[index]||{}).role||'CAD_ORIENTATION_REFERENCE'}));const value=await jsonFetch('/v1/profile',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify({confirmation:'SAVE_ARM_PROFILE_APPENDIX',appendix})});$('profileResult').textContent=value.message;await loadProfile()}catch(error){$('profileResult').textContent=error.message}finally{button.disabled=false}}
function renderImages(hostId,items,emptyText){const host=$(hostId);host.innerHTML='';if(!items?.length){const empty=document.createElement('div');empty.className='empty';empty.textContent=emptyText;host.append(empty);return}for(const item of items){const card=document.createElement('article');card.className='image';const image=document.createElement('img');image.src=item.url;image.alt=item.label;image.loading='lazy';const caption=document.createElement('div');caption.className='caption';const title=document.createElement('b'),use=document.createElement('small'),description=document.createElement('small'),path=document.createElement('small');title.textContent=item.label;use.textContent=`Used by: ${(item.consumers||[]).join(', ')||'Developer inspection only'}`;description.textContent=item.description||'';path.textContent=item.path;caption.append(title,use);if(item.description)caption.append(description);caption.append(path);card.append(image,caption);host.append(card)}}
async function refreshEvidence(){try{const value=await jsonFetch('/v1/inspection');$('runState').textContent=value.status?`Last evidence: ${value.status} · ${value.stage}`:'No run evidence';const fp=value.foundation_pose||{},masks=value.mask_candidates||{},retention=masks.retention||{},vote=masks.vote||{},readiness=value.arm_provider_readiness||{},orientation=value.orientation_selection||{},vlm=value.vlm||{},fits=Array.isArray(fp.fits)?fp.fits:[],normalized=fits.filter(item=>item.upright_normalization_degrees===180).map(item=>`${item.candidate_id} (${Number(item.arm_base_positive_z_dot_world_raw).toFixed(3)}→${Number(item.arm_base_positive_z_dot_world).toFixed(3)})`),rejected=fits.filter(item=>item.physically_eligible===false).map(item=>`${item.candidate_id} (${Number(item.arm_base_positive_z_dot_world).toFixed(3)})`);meta($('runMeta'),[['Run ID',value.run_id],['Failed stage',value.failed_stage],['VLM route',`${vlm.backend||'—'} / ${vlm.model||'—'}`],['Arm Provider',readiness.provider_id],['Arm readiness',readiness.status],['Assembly stream',readiness.assembly_stream],['Arm profile',value.arm_profile?.model_id],['First-VLM profile guidance',masks.vlm_seed_guidance],['CAD filename sent',fp.cad_filename],['CAD path sent',fp.cad_path],['CAD SHA-256',fp.cad_sha256],['Configured VLM→SAM2 attempts',masks.configured_count],['Produced SAM2 masks',masks.produced_count],['Masks retained for vote',(retention.retained_candidate_ids||[]).join(', ')],['Post-SAM2 VLM review',retention.review_performed===false?'not performed':null],['Pixel vote threshold',vote.vote_threshold&&vote.survivor_count?`${vote.vote_threshold} of ${vote.survivor_count}`:null],['Final dilation radius',vote.dilation_radius_px],['Fit candidates',fp.candidate_count],['World-up eligible fits',(fp.physically_eligible_candidate_ids||[]).join(', ')],['Fits normalized by local-X 180°',normalized.join(', ')],['Fits rejected after normalization',rejected.join(', ')],['Fit VLM selected',fp.selection?.candidate_id],['Fit decision basis',fp.selection?.decision_basis],['Orientation decision basis',orientation.decision_basis],['All fits use voted mask',fp.all_fits_use_selected_mask],['Raw aligned depth',fp.depth_npy_path],['Resolved pose image',value.resolved_pose_path],['Calibration candidate',value.candidate_id],['Error',value.error]]);renderImages('images',value.images||[],'No completed or active run evidence yet.')}catch(error){$('runState').textContent=error.message}}
async function run(){const button=$('run');button.disabled=true;$('result').textContent='Running…';try{const request=JSON.parse($('request').value),maskCount=Number($('maskCount').value),fitCount=Number($('fitCount').value);if(!Number.isInteger(maskCount)||maskCount<1||maskCount>8)throw new Error('VLM→SAM2 mask attempt count must be an integer from 1 to 8.');if(!Number.isInteger(fitCount)||fitCount<1||fitCount>8)throw new Error('FoundationPose fit count must be an integer from 1 to 8.');request.mask_attempt_count=maskCount;request.fit_candidate_count=fitCount;const value=await jsonFetch('/v1/run',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(request)});$('result').textContent=JSON.stringify(value,null,2)}catch(error){$('result').textContent=error.message}finally{button.disabled=false;await refreshEvidence()}}
$('saveProfile').onclick=saveProfile;$('reloadProfile').onclick=()=>loadProfile().catch(e=>$('profileResult').textContent=e.message);$('refresh').onclick=refreshEvidence;$('run').onclick=run;
Promise.all([loadProfile(),refreshEvidence()]).catch(error=>$('result').textContent=error.message);setInterval(refreshEvidence,3000);
</script></body></html>"""


class SkillApp:
    def __init__(self, skill: LocateArmBaseSkill) -> None:
        self.skill = skill
        self.lock = threading.Lock()
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.image_paths: dict[str, Path] = {}

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("a locate_arm_base run is already active")
        try:
            self.last_result = self.skill.run(request)
            self.last_error = None
            return self.last_result
        except Exception as exc:
            self.last_error = str(exc)
            raise
        finally:
            self.lock.release()

    def status(self) -> dict[str, Any]:
        return {
            "skill_id": "locate_arm_base",
            "running": self.lock.locked(),
            "last_candidate_id": (self.last_result or {}).get("candidate_id"),
            "last_error": self.last_error,
        }

    def profile(self) -> dict[str, Any]:
        value = self.skill.profile_snapshot()
        preview = value.get("cad", {}).get("preview")
        if isinstance(preview, dict):
            preview["url"] = self._register_image(Path(preview["path"]))
        for item in value.get("reference_images", []):
            item["url"] = self._register_image(Path(item["path"]))
        return value

    def save_profile(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.lock.locked():
            raise RuntimeError("the arm profile cannot change during a localization run")
        if request.get("confirmation") != "SAVE_ARM_PROFILE_APPENDIX":
            raise ValueError("exact arm-profile save confirmation is required")
        appendix = request.get("appendix")
        if not isinstance(appendix, dict):
            raise ValueError("appendix must be a JSON object")
        profile = self.skill.save_profile_appendix(appendix)
        return {
            "status": "SAVED_RESTART_REQUIRED",
            "message": (
                "Arm-profile appendix saved. Restart the arm Provider before "
                "running locate_arm_base so the active assembly digest matches."
            ),
            "profile": profile,
        }

    def inspection(self) -> dict[str, Any]:
        value = self.skill.inspection_snapshot()
        for item in value.get("images", []):
            item["url"] = self._register_image(Path(item["path"]))
        return value

    def image(self, token: str) -> Path | None:
        return self.image_paths.get(token)

    def _register_image(self, path: Path) -> str:
        path = path.resolve()
        try:
            path.relative_to(self.skill.root)
        except ValueError as error:
            raise ValueError("development image resolves outside the workspace") from error
        if not path.is_file() or path.suffix.lower() not in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".bmp",
        }:
            raise ValueError(f"development image is unavailable: {path}")
        token = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        self.image_paths[token] = path
        return f"/v1/image/{token}"


class Handler(BaseHTTPRequestHandler):
    app: SkillApp

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._bytes(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path in {"/health", "/v1/status"}:
            self._json(200, self.app.status())
        elif path == "/v1/profile":
            self._json(200, self.app.profile())
        elif path == "/v1/inspection":
            self._json(200, self.app.inspection())
        elif path.startswith("/v1/image/"):
            image_path = self.app.image(path.rsplit("/", 1)[-1])
            if image_path is None:
                self._json(404, {"error": "image token is not registered"})
                return
            self._bytes(
                200,
                image_path.read_bytes(),
                mimetypes.guess_type(image_path.name)[0] or "application/octet-stream",
            )
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/v1/developer/shutdown":
            self._request_shutdown()
            return
        if path != "/v1/run":
            self._json(404, {"error": "not found"})
            return
        try:
            self._json(200, self.app.run(self._read_json()))
        except (ValueError, RuntimeError) as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def _request_shutdown(self) -> None:
        try:
            content_type = self.headers.get("Content-Type", "").split(
                ";",
                1,
            )[0]
            if content_type.strip().lower() != "application/json":
                raise ValueError(
                    "developer shutdown requires application/json"
                )
            request = self._read_json()
            if request.get("confirmation") != DEVELOPER_SHUTDOWN_CONFIRMATION:
                raise ValueError(
                    "exact developer shutdown confirmation is required"
                )
            self._json(
                202,
                {
                    "status": "SHUTDOWN_REQUESTED",
                    "skill_id": "locate_arm_base",
                },
            )
            threading.Thread(
                target=self.server.shutdown,
                name="locate-arm-base-ui-shutdown",
                daemon=True,
            ).start()
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    def do_PUT(self) -> None:  # noqa: N802
        if self.path != "/v1/profile":
            self._json(404, {"error": "not found"})
            return
        try:
            self._json(200, self.app.save_profile(self._read_json()))
        except (ValueError, RuntimeError) as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
        if not isinstance(value, dict):
            raise ValueError("request must be a JSON object")
        return value

    def _json(self, status: int, body: dict[str, Any]) -> None:
        self._bytes(status, json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"), "application/json")

    def _bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[LocateArmBaseUI] {format % args}", flush=True)


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    # Windows otherwise permits two development UIs to share one port and serve stale code.
    allow_reuse_address = False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=7114)
    args = parser.parse_args()
    root = Path(os.environ.get("PHYSICAL_AGENT_ROOT") or Path(__file__).resolve().parents[4])
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    skill = LocateArmBaseSkill(config, root)
    Handler.app = SkillApp(skill)
    server = ExclusiveThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        skill.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
