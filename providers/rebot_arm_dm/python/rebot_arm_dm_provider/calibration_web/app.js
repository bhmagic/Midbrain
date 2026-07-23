'use strict';
const state = { model:null, arm:null, lease:null, deadman:false, activeRow:null, activePointerId:null, commandSending:false, releaseInProgress:false, view:'iso', safeRanges:{}, autoRunning:false, skipCurrent:false, autoAnchorPose:null };
const $ = id => document.getElementById(id);
const deg = r => r * 180 / Math.PI;
const rad = d => d * Math.PI / 180;
async function api(path, method='GET', body=null) {
  const response = await fetch(path, {method, headers:{'Content-Type':'application/json'}, body:body ? JSON.stringify(body) : null, keepalive:method==='POST'});
  const data = await response.json(); if (!response.ok || data.error) throw new Error(data.error || response.statusText); return data;
}
function badge(id,text,kind='') { const e=$(id); e.textContent=text; e.className='badge '+kind; }
function log(message) { const e=$('auto-log'); const lines=(e.textContent+`[${new Date().toLocaleTimeString()}] ${message}\n`).split('\n'); e.textContent=lines.slice(-16).join('\n'); e.scrollTop=e.scrollHeight; }
function fmt(v,n=3){ return Number.isFinite(Number(v)) ? Number(v).toFixed(n) : '—'; }

async function initialize(){
  state.model=await api('/api/model'); $('model-view').textContent=JSON.stringify(state.model,null,2); buildJointRows(); buildAutoRows(); bind(); await poll(); setInterval(poll,100); setInterval(renewLease,350); setInterval(refreshManualCommand,50);
}
function buildJointRows(){
  const host=$('joint-table'); host.innerHTML='<div class="joint-row header"><span>Joint</span><span>Mode</span><span>Target</span><span>Cal min °</span><span>Cal max °</span><span>Slider</span><span>kp</span><span>kd</span><span>V limit</span><span>Torque ratio</span><span>Limits / state</span></div>';
  state.model.joints.forEach((j,i)=>{
    const c=j.default_test, limits=j.hard_limit_rad, caps=j.provider_test_caps, ml=j.motor_limits;
    const row=document.createElement('div'); row.className='joint-row'; row.dataset.index=i;
    row.dataset.targetInitialized='false';
    row.innerHTML=`<strong>${j.name}</strong>
      <select class="mode"><option>IMPEDANCE</option><option>POSITION_VELOCITY_LIMITED</option><option>POSITION_EFFORT_LIMITED</option></select>
      <span class="joint-value target-value">0.000 rad</span>
      <input class="cal-min" type="number" step="1" value="${deg(j.default_calibration_range_rad[0]).toFixed(1)}">
      <input class="cal-max" type="number" step="1" value="${deg(j.default_calibration_range_rad[1]).toFixed(1)}">
      <input class="target" type="range" min="${deg(j.default_calibration_range_rad[0])}" max="${deg(j.default_calibration_range_rad[1])}" step="0.1" value="${deg(j.home_position_rad)}" disabled aria-label="${j.name} target angle">
      <input class="kp" type="number" min="${caps.min_kp ?? c.kp}" max="${caps.max_kp}" step="0.5" value="${c.kp}">
      <input class="kd" type="number" min="0" max="${caps.max_kd}" step="0.1" value="${Math.min(c.kd,caps.max_kd)}">
      <input class="vlim" type="number" min="0.01" step="0.01" value="${c.velocity_limit_rad_s}">
      <input class="ratio" type="number" min="0" max="1" step="0.01" value="${c.torque_limit_ratio}">
      <span class="limits">${j.motor_model} ${j.motor_revision} · hard ${deg(limits[0]).toFixed(0)}°…${deg(limits[1]).toFixed(0)}°<br>FORCE_POS 1.0 = configured TMAX ${ml.configured_tmax_nm} N·m; rated ${ml.manufacturer_rated_torque_nm} N·m; listed peak ${ml.manufacturer_peak_torque_nm} N·m<br>Official/Unity MIT kp=${c.kp}, kd=${c.kd}; tracking-effort limit ${caps.mit_tracking_effort_limit_nm} N·m<br>Load-bearing MIT rule kp≥${caps.min_kp ?? c.kp}; kd may be low. Reviewed caps kp≤${caps.max_kp}, kd≤${caps.max_kd}<br><span class="live">waiting</span></span>`;
    host.appendChild(row);
    const min=row.querySelector('.cal-min'), max=row.querySelector('.cal-max'), slider=row.querySelector('.target');
    const update=()=>{ const hardMin=deg(limits[0]), hardMax=deg(limits[1]); let lo=Math.max(hardMin+1,Number(min.value)), hi=Math.min(hardMax-1,Number(max.value)); if(lo>=hi){hi=lo+1;} min.value=lo;max.value=hi;slider.min=lo;slider.max=hi;slider.value=Math.min(hi,Math.max(lo,Number(slider.value))); };
    min.addEventListener('change',update); max.addEventListener('change',update);
    slider.addEventListener('focus',()=>{state.activeRow=row;});
    slider.addEventListener('pointerdown',event=>{
      if(!state.lease){showError(new Error('Enable calibration control before moving a slider.'));return;}
      if(state.deadman)return;
      state.activeRow=row;
      state.activePointerId=event.pointerId;
      state.deadman=true;
      row.dataset.dragging='true';
      updateManualStatus();
    });
    slider.addEventListener('input',()=>{
      state.activeRow=row;
      row.querySelector('.target-value').textContent=`${rad(slider.value).toFixed(3)} rad`;
      if(state.deadman&&state.activeRow===row)sendRow(row).catch(showError);
    });
  });
}
function buildAutoRows(){
  const host=$('auto-ranges'); host.innerHTML='<div class="auto-row"><span>Use</span><span>Joint</span><span>Requested min °</span><span>Requested max °</span><span>Automatic safe range</span><span>Result</span></div>';
  state.model.joints.forEach((j,i)=>{ const row=document.createElement('div'); row.className='auto-row'; row.dataset.index=i; row.innerHTML=`<input class="use" type="checkbox" ${i<6?'checked':''}><strong>${j.name}</strong><input class="minimum" type="number" step="1" value="${deg(j.default_calibration_range_rad[0]).toFixed(1)}"><input class="maximum" type="number" step="1" value="${deg(j.default_calibration_range_rad[1]).toFixed(1)}"><span class="safe-range">not checked</span><span class="result">pending</span>`; host.appendChild(row); });
}
function bind(){
  document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.tab-page').forEach(x=>x.classList.remove('active'));b.classList.add('active');$(b.dataset.tab).classList.add('active');});
  document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{state.view=b.dataset.view;draw();});
  $('acquire-lease').onclick=acquireLease; $('gravity-float').onclick=()=>api('/api/gravity-float','POST').then(()=>poll()).catch(showError); $('safe-home').onclick=()=>api('/api/safe-home','POST').then(()=>poll()).catch(showError); $('reset-manual-defaults').onclick=resetManualDefaults; $('reset-auto-defaults').onclick=resetAutomaticDefaults;
  for(const eventName of ['pointerup','pointercancel']){
    window.addEventListener(eventName,event=>{
      if(state.deadman&&(state.activePointerId===null||event.pointerId===state.activePointerId))releaseDeadman();
    },true);
  }
  window.addEventListener('blur',()=>releaseDeadman());
  document.addEventListener('visibilitychange',()=>{if(document.hidden)releaseDeadman();});
  $('capture-anchor').onclick=()=>captureAutomaticAnchor(true); $('check-ranges').onclick=()=>checkAllRanges(); $('start-auto').onclick=startAutomatic; $('skip-joint').onclick=()=>{state.skipCurrent=true;api('/api/experiment/cancel','POST').catch(()=>{});log('Skip requested for current joint.');};
  $('cancel-auto').onclick=async()=>{state.autoRunning=false;await api('/api/experiment/cancel','POST');await poll();log('Automatic calibration paused in speed-limited position hold.');};
  $('stop-home').onclick=async()=>{state.autoRunning=false;await api('/api/experiment/cancel','POST');log('Returning to safe-home…');await api('/api/safe-home','POST');log('Safe-home request completed.');};
}
async function acquireLease(){ state.lease=await api('/api/lease','POST',{holder:'standalone_calibration_gui',duration_ms:800}); setManualSlidersEnabled(true); badge('lease-state','Calibration lease','good'); updateManualStatus(); }
async function renewLease(){ if(!state.lease)return; try{state.lease=await api('/api/renew','POST',{...state.lease,duration_ms:800});}catch(e){state.lease=null;state.deadman=false;state.activePointerId=null;state.activeRow=null;setManualSlidersEnabled(false);badge('lease-state','Lease lost','bad');updateManualStatus();}}
function setManualSlidersEnabled(enabled){document.querySelectorAll('.joint-row[data-index] .target').forEach(slider=>{slider.disabled=!enabled;});}
function updateManualStatus(){const status=$('manual-motion-status');if(state.deadman){status.classList.add('active');status.textContent='MOTION ENABLED — RELEASE THE POINTER FOR GRAVITY FLOAT';}else{status.classList.remove('active');status.textContent=state.lease?'PRESS AND DRAG A SLIDER — RELEASE ANYWHERE FOR GRAVITY FLOAT':'ENABLE CALIBRATION CONTROL TO UNLOCK THE SLIDERS';}}
async function releaseDeadman(){
  if(!state.deadman||state.releaseInProgress)return;
  const row=state.activeRow;
  state.deadman=false;
  state.activePointerId=null;
  if(row)row.dataset.dragging='false';
  state.releaseInProgress=true;
  updateManualStatus();
  snapRowToMeasured(row);
  try{await api('/api/gravity-float','POST');await poll();snapRowToMeasured(row);}catch(error){showError(error);}finally{state.releaseInProgress=false;}
}
function snapRowToMeasured(row){if(!row||!state.arm?.positions_rad)return;const i=Number(row.dataset.index),slider=row.querySelector('.target'),value=deg(state.arm.positions_rad[i]);slider.value=Math.min(Number(slider.max),Math.max(Number(slider.min),value));row.querySelector('.target-value').textContent=`${state.arm.positions_rad[i].toFixed(3)} rad`;row.dataset.targetInitialized='true';}
function resetManualDefaults(){document.querySelectorAll('.joint-row[data-index]').forEach(row=>{const i=Number(row.dataset.index),joint=state.model.joints[i],defaults=joint.default_test;row.querySelector('.mode').value='IMPEDANCE';row.querySelector('.cal-min').value=deg(joint.default_calibration_range_rad[0]).toFixed(1);row.querySelector('.cal-max').value=deg(joint.default_calibration_range_rad[1]).toFixed(1);row.querySelector('.kp').value=defaults.kp;row.querySelector('.kd').value=defaults.kd;row.querySelector('.vlim').value=defaults.velocity_limit_rad_s;row.querySelector('.ratio').value=defaults.torque_limit_ratio;const slider=row.querySelector('.target');slider.min=deg(joint.default_calibration_range_rad[0]);slider.max=deg(joint.default_calibration_range_rad[1]);snapRowToMeasured(row);});}
function resetAutomaticDefaults(){$('table-height').value='0';$('table-clearance').value='0.025';$('slow-speed').value='0.16';$('medium-speed').value='0.32';$('auto-torque').value='0.12';$('save-raw-samples').checked=false;$('auto-save-accepted').checked=true;$('workspace-confirmed').checked=false;document.querySelectorAll('.auto-row[data-index]').forEach(row=>{const i=Number(row.dataset.index),joint=state.model.joints[i];row.querySelector('.use').checked=i<6;row.querySelector('.minimum').value=deg(joint.default_calibration_range_rad[0]).toFixed(1);row.querySelector('.maximum').value=deg(joint.default_calibration_range_rad[1]).toFixed(1);row.querySelector('.safe-range').textContent='not checked';row.querySelector('.result').textContent='pending';});state.safeRanges={};state.autoAnchorPose=null;displayAutomaticAnchor();}
async function refreshManualCommand(){if(!state.deadman||!state.activeRow||!state.lease||state.commandSending)return;state.commandSending=true;try{await sendRow(state.activeRow);}catch(error){state.deadman=false;updateManualStatus();showError(error);}finally{state.commandSending=false;}}
async function sendRow(row){
  if(!state.lease) throw new Error('Acquire a calibration lease first.'); const i=Number(row.dataset.index), mode=row.querySelector('.mode').value, position=rad(Number(row.querySelector('.target').value));
  const values={position_rad:position}; if(mode==='IMPEDANCE'){values.velocity_rad_s=0;values.target_rate_limit_rad_s=Number(row.querySelector('.vlim').value);values.kp=Number(row.querySelector('.kp').value);values.kd=Number(row.querySelector('.kd').value);values.feedforward_torque_nm=0;}
  else if(mode==='POSITION_VELOCITY_LIMITED'){values.velocity_limit_rad_s=Number(row.querySelector('.vlim').value);} else {values.velocity_limit_rad_s=Number(row.querySelector('.vlim').value);values.torque_limit_ratio=Number(row.querySelector('.ratio').value);}
  await api('/api/command','POST',{...state.lease,timeout_ms:150,commands:[{joint_index:i,mode,values}]});
}
async function poll(){
  try{state.arm=await api('/api/state');badge('connection','Provider connected','good');badge('provider-state',state.arm.provider_state||state.arm.state,(state.arm.health==='HEALTHY'?'good':'warn'));updateTelemetry();draw();checkCurrentCollision();}
  catch(e){badge('connection','Disconnected','bad');}
}
function updateTelemetry(){ if(!state.arm?.positions_rad)return; $('telemetry').innerHTML=state.arm.positions_rad.map((q,i)=>`<div><strong>J${i+1} ${fmt(q)} rad</strong>${fmt(state.arm.velocities_rad_s[i])} rad/s<br>${fmt(state.arm.torques_nm[i])} N·m</div>`).join(''); document.querySelectorAll('.joint-row[data-index]').forEach(row=>{const i=Number(row.dataset.index);row.querySelector('.live').textContent=`q ${fmt(state.arm.positions_rad[i])} · v ${fmt(state.arm.velocities_rad_s[i])} · τ ${fmt(state.arm.torques_nm[i])}`;if(row.dataset.targetInitialized==='false'||!state.deadman||state.activeRow!==row)snapRowToMeasured(row);}); }
async function checkCurrentCollision(){ if(!state.arm?.positions_rad)return;try{const r=await api('/api/collision/check','POST',{positions_rad:state.arm.positions_rad,table_height_m:Number($('table-height').value),table_clearance_m:Number($('table-clearance').value)});badge('collision-state',r.safe?`margin ${fmt(r.minimum_safety_margin_m ?? r.minimum_clearance_m)} m`:`unsafe: ${r.reason}`,r.safe?'good':'bad');}catch(_){} }
function project(p){ let [x,y,z]=p, a,b;if(state.view==='front'){a=x;b=z;}else if(state.view==='top'){a=x;b=y;}else{a=(x-y)*.78;b=z+(x+y)*.28;}return[a,b];}
function draw(){ const c=$('arm-canvas'),ctx=c.getContext('2d'),dpr=window.devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;if(c.width!==w*dpr||c.height!==h*dpr){c.width=w*dpr;c.height=h*dpr;}ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);if(!state.arm?.kinematic_points_m)return;const projected=state.arm.kinematic_points_m.map(project), scale=Math.min(w,h)*.82/0.8, ox=w/2, oy=h*.78; const pts=projected.map(([a,b])=>[ox+a*scale,oy-b*scale]);
  const tableY=oy-(Number($('table-height').value))*scale;ctx.strokeStyle='#f4c45e';ctx.lineWidth=2;ctx.setLineDash([10,8]);ctx.beginPath();ctx.moveTo(20,tableY);ctx.lineTo(w-20,tableY);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#f4c45e';ctx.fillText('calibration desktop plane',24,tableY-8);
  ctx.lineCap='round';for(let i=0;i<pts.length-1;i++){ctx.strokeStyle=i<3?'#67b5ff':'#57c991';ctx.lineWidth=Math.max(7,18-i*1.5);ctx.beginPath();ctx.moveTo(...pts[i]);ctx.lineTo(...pts[i+1]);ctx.stroke();}pts.forEach((p,i)=>{ctx.fillStyle=i===pts.length-1?'#ff8c96':'#e8edf4';ctx.beginPath();ctx.arc(p[0],p[1],i===0?10:6,0,Math.PI*2);ctx.fill();}); }
function displayAutomaticAnchor(){
  const element=$('anchor-pose');
  if(!state.autoAnchorPose){element.textContent='No pose captured. Move the arm by hand in gravity-float, then capture it.';element.className='anchor-pose warn';return;}
  element.textContent=state.autoAnchorPose.map((q,i)=>`J${i+1} ${deg(q).toFixed(1)}°`).join(' · ');
  element.className='anchor-pose good';
}
async function captureAutomaticAnchor(writeLog=false){
  await poll();
  if(!state.arm?.positions_rad)throw new Error('No measured arm state is available.');
  if(state.arm.provider_state!=='SAFE_HOLD_GRAVITY_FLOAT')throw new Error('Place the provider in gravity-float before capturing the calibration pose.');
  state.autoAnchorPose=state.arm.positions_rad.map(Number);
  state.safeRanges={};
  displayAutomaticAnchor();
  if(writeLog)log('Calibration pose captured.');
  return state.autoAnchorPose;
}
async function checkAllRanges(anchorPose=null){
  const pose=anchorPose||state.autoAnchorPose;
  if(!pose)throw new Error('Capture the current gravity-float pose before calculating ranges.');
  state.safeRanges={};
  const rows=[...document.querySelectorAll('.auto-row[data-index]')];
  for(const row of rows){
    const i=Number(row.dataset.index);
    if(!row.querySelector('.use').checked)continue;
    const result=await api('/api/collision/range','POST',{positions_rad:pose,joint_index:i,requested_minimum_rad:rad(row.querySelector('.minimum').value),requested_maximum_rad:rad(row.querySelector('.maximum').value),table_height_m:Number($('table-height').value),table_clearance_m:Number($('table-clearance').value)});
    state.safeRanges[i]=result;
    row.querySelector('.safe-range').textContent=result.safe?`${deg(result.minimum_rad).toFixed(1)}° … ${deg(result.maximum_rad).toFixed(1)}°`:`unsafe: ${result.reason}`;
  }
}
async function startAutomatic(){
  if(!$('workspace-confirmed').checked){showError(new Error('Confirm the clear workspace before automatic motion.'));return;}
  if(state.autoRunning)return;
  state.autoRunning=true;state.skipCurrent=false;$('fit-results').innerHTML='';$('auto-log').textContent='';
  try{
    // The exact measured pose at Start is the common anchor. The arm is not
    // sent to safe-home between joints.
    const anchor=await captureAutomaticAnchor(false);
    log('Starting friction-only calibration from the captured pose.');
    await checkAllRanges(anchor);
    const rows=[...document.querySelectorAll('.auto-row[data-index]')].filter(r=>r.querySelector('.use').checked),total=rows.length;
    let done=0;
    for(const row of rows){
      if(!state.autoRunning)break;
      const i=Number(row.dataset.index),range=state.safeRanges[i];
      state.skipCurrent=false;
      if(!range?.safe){row.querySelector('.result').textContent='skipped: unsafe range';done++;continue;}
      if(anchor[i]<range.minimum_rad||anchor[i]>range.maximum_rad){row.querySelector('.result').textContent='skipped: anchor outside range';log(`Joint ${i+1}: captured angle is outside the approved range.`);done++;continue;}
      log(`Joint ${i+1}: two-speed friction test started.`);
      row.querySelector('.result').textContent='running';
      try{
        const result=await api('/api/experiment','POST',{joint_index:i,minimum_rad:range.minimum_rad,maximum_rad:range.maximum_rad,anchor_positions_rad:anchor,speeds_rad_s:[Number($('slow-speed').value),Number($('medium-speed').value)],save_raw_samples:$('save-raw-samples').checked,workspace_confirmed:true});
        if(state.skipCurrent){row.querySelector('.result').textContent='skipped';}
        else{
          const card=renderFit(i,result);row.querySelector('.result').textContent=result.status;
          if(result.fit.accepted&&$('auto-save-accepted').checked){const saved=await api('/api/apply-fit','POST',{joint_index:i,fit:result.fit});card.querySelector('button').disabled=true;row.querySelector('.result').textContent=result.status+' · SAVED';log(`Joint ${i+1}: identifiable values saved as ${saved.calibration_revision}.`);}
        }
      }catch(e){row.querySelector('.result').textContent=state.skipCurrent?'skipped':'failed';log(`Joint ${i+1}: ${e.message}`);}
      await poll();done++;$('progress-bar').style.width=`${done/Math.max(total,1)*100}%`;
    }
    if(state.autoRunning){log('Automatic sequence finished at the captured pose. Speed-limited position hold remains active.');}
  }catch(e){showError(e);}finally{state.autoRunning=false;}
}
function renderFit(index,result){const fit=result.fit,card=document.createElement('div');card.className='fit-card '+(fit.accepted?'accepted':'review');card.innerHTML=`<strong>Joint ${index+1}: ${result.status}</strong><br>Factory mass and gravity model retained.<br>Coulomb friction ${fmt(fit.coulomb_friction_nm)} N·m · viscous friction ${fmt(fit.viscous_friction_nm_per_rad_s)} N·m/(rad/s)<br>paired samples ${fit.pair_count} · validation RMS ${fmt(fit.validation_rms_residual_nm)} N·m · max ${fmt(fit.validation_max_residual_nm)} N·m · condition ${Number(fit.condition_number).toExponential(2)} <button>Save friction values</button>`;const button=card.querySelector('button');button.disabled=!fit.accepted;button.onclick=async()=>{const r=await api('/api/apply-fit','POST',{joint_index:index,fit});button.disabled=true;log(`Joint ${index+1}: friction values saved as ${r.calibration_revision}.`);};$('fit-results').appendChild(card);return card;}
function showError(error){console.error(error);alert(error.message||String(error));log(`ERROR: ${error.message||error}`);}
initialize().catch(showError);
