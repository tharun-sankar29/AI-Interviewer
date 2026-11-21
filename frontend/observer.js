const $ = id => document.getElementById(id);
const colors = { untested:'#9aa8b8', weak:'#d94b63', developing:'#d89532', strong:'#6d8ff5', mastered:'#159b78' };
let lastEntriesSignature = '';
let roles = [];
let selectedRole = null;
let uploadedJd = null;
let healthReady = false;
const customRole = {id:'custom', name:'Custom role', description:'Enter a role for this interview.'};

function ensureObserverStopButton() {
  let button = $('observerStopBtn');
  if (button) return button;
  button = document.createElement('button'); button.id = 'observerStopBtn'; button.type = 'button'; button.textContent = 'End interview';
  $('sessionSource').parentElement.appendChild(button);
  button.addEventListener('click', async () => {
    if (!window.confirm('End this interview and generate the report now?')) return;
    button.disabled = true;
    const response = await fetch('/api/session/stop', {method:'POST'});
    if (!response.ok) {
      button.disabled = false;
      const data = await response.json().catch(() => ({}));
      $('setupError').textContent = data.error || 'Could not end the interview. Please retry.';
    }
    await refresh();
  });
  return button;
}

function esc(value) { const div=document.createElement('div'); div.textContent=value ?? ''; return div.innerHTML; }
function status(node) { return node.status || (node.evidence_count ? 'developing' : 'untested'); }

function drawGraph(graph, currentId) {
  const svg=$('graph'), skills=Object.values(graph?.skills || {}), edges=graph?.edges || [];
  const rows=Math.ceil(skills.length/3);
  const canvasHeight=Math.max(280,82+Math.max(0,rows-1)*145+85);
  const columns=Math.min(3,Math.max(1,skills.length));
  const canvasWidth=[0,300,510,760][columns];
  svg.style.setProperty('--graph-canvas-width',`${canvasWidth}px`);
  svg.setAttribute('viewBox',`0 0 ${canvasWidth} ${canvasHeight}`);
  $('graphEmpty').style.display=skills.length ? 'none' : 'grid';
  svg.innerHTML=''; if (!skills.length) return;
  const defs=document.createElementNS('http://www.w3.org/2000/svg','defs'); defs.innerHTML='<marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#aab8c9"/></marker>'; svg.appendChild(defs);
  const points=skills.map((node,i)=>({node,x:columns===1 ? 150 : 125+(i%3)*250,y:82+Math.floor(i/3)*145})); const pointById=Object.fromEntries(points.map(p=>[p.node.skill_id,p]));
  edges.forEach(edge=>{const a=pointById[edge.source],b=pointById[edge.target];if(!a||!b)return;const line=document.createElementNS('http://www.w3.org/2000/svg','line');line.setAttribute('x1',a.x);line.setAttribute('y1',a.y);line.setAttribute('x2',b.x);line.setAttribute('y2',b.y);line.setAttribute('class',`edge ${edge.relation}`);svg.appendChild(line)});
  points.forEach(({node,x,y})=>{const group=document.createElementNS('http://www.w3.org/2000/svg','g');const circle=document.createElementNS('http://www.w3.org/2000/svg','circle');circle.setAttribute('cx',x);circle.setAttribute('cy',y);circle.setAttribute('r',28);circle.setAttribute('fill',colors[status(node)]||colors.untested);circle.setAttribute('class',`node ${node.skill_id===currentId?'active':''}`);const label=document.createElementNS('http://www.w3.org/2000/svg','text');label.setAttribute('x',x);label.setAttribute('y',y+48);label.setAttribute('class','node-label');label.textContent=node.name;const value=document.createElementNS('http://www.w3.org/2000/svg','text');value.setAttribute('x',x);value.setAttribute('y',y+65);value.setAttribute('class','node-value');value.textContent=`M ${Math.round((node.mastery||0)*100)} · C ${Math.round((node.confidence||0)*100)}`;group.append(circle,label,value);svg.appendChild(group)});
}

function renderRoles() {
  $('roleGrid').innerHTML = [...roles, customRole].map(role => `<button class="role-option${selectedRole?.id === role.id ? ' selected' : ''}" type="button" data-role-id="${esc(role.id)}" aria-pressed="${selectedRole?.id === role.id}"><strong>${esc(role.name)}</strong><small>${esc(role.description)}</small></button>`).join('');
  $('roleGrid').querySelectorAll('[data-role-id]').forEach(button => button.addEventListener('click', () => {
    selectedRole = button.dataset.roleId === 'custom' ? customRole : roles.find(role => role.id === button.dataset.roleId) || null;
    $('customRoleField').hidden = selectedRole?.id !== 'custom';
    renderRoles();
    updateReview();
    if (selectedRole?.id === 'custom') $('customRoleName').focus();
  }));
}

function updateReview() {
  const mode = $('modeSelect').value;
  const modeText = $('modeSelect').selectedOptions[0]?.textContent || mode;
  const jdLength = $('jdText').value.trim().length;
  const customName = $('customRoleName').value.trim();
  const validCustomName = customName.length >= 1 && customName.length <= 80;
  const roleName = selectedRole?.id === 'custom' ? customName : selectedRole?.name;
  $('reviewTitle').textContent = selectedRole?.id === 'custom' && !validCustomName ? 'Enter a role name to continue' : roleName ? `${roleName} interview` : 'Select a role to continue';
  $('reviewSummary').textContent = selectedRole ? `${modeText} · ${jdLength ? `${jdLength.toLocaleString()} characters of job context` : 'resume-aware questions'}` : 'Your session summary will appear here.';
  $('launchBtn').disabled = !selectedRole || (selectedRole.id === 'custom' && !validCustomName) || !healthReady;
}

async function loadRoles() {
  const response = await fetch('/api/roles', { cache: 'no-store' });
  if (!response.ok) throw new Error('Could not load the role library.');
  roles = (await response.json()).roles || [];
  renderRoles();
  updateReview();
}

async function handleJdUpload(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  $('uploadStatus').textContent = 'Reading job description…';
  const formData = new FormData();
  formData.append('job_description', file);
  try {
    const response = await fetch('/api/session/config/upload', { method: 'POST', body: formData });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not read this file.');
    $('jdText').value = data.content;
    uploadedJd = data.filename;
    $('uploadStatus').textContent = `${data.filename} · ${data.character_count.toLocaleString()} characters`;
    updateReview();
  } catch (error) {
    uploadedJd = null;
    $('uploadStatus').textContent = error.message;
    $('jdFile').value = '';
  }
}

function showDashboard() {
  $('onboarding').hidden = true;
  $('dashboard').hidden = false;
  $('phase').textContent = 'Live session';
}

function showOnboarding() {
  $('dashboard').hidden = true;
  $('onboarding').hidden = false;
  $('phase').textContent = 'Setup required';
  $('viewReportBtn').hidden = true;
  $('downloadReportBtn').hidden = true;
  lastEntriesSignature = '';
}

async function launchSession() {
  if (!selectedRole || $('launchBtn').disabled) return;
  const button = $('launchBtn');
  button.disabled = true;
  $('setupError').textContent = '';
  button.textContent = 'Launching…';
  try {
    const saveResponse = await fetch('/api/session/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        role_id: selectedRole.id,
        ...(selectedRole.id === 'custom' ? {role_name: $('customRoleName').value.trim()} : {}),
        mode: $('modeSelect').value,
        context: selectedRole.id === 'custom' ? $('customRoleName').value.trim() : selectedRole.name,
        job_description: $('jdText').value.trim(),
        job_description_files: uploadedJd ? [uploadedJd] : [],
        job_description_source: uploadedJd ? 'upload' : ($('jdText').value.trim() ? 'pasted' : 'default'),
      }),
    });
    const saved = await saveResponse.json();
    if (!saveResponse.ok) throw new Error(saved.error || 'Could not save session setup.');
    const launchResponse = await fetch('/api/session/launch', { method: 'POST' });
    const launched = await launchResponse.json();
    if (!launchResponse.ok) throw new Error(launched.error || 'Could not launch the interview.');
    showDashboard();
    await refresh();
  } catch (error) {
    $('setupError').textContent = error.message;
    button.innerHTML = 'Launch interview <span>↗</span>';
    updateReview();
  }
}

async function resetSession() {
  await fetch('/api/session/reset', { method: 'POST' });
  selectedRole = null; uploadedJd = null; $('customRoleName').value = ''; $('customRoleField').hidden = true; $('jdText').value = ''; $('jdFile').value = '';
  $('uploadStatus').textContent = 'No job description added';
  $('launchBtn').innerHTML = 'Launch interview <span>↗</span>';
  $('setupError').textContent = '';
  renderRoles(); updateReview(); showOnboarding();
}

function update(data) {
  const graph=data.skill_graph||{}, p=data.planner_state||{}, q=p.current_question||{}; const node=graph.skills?.[p.current_skill_id]; const session=data.session||{};
  const graphSource={gemini:'Gemini',groq:'Groq',question_fallback:'Basic fallback'}[data.graph_source]||'Waiting';
  const graphHeading=document.querySelector('.graph-panel .panel-head h2');
  if(graphHeading) graphHeading.textContent=`Knowledge map · ${graphSource}`;
  $('phase').textContent=(p.execution_phase||'idle').replaceAll('_',' '); $('phasePill').textContent=(p.execution_phase||'idle').replaceAll('_',' ').toUpperCase();
  const evaluated=session.interview_started?(Number(p.evaluated_turn_count)||0):0;
  const bounded=session.main_question_budget!=null;
  const mainRemaining=bounded?Math.max(0,session.main_question_budget-(Number(p.main_questions_used)||0)):0;
  const followupsRemaining=bounded?Math.max(0,session.max_followups_total-(Number(p.followups_used)||0)):0;
  const perSkill=session.max_followups_per_skill||1;
  const pendingFollowups=q.is_followup?Math.max(0,perSkill-(Number(q.followup_depth)||1)+1):0;
  const skillNodes=Object.values(graph.skills||{});
  const skillCapacity=skillNodes.length?skillNodes.reduce((total,skill)=>total+Math.max(0,perSkill-(Number(skill.followup_count)||0)),0):followupsRemaining;
  const possibleFollowups=bounded?Math.min(followupsRemaining,skillCapacity,mainRemaining*perSkill+pendingFollowups):0;
  const completed=session.interview_started&&p.execution_phase==='completed';
  $('viewReportBtn').hidden = !data.report_available;
  $('downloadReportBtn').hidden = !data.report_available;
  $('sessionRole').textContent=session.role_name||'Interview';
  $('sessionMode').textContent=session.mode_label||session.mode||'—';
  $('modeDescription').textContent=session.mode_description||'';
  $('answersRemaining').textContent=completed?'0':bounded?`Up to ${mainRemaining+possibleFollowups}`:'No fixed limit';
  $('remainingDetail').textContent=completed?'Interview completed':
    !session.interview_started||p.execution_phase==='waiting_for_introduction'?'Introduction is unscored':
    bounded?`${mainRemaining} main remaining · up to ${possibleFollowups} follow-ups possible`:
    `${evaluated} evaluated ${evaluated===1?'answer':'answers'} completed`;
  const readiness = session.interview_started ? (p.execution_phase === 'completed' ? 'Interview completed' : 'Interview started') : session.candidate_resume_uploaded ? `Resume uploaded · ${session.candidate_resume_filename || ''}` : 'Waiting for resume';
  $('sessionSource').textContent = `${session.job_description_length ? `${session.job_description_source === 'upload' ? 'Uploaded' : 'Pasted'} job description · ${session.job_description_length.toLocaleString()} characters` : 'Resume-aware session · no job description added'} · ${readiness}`;
  $('questions').textContent=evaluated; $('mainUsed').textContent=p.main_questions_used||0; $('followups').textContent=p.followups_used||0; $('mastered').textContent=p.mastered_skill_count||0; $('activeSkills').textContent=`${p.active_skill_count||0} active`;
  $('skillName').textContent=node?.name||'Waiting for interview'; $('skillId').textContent=node?node.skill_id:'No active skill'; $('difficulty').textContent=q.difficulty??'—'; $('questionType').textContent=q.question_type||'—'; $('reason').textContent=q.planner_reason?.replaceAll('_',' ')||'—'; $('followupState').textContent=q.is_followup?'Yes':'No'; $('updated').textContent=`Updated ${new Date().toLocaleTimeString()}`; drawGraph(graph,p.current_skill_id);
  const stopButton=ensureObserverStopButton(); stopButton.hidden=!p.manual_stop_available;
  const entries=data.entries||[];
  const signature=JSON.stringify(entries);
  if(signature!==lastEntriesSignature){
    $('timeline').innerHTML=entries.length ? [...entries].reverse().map((entry,index)=>{
      const time=entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';
      const kind=entry.is_intro?'Introduction':entry.is_followup?'Follow-up question':'Main question';
      const score=Number.isFinite(entry.score) ? `${entry.score}/10` : 'Not scored';
      return `<article class="conversation-entry"><header><span class="conversation-kind">${esc(kind)}</span><span class="conversation-meta">${esc(entry.skill_id||'Warm-up')}${time?` · ${esc(time)}`:''}</span><strong class="conversation-score${entry.score < 5 ? ' low' : ''}">${esc(score)}</strong></header><section><h3>Interviewer asked</h3><p>${esc(entry.question||'')}</p></section><section><h3>Interviewee said</h3><p>${esc(entry.answer||'')}</p></section>${entry.is_intro?'<p class="conversation-note">Introduction responses are not scored.</p>':`<section class="score-reason"><h3>Why this score</h3><p>${esc(entry.evaluation||'No evaluation details were recorded.')}</p></section>`}${entry.follow_up_question?`<section class="followup-detail"><h3>Follow-up question</h3><p>${esc(entry.follow_up_question)}</p></section>`:''}</article>`;
    }).join('') : '<p class="muted">No interview conversation yet. Answers will appear here as the interview progresses.</p>';
    lastEntriesSignature=signature;
  }
}

async function refresh() {
  try { const response=await fetch('/api/log',{cache:'no-store'}); if(response.ok) update(await response.json()); }
  catch(error) { $('phase').textContent='observer disconnected'; }
}

async function initialize() {
  $('modeSelect').addEventListener('change', updateReview); $('customRoleName').addEventListener('input', updateReview); $('jdText').addEventListener('input', updateReview); $('jdFile').addEventListener('change', handleJdUpload); $('launchBtn').addEventListener('click', launchSession); $('resetBtn').addEventListener('click', resetSession); ensureObserverStopButton();
  try {
    const healthResponse = await fetch('/api/health', {cache:'no-store'});
    const health = await healthResponse.json();
    healthReady = healthResponse.ok;
    if (!healthReady) {
      $('setupError').textContent = `Demo setup is not ready. ${(health.missing || []).join('; ') || 'Check the server configuration.'}`;
    }
    const configResponse = await fetch('/api/session/config', {cache:'no-store'}); const config = await configResponse.json();
    if (config.launched && config.role_id) { selectedRole = {id: config.role_id, name: config.role_name}; showDashboard(); await refresh(); }
    else showOnboarding();
    const modeResponse = await fetch('/api/roles', {cache:'no-store'}); const modeData = await modeResponse.json();
    $('modeSelect').innerHTML = Object.entries(modeData.modes || {}).map(([id, mode]) => `<option value="${esc(id)}">${esc(mode.label || id)} · ${mode.main_question_budget != null ? `${mode.main_question_budget} main + up to ${mode.max_followups_total} follow-ups` : mode.adaptive_turn_budget == null ? 'unbounded' : `${mode.adaptive_turn_budget} answers`}</option>`).join('');
    await loadRoles();
    if (selectedRole?.id) { selectedRole = selectedRole.id === 'custom' ? customRole : roles.find(role => role.id === selectedRole.id) || selectedRole; renderRoles(); updateReview(); }
  } catch (error) { healthReady = false; $('setupError').textContent = error.message || 'Observer setup failed.'; }
}

initialize();
setInterval(() => { if (!$('dashboard').hidden) refresh(); }, 1000);
