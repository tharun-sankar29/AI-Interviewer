const $ = id => document.getElementById(id);
const audioRecorder = new AudioRecorder();
const ui = {
  onboarding: $('candidateOnboarding'), active: $('activeInterview'),
  start: $('startInterviewBtn'), resume: $('resumeInterviewBtn'), device: $('retryDeviceBtn'),
  name: $('candidateFirstName'),
  retry: $('retryAnswerBtn'), again: $('recordAgainBtn'), record: $('voiceBtn'), end: $('candidateStopBtn'),
  attachmentOpen: $('openAttachmentBtn'), attachmentSave: $('saveAttachmentBtn'),
  attachmentCancel: $('cancelAttachmentBtn'), attachmentEdit: $('editAttachmentBtn'),
  attachmentRemove: $('removeAttachmentBtn'), attachmentText: $('attachmentText'),
};
let session = null;
let healthReady = false;
let connected = false;
let generation = 0;
let busy = false;
let recording = false;
let complete = false;
let cameraStream = null;
let pending = null;
let polling = false;
let healthCheckedAt = 0;
let lastQuestionAudioEndedAt = null;
let voiceLatencyMs = null;
let recordingStartedAt = 0;
let timer = null;
let playback = null;
let recordingContext = null;
let videoCapture = null;
let retryDevices = false;
let connectionWarning = false;
let awaitingCompletionReply = false;
let candidateNameTouched = false;
const attachmentStorageKey = 'candidateTextAttachment';
let attachmentScope = null;
let attachedText = '';
let attachmentDraft = '';
let editingAttachment = false;
const controllers = new Set();
const notificationTimers = new Map();

function current(token) { return token === generation; }
function setMessage(message) { $('candidateMessage').textContent = message; }
function addNotification(kind, message) {
  if (!message) return null;
  const notice = document.createElement('section');
  notice.className = `candidate-overlay candidate-${kind}`;
  notice.tabIndex = 0;
  const heading = document.createElement('h2');
  heading.textContent = kind === 'transcript' ? 'Your answer' : 'Interviewer reply';
  const body = document.createElement('p');
  body.textContent = message;
  notice.append(heading, body);
  const stack = $('candidateNotifications');
  stack.append(notice);
  stack.scrollTop = stack.scrollHeight;
  return notice;
}
function removeNotification(notice) {
  if (!notice) return;
  const timers = notificationTimers.get(notice);
  if (timers) {
    clearTimeout(timers.fade);
    clearTimeout(timers.remove);
    notificationTimers.delete(notice);
  }
  notice.remove();
}
function clearNotifications() {
  [...$('candidateNotifications').children].forEach(removeNotification);
}
function fadeNotificationAfter(notice, delayMs = 5000) {
  if (!notice?.isConnected) return;
  const timers = {fade: null, remove: null};
  timers.fade = setTimeout(() => {
    if (!notice.isConnected) return;
    notice.classList.add('is-fading');
    const fadeMs = matchMedia('(prefers-reduced-motion: reduce)').matches ? 20 : 550;
    timers.remove = setTimeout(() => removeNotification(notice), fadeMs);
  }, delayMs);
  notificationTimers.set(notice, timers);
}
function setError(message = '') {
  $('candidateError').textContent = message;
  $('candidateError').hidden = !message;
  $('onboardingError').textContent = message;
}
function badge(text, style = '') {
  $('recordingBadge').textContent = text;
  $('recordingBadge').classList.toggle('live', style === 'live');
  $('recordingBadge').classList.toggle('error', style === 'error');
}
function stopTimer() {
  clearInterval(timer);
  timer = null;
  $('videoTimer').textContent = '00:00';
}
function startTimer() {
  stopTimer();
  recordingStartedAt = performance.now();
  timer = setInterval(() => {
    const seconds = Math.floor((performance.now() - recordingStartedAt) / 1000);
    $('videoTimer').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  }, 250);
}
function showQuestion(state) {
  const text = state.current_question?.question;
  if (text) $('questionText').textContent = text;
  $('questionLabel').textContent = state.current_question?.is_followup ? 'Follow-up Question' : 'Interview Question';
  $('questionNumber').textContent = String(state.question_number || 1);
  $('interviewTitle').textContent = state.role_name || 'AI Interviewer';
  $('interviewCategory').textContent = state.category || 'Interview Practice';
}
function saveAttachmentDraft() {
  try {
    if (attachmentScope && (attachedText || attachmentDraft)) {
      sessionStorage.setItem(attachmentStorageKey, JSON.stringify({
        scope: attachmentScope, attachedText, attachmentDraft, editingAttachment,
      }));
    } else sessionStorage.removeItem(attachmentStorageKey);
  } catch { /* Storage may be unavailable; the in-memory draft still works. */ }
}
function renderAttachment() {
  const saved = Boolean(attachedText);
  const canChange = Boolean(session?.interview_started) && !complete && !recording && !pending && !busy;
  ui.attachmentOpen.hidden = editingAttachment || saved;
  ui.attachmentOpen.setAttribute('aria-expanded', String(editingAttachment));
  $('attachmentEditor').hidden = !editingAttachment;
  $('attachedPreview').hidden = editingAttachment || !saved;
  $('attachedText').textContent = attachedText;
  const status = $('attachmentStatus');
  status.hidden = !saved && !editingAttachment;
  status.textContent = editingAttachment ? 'Editing attachment. Save it before recording.' :
    'Attachment ready. Record your spoken answer to submit both.';
  ui.attachmentText.disabled = !canChange;
  ui.attachmentOpen.disabled = !canChange;
  ui.attachmentSave.disabled = !canChange || !attachmentDraft.trim();
  ui.attachmentCancel.disabled = !canChange;
  ui.attachmentEdit.disabled = !canChange;
  ui.attachmentRemove.disabled = !canChange;
}
function clearAttachment() {
  attachedText = '';
  attachmentDraft = '';
  editingAttachment = false;
  ui.attachmentText.value = '';
  saveAttachmentDraft();
  renderAttachment();
}
function syncAttachment(state) {
  const scope = state.interview_started && state.current_question?.question ?
    JSON.stringify([state.session_id, state.question_number, state.current_question.question]) : null;
  if (scope === attachmentScope) return;
  attachmentScope = scope;
  attachedText = '';
  attachmentDraft = '';
  editingAttachment = false;
  try {
    const stored = JSON.parse(sessionStorage.getItem(attachmentStorageKey) || 'null');
    if (scope && stored?.scope === scope) {
      attachedText = typeof stored.attachedText === 'string' ? stored.attachedText : '';
      attachmentDraft = typeof stored.attachmentDraft === 'string' ? stored.attachmentDraft : attachedText;
      editingAttachment = Boolean(stored.editingAttachment);
    } else sessionStorage.removeItem(attachmentStorageKey);
  } catch { /* Ignore inaccessible or malformed session storage. */ }
  ui.attachmentText.value = attachmentDraft;
  renderAttachment();
}
function updateControls() {
  const started = Boolean(session?.interview_started);
  const usable = connected && !busy && !complete && !playback;
  const validName = /^\p{L}{2,30}(?:[-'’]\p{L}{2,30})?$/u.test(ui.name.value.trim());
  ui.start.disabled = !usable || started || !healthReady || !session?.candidate_ready || !validName;
  ui.start.textContent = busy && !started ? 'Preparing…' : retryDevices && !started ? 'Retry camera and microphone' : 'Start Interview';
  $('resumeFile').disabled = !connected || busy || started;
  $('candidateNameRow').hidden = !session?.candidate_resume_uploaded || started;
  ui.name.disabled = !connected || busy || started;
  ui.record.disabled = !usable || !started || !cameraStream || Boolean(pending) || editingAttachment;
  ui.end.hidden = !started || complete;
  ui.end.disabled = !connected || busy || Boolean(playback);
  ui.resume.hidden = !started || complete || Boolean(cameraStream);
  ui.resume.disabled = !usable;
  ui.device.disabled = !usable;
  ui.retry.hidden = !pending || !pending.retryable || complete;
  ui.retry.disabled = !usable;
  ui.retry.textContent = pending?.answer ? 'Retry answer' : 'Retry transcription';
  ui.again.hidden = !pending || complete;
  ui.again.disabled = !usable || !cameraStream;
  $('voiceBtnLabel').textContent = complete ? 'Interview complete' : recording ? 'Stop and submit answer' : 'Start recording';
  ui.record.classList.toggle('recording', recording);
  document.body.classList.toggle('processing', busy);
  $('sessionStatus').textContent = complete ? 'Interview complete' : !connected ? 'Connection problem' :
    busy ? 'Processing…' : started ? (cameraStream ? 'Interview in progress' : 'Resume your interview') :
    session?.observer_ready ? 'Ready for your resume' : 'Waiting for observer';
  renderAttachment();
}
function cancelPlayback() {
  if (playback) {
    const active = playback;
    playback = null;
    active.audio.pause();
    active.settle();
  }
}
function releaseDevices() {
  discardVideoCapture();
  const stream = cameraStream;
  cameraStream = null;
  stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
  $('cameraPreview').srcObject = null;
  $('videoPlaceholder').style.display = '';
  $('cameraStatus').textContent = 'Camera access required';
  $('micStatus').textContent = 'Microphone access required';
  $('cameraStatus').classList.remove('ready');
  $('micStatus').classList.remove('ready');
  recording = false;
  stopTimer();
  void audioRecorder.destroy().catch(() => {});
}
function invalidate({discard = true, keepNotifications = false} = {}) {
  generation += 1;
  if (!keepNotifications) clearNotifications();
  controllers.forEach(controller => controller.abort());
  controllers.clear();
  cancelPlayback();
  releaseDevices();
  busy = false;
  lastQuestionAudioEndedAt = null;
  voiceLatencyMs = null;
  if (discard) pending = null;
  recordingContext = null;
}
function finishInterview({keepNotifications = false} = {}) {
  if (!complete) invalidate({keepNotifications});
  complete = true;
  clearAttachment();
  ui.onboarding.hidden = true;
  ui.active.hidden = false;
  ui.device.hidden = true;
  setError();
  setMessage('Interview completed. Thank you for your time.');
  badge('COMPLETED');
  updateControls();
}
async function announceCompletion(data, answerNotice = null) {
  const reply = data.reaction || data.response || data.message || '';
  const sendoff = data.closing_message || data.message || '';
  const spoken = reply && sendoff && reply !== sendoff ? `${reply} ${sendoff}` : reply || sendoff;
  const replyNotice = addNotification('reply', spoken);
  finishInterview({keepNotifications: true});
  if (sendoff) setMessage(sendoff);
  await playTTS(spoken, false, generation, true);
  fadeNotificationAfter(answerNotice);
  fadeNotificationAfter(replyNotice);
}
function applyState(next) {
  const changed = session && session.session_id !== next.session_id;
  // A poll can have captured its snapshot before a start/answer completed.
  // Within one session, delayed snapshots must never move the UI backwards.
  if (session && !changed && (
    (session.interview_started && !next.interview_started) ||
    (complete && next.execution_phase !== 'completed') ||
    (next.question_number < session.question_number)
  )) return;
  if (changed) {
    invalidate();
    clearAttachment();
    complete = false;
    $('resumeFile').value = '';
    ui.device.hidden = true;
    retryDevices = false;
    candidateNameTouched = false;
    ui.name.value = '';
    setError('The observer reset the session.');
  }
  const wasStarted = Boolean(session?.interview_started);
  session = next;
  if (!candidateNameTouched) ui.name.value = next.candidate_first_name || '';
  syncAttachment(next);
  connected = true;
  if (connectionWarning && !changed) setError();
  connectionWarning = false;
  if (next.interview_started && next.execution_phase === 'completed') {
    if (awaitingCompletionReply && !changed) return;
    if (!complete && wasStarted && next.closing_message) void announceCompletion(next);
    else {
      finishInterview();
      if (next.closing_message) setMessage(next.closing_message);
    }
    return;
  }
  complete = false;
  ui.onboarding.hidden = Boolean(next.interview_started);
  ui.active.hidden = !next.interview_started;
  $('candidateRole').textContent = next.observer_ready ?
    `${next.role_name || 'Interview'} · ${next.mode} mode` : 'Waiting for observer session…';
  $('resumeStatus').textContent = next.candidate_resume_uploaded ?
    `${next.candidate_resume_filename} uploaded` : 'No resume uploaded';
  if (next.interview_started) {
    showQuestion(next);
    if (!cameraStream && !busy) setMessage('Your interview is active. Click Resume Interview to reconnect your camera and microphone.');
  }
  updateControls();
}

async function fetchJson(url, options = {}, timeout = 45000) {
  const controller = new AbortController();
  controllers.add(controller);
  const deadline = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, {...options, signal: controller.signal, cache: 'no-store'});
    let data;
    try { data = await response.json(); }
    catch { throw Object.assign(new Error('The server returned an unreadable response. Please retry.'), {retryable: response.status >= 500}); }
    if (!response.ok) {
      throw Object.assign(new Error(data.error || `Server error ${response.status}`), {
        status: response.status, code: data.code, retryable: data.retryable ?? (response.status >= 500),
      });
    }
    return data;
  } catch (error) {
    if (error.status === undefined) error.retryable = true;
    throw error;
  } finally {
    clearTimeout(deadline);
    controllers.delete(controller);
  }
}
function postJson(url, data) {
  return fetchJson(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
}

function startVideoCapture() {
  if (!cameraStream || typeof MediaRecorder === 'undefined') return null;
  const types = ['video/webm;codecs=vp8,opus', 'video/webm', 'video/mp4'];
  const type = types.find(candidate => MediaRecorder.isTypeSupported(candidate));
  if (!type) return null;
  try {
    const recorder = new MediaRecorder(cameraStream, {mimeType: type,
      videoBitsPerSecond: 1000000, audioBitsPerSecond: 64000});
    const chunks = [];
    let finish;
    const result = new Promise(resolve => { finish = resolve; });
    const capture = {recorder, result, finish, chunks, discarded: false};
    recorder.addEventListener('dataavailable', event => { if (event.data?.size) chunks.push(event.data); });
    recorder.addEventListener('stop', () => {
      finish(capture.discarded || !chunks.length ? null : new Blob(chunks, {type: recorder.mimeType || type}));
    }, {once: true});
    recorder.addEventListener('error', () => { capture.discarded = true; finish(null); }, {once: true});
    recorder.start();
    videoCapture = capture;
    return capture;
  } catch { return null; }
}
function stopVideoCapture(discard = false) {
  const capture = videoCapture;
  videoCapture = null;
  if (!capture) return Promise.resolve(null);
  capture.discarded ||= discard;
  try {
    if (capture.recorder.state !== 'inactive') capture.recorder.stop();
  } catch { capture.finish(null); }
  return capture.result;
}
function discardVideoCapture() { void stopVideoCapture(true); }

async function deliverVideo(answer) {
  const blob = await answer.video;
  if (!blob?.size || !answer.interview_id) return;
  const extension = blob.type.startsWith('video/mp4') ? 'mp4' : 'webm';
  const params = new URLSearchParams({interview_id: answer.interview_id,
    session_id: answer.session_id, question_number: String(answer.question_number)});
  for (let attempt = 0; ; attempt += 1) {
    try {
      const form = new FormData();
      form.append('interview_id', answer.interview_id);
      form.append('session_id', answer.session_id);
      form.append('question_number', String(answer.question_number));
      form.append('video', blob, `question_${answer.question_number}.${extension}`);
      const uploaded = await fetch('/api/session/recordings', {method: 'POST', body: form});
      if (!uploaded.ok) {
        if (uploaded.status >= 400 && uploaded.status < 500) return;
        throw new Error('Video upload failed');
      }
      if ((await uploaded.json()).status === 'saved') return;
      for (let poll = 0; poll < 12; poll += 1) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const response = await fetch(`/api/session/recordings/status?${params}`, {cache: 'no-store'});
        if (!response.ok) {
          if (response.status >= 400 && response.status < 500) return;
          throw new Error('Video status unavailable');
        }
        const status = (await response.json()).status;
        if (status === 'saved') return;
        if (status === 'failed' || status === 'missing') break;
      }
    } catch { /* Retry while this page stays open. */ }
    await new Promise(resolve => setTimeout(resolve, Math.min(30000, 1000 * 2 ** Math.min(attempt, 5))));
  }
}
async function synchronize() {
  if (polling) return false;
  polling = true;
  const token = generation;
  try {
    const next = await fetchJson('/api/session/state', {}, 45000);
    if (!current(token)) return false;
    applyState(next);
    return true;
  } catch (error) {
    if (current(token)) {
      connected = false;
      connectionWarning = true;
      setError('Connection lost. Your pending answer is retained. Reconnecting…');
      updateControls();
    }
    return false;
  } finally { polling = false; }
}
async function checkHealth() {
  const token = generation;
  try {
    await fetchJson('/api/health', {}, 10000);
    if (current(token)) healthReady = true;
  } catch (error) {
    if (current(token)) {
      healthReady = false;
      if (!session?.interview_started) setError('Interview setup is not ready. Ask the observer to check the server.');
    }
  } finally {
    healthCheckedAt = Date.now();
    if (current(token)) updateControls();
  }
}

async function connectDevices(token) {
  if (!navigator.mediaDevices?.getUserMedia) throw new Error('Camera access requires a supported browser on localhost or HTTPS.');
  let stream = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({video: true, audio: true});
    if (!current(token)) { stream.getTracks().forEach(track => track.stop()); return false; }
    cameraStream = stream;
    $('cameraPreview').srcObject = stream;
    await $('cameraPreview').play();
    if (!current(token)) return false;
    $('videoPlaceholder').style.display = 'none';
    $('cameraStatus').textContent = 'Camera ready';
    $('micStatus').textContent = 'Microphone ready';
    $('cameraStatus').classList.add('ready');
    $('micStatus').classList.add('ready');
    stream.getTracks().forEach(track => {
      track.onended = () => {
        invalidate({discard: false});
        if (!complete) {
          ui.device.hidden = false;
          setError('Camera or microphone disconnected. Reconnect your device, then retry.');
          badge('ACCESS NEEDED', 'error');
          updateControls();
        }
      };
    });
    ui.device.hidden = true;
    retryDevices = false;
    badge('READY');
    return true;
  } catch (error) {
    stream?.getTracks().forEach(track => track.stop());
    if (!current(token)) return false;
    releaseDevices();
    const message = error.name === 'NotAllowedError' ?
      'Camera and microphone permission was denied. Allow access in your browser, then retry.' :
      error.name === 'NotFoundError' ? 'No camera or microphone was found. Connect both devices, then retry.' :
      'Camera and microphone could not be opened. Check device access, then retry.';
    throw new Error(message);
  }
}

async function playTTS(text, markQuestion = false, token = generation, allowCompleted = false) {
  if (!text || !current(token) || (complete && !allowCompleted)) return false;
  let url;
  let controller;
  let endedNormally = false;
  try {
    await audioRecorder.pauseForPlayback();
    if (!current(token)) return;
    controller = new AbortController();
    controllers.add(controller);
    const deadline = setTimeout(() => controller.abort(), 20000);
    let blob;
    try {
      const response = await fetch('/api/tts', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text}), signal: controller.signal,
      });
      if (!response.ok) throw new Error(allowCompleted ?
        'Closing audio is unavailable. You can read the message on screen.' :
        'Question audio is unavailable. Read the displayed question to continue.');
      blob = await response.blob();
    } finally { clearTimeout(deadline); }
    if (!current(token)) return;
    url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    let settle;
    const ended = new Promise(resolve => { settle = resolve; });
    const active = {audio, settle};
    playback = active;
    updateControls();
    audio.addEventListener('ended', () => { endedNormally = true; settle(); }, {once: true});
    audio.addEventListener('error', settle, {once: true});
    // Bound both starting and finishing playback. Cancellation must also
    // settle when the browser has not resolved audio.play() yet.
    const playbackDeadline = setTimeout(settle, 120000);
    try {
      await Promise.race([Promise.resolve(audio.play()), ended]);
      await ended;
    } finally {
      clearTimeout(playbackDeadline);
      audio.pause();
      if (playback === active) playback = null;
    }
    if (current(token) && markQuestion && endedNormally) lastQuestionAudioEndedAt = performance.now();
    return endedNormally;
  } catch (error) {
    if (current(token)) setError(error.message || (allowCompleted ?
      'Closing audio failed. You can read the message on screen.' :
      'Audio playback failed. You can read the question and continue.'));
    return false;
  } finally {
    if (controller) controllers.delete(controller);
    if (url) URL.revokeObjectURL(url);
    if (current(token)) updateControls();
  }
}

async function beginOrResume() {
  if (busy || !connected || complete || !session) return;
  const initial = !session.interview_started;
  if (initial && (!healthReady || !session.candidate_ready)) return;
  busy = true;
  setError();
  updateControls();
  const token = generation;
  try {
    if (!await connectDevices(token)) return;
    const next = await fetchJson('/api/session/state');
    if (!current(token)) return;
    if (next.session_id !== session.session_id) { applyState(next); return; }
    if (next.interview_started && next.execution_phase === 'completed') { applyState(next); return; }
    if (!next.interview_started) {
      const started = await postJson('/api/session/start', {session_id: next.session_id,
        candidate_first_name: ui.name.value.trim()});
      if (!current(token)) return;
      applyState({...next, interview_started: true, candidate_ready: false,
        execution_phase: 'waiting_for_introduction', current_question: {question: started.next_question},
        question_number: started.question_number, interview_id: started.interview_id});
      await playTTS(started.welcome_message || 'Welcome! Let’s begin.', false, token);
    } else applyState(next);
    if (!current(token)) return;
    setMessage('Your camera is live. Click Start recording when you are ready to answer.');
    await playTTS(session.current_question?.question, true, token);
  } catch (error) {
    if (!current(token)) return;
    // The start response may have been lost after the server committed it.
    const synced = await synchronize();
    if (!current(token)) return;
    releaseDevices();
    ui.device.hidden = false;
    retryDevices = true;
    setError(error.message || 'Could not open the interview. Please retry.');
    badge('ACCESS NEEDED', 'error');
    if (!synced) connected = false;
  } finally {
    if (current(token)) { busy = false; updateControls(); }
  }
}

async function processPending() {
  if (!pending || busy || !connected || complete) return;
  busy = true;
  const token = generation;
  const answer = pending;
  setError();
  updateControls();
  try {
    const state = await fetchJson('/api/session/state');
    if (!current(token)) return;
    applyState(state);
    if (!current(token) || complete || pending !== answer) return;
    if (state.question_number !== answer.question_number && !answer.submitted) {
      removeNotification(answer.notice);
      pending = null;
      clearAttachment();
      setError('The question has changed. Record an answer for the displayed question.');
      return;
    }
    if (!answer.answer) {
      setMessage('Transcribing your answer…');
      const data = await fetchJson('/api/transcribe', {method: 'POST', body: answer.audio});
      if (!current(token) || pending !== answer) return;
      answer.answer = data.transcript?.trim();
      if (!answer.answer) throw Object.assign(new Error('No speech was detected. Please record again.'), {retryable: false});
      answer.audio = null;
      answer.notice = addNotification('transcript', answer.attachment ?
        `${answer.answer}\n\nAttached text/code:\n${answer.attachment}` : answer.answer);
    }
    setMessage('Evaluating your answer…');
    answer.submitted = true;
    awaitingCompletionReply = true;
    let data;
    try {
      data = await postJson('/api/answer', {
        answer: answer.attachment ? `${answer.answer}\n\nAttached text/code:\n${answer.attachment}` : answer.answer,
        ...(answer.attachment ? {} : {latency_ms: answer.latency_ms}), input_mode: 'voice',
        session_id: answer.session_id, question_number: answer.question_number, request_id: answer.request_id,
      });
    } finally { awaitingCompletionReply = false; }
    void deliverVideo(answer);
    if (!current(token) || pending !== answer) return;
    pending = null;
    clearAttachment();
    const reply = data.reaction || data.response || data.message || '';
    if (data.status === 'completed') {
      await announceCompletion(data, answer.notice);
      return;
    }
    const replyNotice = addNotification('reply', reply);
    applyState({...session, current_question: data.current || {question: data.next_question},
      question_number: data.question_number, execution_phase: 'waiting_for_answer'});
    await playTTS(reply, false, token);
    if (!current(token)) return;
    fadeNotificationAfter(answer.notice);
    fadeNotificationAfter(replyNotice);
    await playTTS(session.current_question?.question, true, token);
    if (current(token)) setMessage('Click Start recording when you are ready to answer.');
  } catch (error) {
    if (current(token) && pending === answer) {
      if (error.status === 409) {
        removeNotification(answer.notice);
        pending = null;
        await synchronize();
      } else answer.retryable = Boolean(error.retryable);
      if (current(token)) setError(error.message || 'Your answer could not be processed. Please retry.');
    }
  } finally {
    if (current(token)) {
      busy = false;
      badge(complete ? 'COMPLETED' : pending ? 'RETRY NEEDED' : 'READY', pending ? 'error' : '');
      updateControls();
    }
  }
}

audioRecorder.onStartRecording = () => {
  recording = true;
  recordingContext = {session_id: session.session_id, question_number: session.question_number,
    interview_id: session.interview_id, attachment: attachedText};
  if (!startVideoCapture()) throw new Error('Video recording is unavailable in this browser or with this camera. Try Chromium and reconnect your devices.');
  startTimer();
  badge('RECORDING', 'live');
  setMessage('Recording… Your answer submits after 1.4 seconds of silence.');
  updateControls();
};
audioRecorder.onSpeechStart = () => {
  voiceLatencyMs = lastQuestionAudioEndedAt === null ? null : Math.max(0, performance.now() - lastQuestionAudioEndedAt);
};
audioRecorder.onStopRecording = formData => {
  const video = stopVideoCapture();
  if (complete || !session?.interview_started || !cameraStream || !recordingContext) return;
  recording = false;
  stopTimer();
  pending = {
    audio: formData, video, answer: null, latency_ms: voiceLatencyMs,
    attachment: recordingContext.attachment,
    interview_id: recordingContext.interview_id,
    session_id: recordingContext.session_id, question_number: recordingContext.question_number,
    request_id: crypto.randomUUID(), retryable: true, submitted: false,
  };
  voiceLatencyMs = null;
  recordingContext = null;
  badge('PROCESSING');
  setMessage(connected ? 'Processing your answer…' : 'Your answer is retained. Reconnect, then retry.');
  updateControls();
  void processPending();
};
audioRecorder.onRecordingFailed = (error, fatal = true) => {
  discardVideoCapture();
  if (complete || !session?.interview_started) return;
  recording = false;
  stopTimer();
  if (fatal) {
    invalidate({discard: false});
    ui.device.hidden = false;
  }
  setError(error.message || 'Recording failed. Please retry.');
  badge('ERROR', 'error');
  updateControls();
};

ui.record.addEventListener('click', async () => {
  if (ui.record.disabled) return;
  const token = generation;
  if (recording) {
    try { await audioRecorder.stopRecording(); }
    catch (error) { if (current(token)) audioRecorder.onRecordingFailed(error, true); }
    return;
  }
  busy = true;
  setError();
  updateControls();
  try {
    const state = await fetchJson('/api/session/state');
    if (!current(token)) return;
    applyState(state);
    if (!current(token) || complete || !cameraStream || editingAttachment) return;
    await audioRecorder.startRecording();
  } catch (error) {
    if (current(token)) setError(error.message || 'Could not start recording.');
  } finally { if (current(token)) { busy = false; updateControls(); } }
});
ui.attachmentOpen.addEventListener('click', () => {
  if (ui.attachmentOpen.disabled) return;
  editingAttachment = true;
  attachmentDraft = attachedText;
  ui.attachmentText.value = attachmentDraft;
  saveAttachmentDraft();
  updateControls();
  ui.attachmentText.focus();
});
ui.attachmentEdit.addEventListener('click', () => {
  if (ui.attachmentEdit.disabled) return;
  editingAttachment = true;
  attachmentDraft = attachedText;
  ui.attachmentText.value = attachmentDraft;
  saveAttachmentDraft();
  updateControls();
  ui.attachmentText.focus();
});
ui.attachmentText.addEventListener('input', () => {
  attachmentDraft = ui.attachmentText.value;
  saveAttachmentDraft();
  renderAttachment();
});
ui.attachmentSave.addEventListener('click', () => {
  if (ui.attachmentSave.disabled) return;
  attachedText = attachmentDraft;
  editingAttachment = false;
  saveAttachmentDraft();
  updateControls();
});
ui.attachmentCancel.addEventListener('click', () => {
  if (ui.attachmentCancel.disabled) return;
  attachmentDraft = attachedText;
  ui.attachmentText.value = attachmentDraft;
  editingAttachment = false;
  saveAttachmentDraft();
  updateControls();
});
ui.attachmentRemove.addEventListener('click', () => {
  if (ui.attachmentRemove.disabled) return;
  clearAttachment();
  updateControls();
});
ui.start.addEventListener('click', beginOrResume);
ui.name.addEventListener('input', () => { candidateNameTouched = true; updateControls(); });
ui.resume.addEventListener('click', beginOrResume);
ui.device.addEventListener('click', beginOrResume);
ui.retry.addEventListener('click', processPending);
ui.again.addEventListener('click', () => {
  if (ui.again.disabled) return;
  removeNotification(pending?.notice);
  pending = null;
  setError();
  setMessage('Click Start recording to record your answer again.');
  badge('READY');
  updateControls();
});
ui.end.addEventListener('click', async () => {
  if (ui.end.disabled || !window.confirm('End this interview and generate the report now?')) return;
  const token = generation;
  busy = true;
  updateControls();
  try {
    await audioRecorder.cancel();
    awaitingCompletionReply = true;
    let data;
    try { data = await postJson('/api/session/stop', {session_id: session.session_id}); }
    finally { awaitingCompletionReply = false; }
    if (current(token) && data.status === 'completed') await announceCompletion(data);
  } catch (error) {
    if (current(token)) setError(error.message || 'Could not end the interview. Please retry.');
  } finally { if (current(token)) { busy = false; updateControls(); } }
});
$('resumeFile').addEventListener('change', async () => {
  const file = $('resumeFile').files?.[0];
  if (!file || busy || session?.interview_started) return;
  const token = generation;
  busy = true;
  setError();
  updateControls();
  try {
    const form = new FormData();
    form.append('resume', file);
    form.append('session_id', session.session_id);
    const uploaded = await fetchJson('/api/session/resume/upload', {method: 'POST', body: form});
    if (current(token)) {
      candidateNameTouched = false;
      ui.name.value = uploaded.candidate_first_name || '';
      await synchronize();
    }
  } catch (error) {
    if (current(token)) setError(error.message || 'Resume upload failed.');
  } finally { if (current(token)) { busy = false; updateControls(); } }
});

document.addEventListener('visibilitychange', () => {
  if (document.hidden) cancelPlayback();
  else void synchronize();
});
window.addEventListener('beforeunload', () => invalidate());
setInterval(() => {
  void synchronize();
  if (!busy && Date.now() - healthCheckedAt > 15000) void checkHealth();
}, 2000);
void (async () => {
  await synchronize();
  await checkHealth();
})();
