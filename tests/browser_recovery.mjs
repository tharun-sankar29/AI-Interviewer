// Uses Chromium's DevTools protocol and Node's built-in WebSocket; no browser
// test dependencies are needed. Devices/providers are controlled test doubles.
import {createServer} from 'node:http';
import {readFile, mkdtemp, rm} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import assert from 'node:assert/strict';

const fixture = String.raw`
window.fixture = {
  sendoff:'Thank you for sharing your experience today. I appreciate the thought you put into your answers. Your interview is complete, and your report is ready for review. Take care, and best of luck with what comes next.',
  state: {session_id:'s1',interview_id:'interview_20261007_123456_000001',observer_ready:true,candidate_resume_uploaded:true,candidate_resume_filename:'resume.txt',
    candidate_ready:true,candidate_first_name:null,interview_started:false,role_name:'Backend Engineer',category:'Engineering',mode:'quick',
    execution_phase:'idle',current_question:null,question_number:0,evaluated_answer_count:0},
  calls: {}, tracks: [], ttsTexts:[], answerPayloads:[], transcriptText:'I built APIs with transactions and reliable database design.', deny:0, transcriptErrors:0, answerErrors:0, ttsErrors:0, loseAnswer:false,
  receipts:{}, offline:false, holdAudio:false, releaseAudio:null, holdAudioStart:false, releaseAudioStart:null, holdAnswer:false, releaseAnswer:null,
  holdTranscription:false, held:null, destroyed:0, completeNext:false,
  holdDevices:false, releaseDevice:null, holdVad:false, releaseVad:null, startErrors:0,
  delayNextState:false, releaseState:null, videoCaptures:[], videoUploads:[], holdVideoUpload:false, releaseVideoUpload:null,
};
const F = window.fixture;
const originalFetch = window.fetch.bind(window);
const response = (body, status=200) => new Response(JSON.stringify(body), {status, headers:{'Content-Type':'application/json'}});
window.confirm = () => true;
Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {value:async constraints => {
  F.calls.devices = (F.calls.devices || 0) + 1;
  if (F.deny-- > 0) throw new DOMException('Denied', 'NotAllowedError');
  if (F.holdDevices) await new Promise(resolve => {F.releaseDevice=resolve;});
  const stream = new MediaStream();
  if (constraints.video) {
    const canvas = document.createElement('canvas'); canvas.width=32; canvas.height=32;
    canvas.getContext('2d').fillRect(0,0,32,32);
    canvas.captureStream().getVideoTracks().forEach(track => stream.addTrack(track));
  }
  if (constraints.audio) {
    const context = new AudioContext();
    const dest = context.createMediaStreamDestination();
    dest.stream.getAudioTracks().forEach(track => stream.addTrack(track));
  }
  F.tracks.push(...stream.getTracks());
  return stream;
}});
HTMLMediaElement.prototype.play = async function() {};
window.Audio = class extends EventTarget {
  constructor() {super();}
  async play() {
    if (F.holdAudioStart) return new Promise(resolve => {F.releaseAudioStart=resolve;});
    if (F.holdAudio) F.releaseAudio=()=>{this.dispatchEvent(new Event('ended'));};
    else setTimeout(() => this.dispatchEvent(new Event('ended')), 0);
  }
  pause() {}
};
window.MediaRecorder = class extends EventTarget {
  static isTypeSupported(type) {return type.startsWith('video/webm');}
  constructor(stream, options={}) {super();this.stream=stream;this.mimeType=options.mimeType;this.state='inactive';}
  start() {this.state='recording';F.videoCaptures.push(this);}
  stop() {
    this.state='inactive';
    this.dispatchEvent(new BlobEvent('dataavailable',{data:new Blob([new Uint8Array([0x1a,0x45,0xdf,0xa3,1])],{type:'video/webm'})}));
    this.dispatchEvent(new Event('stop'));
  }
};
window.vad = {MicVAD:{new:async options => {
  const stream = await options.getStream();
  if (F.holdVad) await new Promise(resolve => {F.releaseVad=resolve;});
  const detector = {start:async()=>{},pause:async()=>{},destroy:async()=>{F.destroyed++;}};
  F.speech = () => options.onSpeechEnd(new Float32Array(16000));
  return detector;
}}};
window.fetch = async (url, options={}) => {
  if (!String(url).startsWith('/api/')) return originalFetch(url, options);
  F.calls[url] = (F.calls[url] || 0) + 1;
  if (F.offline) throw new TypeError('Network disconnected');
  if (url === '/api/health') return response({status:'ready'});
  if (url === '/api/session/state') {
    const snapshot=response(F.state);
    if (F.delayNextState) {F.delayNextState=false;await new Promise(resolve=>{F.releaseState=resolve;});}
    return snapshot;
  }
  if (url === '/api/session/start') {
    if (F.startErrors-- > 0) return response({error:'Start service unavailable.',retryable:true},503);
    const firstName=JSON.parse(options.body).candidate_first_name;
    F.state.candidate_first_name=firstName;
    F.state = {...F.state,interview_started:true,candidate_ready:false,execution_phase:'waiting_for_introduction',
      current_question:{question:'Introduce yourself.'},question_number:1};
    return response({status:'started',interview_id:F.state.interview_id,welcome_message:'Welcome, '+firstName+'! Let\'s begin.',next_question:'Introduce yourself.',question_number:1});
  }
  if (url === '/api/session/resume/upload') {F.state.candidate_resume_uploaded=true;F.state.candidate_ready=true;return response({status:'uploaded'});}
  if (url === '/api/transcribe') {
    if (F.holdTranscription) await new Promise(resolve => {F.held=resolve;});
    if (F.transcriptErrors-- > 0) return response({error:'Transcription unavailable.'},503);
    return response({transcript:F.transcriptText});
  }
  if (url === '/api/answer') {
    const data = JSON.parse(options.body);
    F.answerPayloads.push(data);
    if (F.holdAnswer) await new Promise(resolve => {F.releaseAnswer=resolve;});
    if (F.answerErrors-- > 0) return response({error:'Evaluation unavailable.',retryable:true},503);
    if (F.receipts[data.request_id]) return response(F.receipts[data.request_id]);
    if (data.session_id !== F.state.session_id || data.question_number !== F.state.question_number) return response({error:'Question changed.',code:'QUESTION_CHANGED'},409);
    let result;
    if (F.completeNext) {
      F.completeNext=false;
      F.state.execution_phase='completed';
      F.state.closing_message=F.sendoff;
      result={status:'completed',reaction:'This is the final interviewer reply.',message:F.sendoff,closing_message:F.sendoff};
    } else {
      F.state.question_number++;
      F.state.current_question = {question:'Question '+F.state.question_number};
      result = {status:'ongoing',reaction:'Good explanation.',next_question:F.state.current_question.question,
        current:F.state.current_question,question_number:F.state.question_number};
    }
    F.receipts[data.request_id] = result;
    if (F.loseAnswer) {F.loseAnswer=false; throw new TypeError('Response lost');}
    return response(result);
  }
  if (url === '/api/session/stop') {F.state.execution_phase='completed';F.state.closing_message=F.sendoff;return response({status:'completed',reaction:F.sendoff,message:F.sendoff,closing_message:F.sendoff});}
  if (url === '/api/session/recordings') {
    const body=options.body;
    F.videoUploads.push({interview_id:body.get('interview_id'),session_id:body.get('session_id'),
      question_number:Number(body.get('question_number')),file:body.get('video')});
    if (F.holdVideoUpload) await new Promise(resolve => {F.releaseVideoUpload=resolve;});
    return response({status:'saved'});
  }
  if (String(url).startsWith('/api/session/recordings/status?')) return response({status:'saved'});
  if (url === '/api/tts') {F.ttsTexts.push(JSON.parse(options.body).text);if(F.ttsErrors-- > 0)return response({error:'Audio unavailable'},503);return new Response(new Blob(['test audio'],{type:'audio/wav'}));}
  throw new Error('Unexpected API: '+url);
};
`;

const scenarios = String.raw`
(async () => {
  const F = window.fixture;
  const results = [];
  const pushResult = results.push.bind(results);
  results.push = name => {console.log('PASS '+name);return pushResult(name);};
  const assert = (value,message) => {if(!value) throw new Error(message);};
  const wait = async predicate => {
    const end=Date.now()+8000;
    while(!predicate()) {if(Date.now()>end) throw new Error('Timed out waiting for UI'); await new Promise(r=>setTimeout(r,10));}
  };
  const notices=() => [...$('candidateNotifications').children];
  const latestNotice=kind => notices().filter(item=>item.classList.contains('candidate-'+kind)).at(-1);
  await wait(() => session && healthReady);
  assert(!ui.onboarding.hidden && ui.active.hidden,'Onboarding is first');
  assert(!F.calls.devices,'No permission request on load');
  assert(ui.start.disabled && !ui.name.value && !$('candidateNameRow').hidden,'Missing resume name requires candidate input');
  ui.name.value='Priya';ui.name.dispatchEvent(new Event('input'));
  assert(!ui.start.disabled,'Valid first name enables interview start');
  F.deny=1;
  await beginOrResume();
  assert(!F.calls['/api/session/start'],'Denied permissions must not start session');
  assert($('onboardingError').textContent.includes('denied'),'Permission error visible');
  assert(ui.start.textContent.includes('Retry'),'Initial device retry button visible');
  results.push('permission denial and retry');
  F.startErrors=1;
  await beginOrResume();
  assert(!cameraStream && !F.state.interview_started && F.tracks.every(track=>track.readyState==='ended'),'Backend start failure releases acquired devices');
  assert($('onboardingError').textContent.includes('unavailable'),'Start failure visible');
  results.push('start failure releases devices');
  const attempts=F.calls['/api/session/start'];
  await Promise.all([beginOrResume(),beginOrResume()]);
  assert(F.calls['/api/session/start']===attempts+1,'Start once on repeated click');
  assert(F.ttsTexts[0]==='Welcome, Priya! Let\'s begin.','Start speaks the personalized server greeting');
  results.push('personalized welcome is spoken from start response');
  assert(cameraStream && !ui.active.hidden && ui.onboarding.hidden,'Interview active');
  assert(F.videoCaptures.length===0,'Welcome and question playback must not record video');
  assert(getComputedStyle(document.querySelector('.history')).display==='none','History hidden');
  const code = 'function total(items) {\n    return items.reduce((sum, item) => sum + item, 0);\n}';
  const answerCallsBeforeAttachment = F.calls['/api/answer'] || 0;
  ui.attachmentOpen.click();
  assert(editingAttachment && ui.record.disabled && !$('attachmentEditor').hidden,'Editor opens and blocks recording');
  ui.attachmentText.value = code;
  ui.attachmentText.dispatchEvent(new Event('input'));
  assert(!ui.attachmentSave.disabled && (F.calls['/api/answer'] || 0) === answerCallsBeforeAttachment,'Text alone cannot submit');
  ui.attachmentSave.click();
  assert(attachedText === code && !$('attachedPreview').hidden && $('attachmentStatus').textContent.includes('ready'),'Multiline attachment saved');
  ui.attachmentEdit.click();
  assert(editingAttachment && ui.record.disabled && ui.attachmentText.value === code,'Saved attachment can be edited');
  ui.attachmentText.value = code + '\n// updated'; ui.attachmentText.dispatchEvent(new Event('input'));
  ui.attachmentCancel.click();
  assert(attachedText === code && !editingAttachment,'Cancel keeps saved attachment');
  ui.attachmentEdit.click();
  ui.attachmentText.value = code + '\n// updated'; ui.attachmentText.dispatchEvent(new Event('input'));
  ui.attachmentSave.click();
  ui.attachmentRemove.click();
  assert(!attachedText && $('attachmentEditor').hidden && !ui.attachmentOpen.hidden,'Remove clears attachment');
  ui.attachmentOpen.click(); ui.attachmentText.value = code; ui.attachmentText.dispatchEvent(new Event('input')); ui.attachmentSave.click();
  results.push('open, edit, cancel, remove and save multiline code');
  const previewTracks = cameraStream.getTracks();
  let firstReplyNotice;
  for(let turn=0;turn<3;turn++) {
    if(turn===0) {F.holdAnswer=true;F.holdAudio=true;F.holdVideoUpload=true;}
    ui.record.click(); await wait(()=>recording && !busy);
    assert(F.videoCaptures.at(-1)?.state==='recording','Camera and microphone capture starts with answer recording');
    if (turn === 0) assert(ui.attachmentEdit.disabled && ui.attachmentRemove.disabled,'Attachment locked during recording');
    F.speech();
    await wait(()=>F.videoCaptures.at(-1)?.state==='inactive');
    if (turn === 1) {
      await wait(()=>latestNotice('transcript') && notices().length>=3);
      assert(firstReplyNotice.isConnected && notices().indexOf(firstReplyNotice)<notices().indexOf(latestNotice('transcript')),'Next answer appears below older notifications');
    }
    if(turn===0) {
      await wait(()=>F.releaseAnswer && latestNotice('transcript'));
      const payload=F.answerPayloads.at(-1);
      assert(payload.answer === F.transcriptText + '\n\nAttached text/code:\n' + code,'Spoken answer and exact multiline code reach API');
      assert(!('latency_ms' in payload),'Attachment omits speech latency');
      const transcriptNotice=latestNotice('transcript');
      assert(transcriptNotice.querySelector('p').textContent === payload.answer,'Combined answer visible to candidate');
      assert(document.querySelector('.video-stage').contains(transcriptNotice),'Transcript is over the camera panel');
      F.releaseAnswer();F.holdAnswer=false;
      await wait(()=>F.releaseAudio && latestNotice('reply'));
      const replyNotice=latestNotice('reply');
      firstReplyNotice=replyNotice;
      assert(document.querySelector('.video-stage').contains(replyNotice),'Reply is over the camera panel');
      assert(notices()[0]===transcriptNotice && notices()[1]===replyNotice && replyNotice.querySelector('p').textContent==='Good explanation.','Reply stacks beneath transcript');
      assert(!transcriptNotice.classList.contains('is-fading') && !replyNotice.classList.contains('is-fading'),'Both notices stay visible during reply audio');
      assert(F.videoCaptures.at(-1).state==='inactive','Interviewer reply playback is outside the video clip');
      F.releaseAudio();F.holdAudio=false;
      await wait(()=>!playback);
      await wait(()=>F.releaseVideoUpload);
      assert(session.question_number===2 && !busy,'Slow video upload does not hold the next question');
      F.holdVideoUpload=false;F.releaseVideoUpload();
      await new Promise(r=>setTimeout(r,150));
      assert(transcriptNotice.isConnected && replyNotice.isConnected && !replyNotice.classList.contains('is-fading'),'Short reply remains visible after audio');
      results.push('camera notifications stack and persist after spoken reply');
    }
    await wait(()=>!busy && !pending && !recording);
    await wait(()=>F.videoUploads.length===turn+1);
    assert(F.videoUploads.at(-1).question_number===turn+1 && F.videoUploads.at(-1).session_id==='s1' &&
      F.videoUploads.at(-1).interview_id===F.state.interview_id && F.videoUploads.at(-1).file.size>0,
      'Each accepted answer uploads its numbered video and microphone clip');
    if (turn === 0) assert(!attachedText && !sessionStorage.getItem(attachmentStorageKey),'Accepted answer clears attachment');
    assert(previewTracks.every(track=>track.readyState==='live'),'Preview survives answer cycles');
  }
  const dismissalStartedAt=performance.now();
  await wait(()=>notices().length===0);
  assert(performance.now()-dismissalStartedAt>=4500,'Notifications remain for about five seconds after reply audio');
  assert(F.state.question_number===4,'Three questions progressed');
  assert(F.answerPayloads.at(-1).answer === F.transcriptText && 'latency_ms' in F.answerPayloads.at(-1),'Voice-only payload unchanged');
  results.push('three answer cycles and continuous preview');
  results.push('answer-only camera recording and accepted clip uploads');
  results.push('slow video upload does not delay question progression');
  results.push('spoken LLM reply visible to interviewee');
  F.ttsErrors=1;
  ui.record.click(); await wait(()=>recording && !busy); F.speech(); await wait(()=>!busy && !pending && !recording);
  const silentReply=latestNotice('reply');
  await new Promise(r=>setTimeout(r,150));
  assert(silentReply?.isConnected && !silentReply.classList.contains('is-fading'),'Reply remains visible when audio playback fails');
  results.push('reply notification persists when audio playback fails');
  ui.attachmentOpen.click(); ui.attachmentText.value = code; ui.attachmentText.dispatchEvent(new Event('input')); ui.attachmentSave.click();
  F.transcriptText = '';
  const answerCallsBeforeSilence = F.calls['/api/answer'];
  const uploadsBeforeSilence = F.videoUploads.length;
  ui.record.click(); await wait(()=>recording && !busy); F.speech(); await wait(()=>pending && !busy);
  assert(F.calls['/api/answer'] === answerCallsBeforeSilence && !ui.again.hidden,'Attachment without spoken words cannot submit');
  ui.again.click();
  assert(F.videoUploads.length===uploadsBeforeSilence,'Unaccepted empty speech does not upload a clip');
  assert(attachedText === code,'Record again keeps attachment after empty speech');
  ui.attachmentRemove.click();
  F.transcriptText = 'I built APIs with transactions and reliable database design.';
  results.push('attachment alone cannot submit without speech');
  ui.record.click(); await wait(()=>recording && !busy);
  F.delayNextState=true;
  const delayedPoll=synchronize(); await wait(()=>F.releaseState);
  F.speech(); await wait(()=>!busy && !pending && !recording);
  const advancedNumber=session.question_number;
  F.releaseState(); await delayedPoll;
  assert(session.question_number===advancedNumber && $('questionText').textContent===F.state.current_question.question,'Delayed poll cannot regress question');
  results.push('delayed state snapshot cannot regress question');
  F.transcriptErrors=1;
  ui.attachmentOpen.click(); ui.attachmentText.value = code; ui.attachmentText.dispatchEvent(new Event('input')); ui.attachmentSave.click();
  ui.record.click(); await wait(()=>recording && !busy); F.speech(); await wait(()=>pending && !busy);
  const transcriptions=F.calls['/api/transcribe'];
  assert(!ui.retry.hidden && ui.retry.textContent==='Retry transcription','Transcription retry offered');
  assert(pending.attachment === code && attachedText === code && ui.attachmentEdit.disabled,'Attachment retained through transcription failure');
  assert(!$('candidateError').hidden,'Processing error visible outside history');
  await processPending();
  assert(!pending && F.calls['/api/transcribe']===transcriptions+1,'Same audio retried');
  assert(F.answerPayloads.at(-1).answer.endsWith('Attached text/code:\n' + code),'Retry submits attachment');
  results.push('retained audio transcription retry');
  F.answerErrors=1;
  ui.attachmentOpen.click(); ui.attachmentText.value = code; ui.attachmentText.dispatchEvent(new Event('input')); ui.attachmentSave.click();
  ui.record.click(); await wait(()=>recording && !busy); F.speech(); await wait(()=>pending && !busy);
  const transcriptCalls=F.calls['/api/transcribe'];
  const requestId=pending.request_id;
  assert(pending.answer && !pending.audio && !ui.retry.hidden,'Transcript preserved');
  assert(pending.attachment === code && attachedText === code,'Evaluation retry retains attachment');
  const retryNotice=pending.notice;
  assert(retryNotice?.isConnected,'Transcript remains visible while answer retry is needed');
  await processPending();
  assert(F.calls['/api/transcribe']===transcriptCalls && F.receipts[requestId],'Retry did not retranscribe');
  assert(F.answerPayloads.at(-1).answer.endsWith('Attached text/code:\n' + code),'Evaluation retry resends same attachment');
  assert(retryNotice.isConnected && notices().filter(item=>item===retryNotice).length===1,'Evaluation retry keeps the same transcript notification');
  results.push('retained transcript evaluation retry');
  F.answerErrors=1;
  ui.attachmentOpen.click(); ui.attachmentText.value = code; ui.attachmentText.dispatchEvent(new Event('input')); ui.attachmentSave.click();
  ui.record.click(); await wait(()=>recording && !busy); F.speech(); await wait(()=>pending && !busy);
  const discardedNotice=pending.notice;
  const uploadsBeforeDiscard=F.videoUploads.length;
  ui.again.click();
  assert(F.videoUploads.length===uploadsBeforeDiscard,'Discarded answer does not upload video');
  assert(!pending && !ui.record.disabled,'Record again discards retained input');
  assert(!discardedNotice.isConnected,'Record again removes discarded transcript notification');
  assert(attachedText === code && !ui.attachmentEdit.disabled,'Record again keeps editable attachment');
  results.push('record again discards pending input');
  F.loseAnswer=true;
  ui.record.click(); await wait(()=>recording && !busy); F.speech(); await wait(()=>pending && !busy);
  const acceptedNumber=F.state.question_number;
  await synchronize();
  await processPending();
  assert(!pending && F.state.question_number===acceptedNumber,'Lost response retry did not advance twice');
  assert(!attachedText,'Reconciled accepted answer clears attachment');
  results.push('lost answer response safely reconciled');
  invalidate({discard:false});
  await synchronize();
  assert(!ui.resume.hidden && !cameraStream,'Resume button on restored active session');
  const starts=F.calls['/api/session/start'];
  await beginOrResume();
  assert(F.calls['/api/session/start']===starts && cameraStream,'Resume without restart');
  results.push('resume current question without restart');
  F.offline=true;
  await synchronize();
  assert(ui.record.disabled && !connected,'Connection failure disables recording');
  F.offline=false; await synchronize();
  assert(connected && !ui.record.disabled,'Connection recovery restores controls');
  results.push('connection recovery');
  F.holdAudioStart=true;
  const delayedStart=playTTS('Question with delayed playback',true);
  await wait(()=>playback && F.releaseAudioStart);
  cancelPlayback();await delayedStart;
  assert(!playback && !ui.record.disabled,'Cancellation settles pending audio.play and restores controls');
  F.holdAudioStart=false;F.releaseAudioStart();
  results.push('cancellation during pending audio startup');
  F.holdAudio=true;
  const speaking=playTTS('Question to cancel',true);
  await wait(()=>playback);
  assert(ui.record.disabled,'Recording blocked during speech');
  const speechBeforeObserverStop=F.ttsTexts.length;
  F.state.execution_phase='completed'; F.state.closing_message=F.sendoff;
  await synchronize(); await speaking;
  await wait(()=>F.ttsTexts.length>speechBeforeObserverStop && playback);
  assert(complete && !cameraStream && F.tracks.every(track=>track.readyState==='ended'),'Observer completion releases all device streams');
  assert(F.ttsTexts.at(-1)===F.sendoff && latestNotice('reply')?.querySelector('p').textContent===F.sendoff,'Observer completion speaks and shows send-off');
  assert($('sessionStatus').textContent==='Interview complete','Completion status retained');
  F.releaseAudio(); await wait(()=>!playback);
  results.push('observer stop during playback and complete cleanup');
  F.holdAudio=false;
  F.state={...F.state,session_id:'s2',interview_started:false,candidate_ready:true,execution_phase:'idle',question_number:0,current_question:null};
  await synchronize(); await beginOrResume();
  F.holdTranscription=true;
  ui.attachmentOpen.click(); ui.attachmentText.value = code; ui.attachmentText.dispatchEvent(new Event('input')); ui.attachmentSave.click();
  ui.record.click(); await wait(()=>recording && !busy); F.speech(); await wait(()=>F.held);
  const answerCalls=F.calls['/api/answer'];
  F.state={...F.state,session_id:'s3',interview_started:false,candidate_ready:false,candidate_resume_uploaded:false,execution_phase:'idle',question_number:0,current_question:null};
  await synchronize();
  F.held(); await new Promise(r=>setTimeout(r,30));
  assert(!pending && !cameraStream && !ui.onboarding.hidden,'Reset clears pending work and returns onboarding');
  assert(notices().length===0,'Reset clears camera notifications');
  assert(!attachedText && !sessionStorage.getItem(attachmentStorageKey),'Reset clears attachment');
  assert(F.calls['/api/answer']===answerCalls,'Late transcription ignored');
  assert(F.tracks.every(track=>track.readyState==='ended'),'VAD and preview tracks stopped on reset');
  results.push('observer reset and late response rejection');
  F.holdTranscription=false;
  F.state={...F.state,candidate_ready:true,candidate_resume_uploaded:true}; await synchronize();
  F.holdDevices=true;
  const connecting=beginOrResume(); await wait(()=>F.releaseDevice);
  F.state={...F.state,session_id:'s4',candidate_ready:false,candidate_resume_uploaded:false}; await synchronize();
  F.holdDevices=false; F.releaseDevice(); await connecting;
  assert(!cameraStream && F.tracks.every(track=>track.readyState==='ended'),'Late permission stream released after reset');
  results.push('late camera permission result released');
  F.state={...F.state,candidate_ready:true,candidate_resume_uploaded:true}; await synchronize(); await beginOrResume();
  F.holdVad=true;
  ui.record.click(); await wait(()=>F.releaseVad);
  F.state={...F.state,session_id:'s5',interview_started:false,candidate_ready:false,candidate_resume_uploaded:false,execution_phase:'idle',current_question:null,question_number:0};
  await synchronize(); F.holdVad=false; F.releaseVad(); await wait(()=>!audioRecorder.initializing);
  await new Promise(r=>setTimeout(r,20));
  assert(!cameraStream && F.tracks.every(track=>track.readyState==='ended'),'Reset during VAD initialization releases microphones');
  results.push('reset during VAD initialization');
  F.state={...F.state,candidate_ready:true,candidate_resume_uploaded:true}; await synchronize(); await beginOrResume();
  ui.record.click(); await wait(()=>recording && !busy);
  F.state.execution_phase='completed'; await synchronize();
  const submitted=F.calls['/api/answer']; F.speech(); await new Promise(r=>setTimeout(r,20));
  assert(complete && !pending && F.calls['/api/answer']===submitted && F.tracks.every(track=>track.readyState==='ended'),'Observer stop cancels recording and ignores stale VAD');
  results.push('observer stop during recording');
  F.state={...F.state,session_id:'s6',interview_started:true,candidate_ready:false,execution_phase:'waiting_for_answer',current_question:{question:'Final test question.'},question_number:1};
  await synchronize(); await beginOrResume(); F.completeNext=true;
  ui.attachmentOpen.click(); ui.attachmentText.value = code; ui.attachmentText.dispatchEvent(new Event('input')); ui.attachmentSave.click();
  ui.record.click(); await wait(()=>recording && !busy); F.speech(); await wait(()=>complete && !busy);
  assert(!attachedText && !sessionStorage.getItem(attachmentStorageKey),'Completion clears attachment');
  assert(latestNotice('reply')?.querySelector('p').textContent==='This is the final interviewer reply. '+F.sendoff && latestNotice('transcript')?.isConnected,'Final transcript, reply, and send-off remain visible after completion');
  assert(F.ttsTexts.at(-1)==='This is the final interviewer reply. '+F.sendoff,'Final interviewer reply and send-off are spoken');
  await wait(()=>notices().length===0);
  assert(complete,'Final notifications fade without changing completed state');
  results.push('final interviewer reply and send-off spoken after completion');
  F.state={...F.state,session_id:'manual-stop-session',interview_started:true,execution_phase:'waiting_for_answer',closing_message:null,current_question:{question:'Manual stop test question.'},question_number:1};
  await synchronize(); await beginOrResume();
  const speechBeforeManualStop=F.ttsTexts.length;
  ui.end.click();
  await wait(()=>complete && F.ttsTexts.length>speechBeforeManualStop);
  assert(F.ttsTexts.at(-1)===F.sendoff && latestNotice('reply')?.querySelector('p').textContent===F.sendoff,'Candidate stop speaks and shows send-off');
  results.push('candidate stop speaks send-off');
  return results;
})()
`;

const root = resolve('frontend');
const server = createServer(async (req,res) => {
  const url = new URL(req.url,'http://localhost');
  const path = resolve(root, '.'+(url.pathname==='/'?'/index.html':url.pathname));
  if(!path.startsWith(root+'/')) {res.writeHead(404);res.end();return;}
  try {
    if(url.pathname.startsWith('/vendor/')) {res.setHeader('Content-Type','text/javascript');res.end('');return;}
    res.setHeader('Content-Type',path.endsWith('.js')?'text/javascript':path.endsWith('.css')?'text/css':'text/html');
    res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; media-src 'self' blob:; connect-src 'self';");
    res.end(await readFile(path));
  } catch {res.writeHead(404);res.end();}
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const profile = await mkdtemp(join(tmpdir(),'interview-browser-'));
const chrome = spawn(process.env.CHROMIUM_PATH || 'chromium',[
  '--headless','--no-sandbox','--disable-gpu','--remote-debugging-port=0',
  '--no-first-run','--no-default-browser-check','--user-data-dir='+profile,'about:blank',
]);
let socket;
try {
  const endpoint = await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Chromium did not start')),10000);
    chrome.on('error',reject);
    chrome.stderr.on('data',chunk=>{const match=String(chunk).match(/DevTools listening on (ws:\/\/[^\s]+)/);if(match){clearTimeout(timer);resolve(match[1]);}});
  });
  socket = new WebSocket(endpoint);
  await new Promise((r,j)=>{socket.addEventListener('open',r,{once:true});socket.addEventListener('error',j,{once:true});});
  let serial=0;
  const pending=new Map();
  socket.addEventListener('message',event=>{
    const value=JSON.parse(String(event.data));
    if(value.id){const request=pending.get(value.id);if(request){clearTimeout(request.timer);request.resolve(value);pending.delete(value.id);}}
    if(value.method==='Runtime.consoleAPICalled'){
      const message=value.params.args.map(arg=>arg.value||'').join(' ');
      if(message.startsWith('PASS ')) console.log(message);
    }
  });
  socket.addEventListener('close',()=>{for(const request of pending.values()){clearTimeout(request.timer);request.reject(new Error('Browser connection closed'));}pending.clear();});
  const send=async(method,params={},sessionId,timeoutMs=15000)=>{
    const id=++serial;
    const reply=new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{pending.delete(id);reject(new Error('Timed out: '+method));},timeoutMs);
      pending.set(id,{resolve,reject,timer});
    });
    socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));
    const result=await reply;
    if(result.error) throw new Error(JSON.stringify(result.error));
    return result.result;
  };
  const {targetInfos}=await send('Target.getTargets');
  const target=targetInfos.find(info=>info.type==='page');
  const {sessionId}=await send('Target.attachToTarget',{targetId:target.targetId,flatten:true});
  await send('Page.enable',{},sessionId);
  await send('Runtime.enable',{},sessionId);
  await send('Page.addScriptToEvaluateOnNewDocument',{source:fixture},sessionId);
  await send('Page.navigate',{url:'http://127.0.0.1:'+server.address().port},sessionId);
  let ready=false;
  for(let i=0;i<100&&!ready;i++) {
    await new Promise(r=>setTimeout(r,30));
    const result=await send('Runtime.evaluate',{expression:'typeof beginOrResume === "function"'},sessionId);
    ready=result.result.value;
  }
  assert(ready,'Candidate script loaded');
  const result=await send('Runtime.evaluate',{expression:scenarios,awaitPromise:true,returnByValue:true},sessionId,60000);
  if(result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  await send('Emulation.setDeviceMetricsOverride',{width:1366,height:900,deviceScaleFactor:1,mobile:false},sessionId);
  const desktop=await send('Runtime.evaluate',{expression:`(()=>{const stage=document.querySelector('.video-stage'),r=stage.getBoundingClientRect(),overlay=addNotification('reply','Preview reply.'),long=addNotification('transcript','Long answer. '.repeat(400));const scrolls=long.scrollHeight>long.clientHeight,blur=getComputedStyle(overlay).backdropFilter.includes('blur');removeNotification(long);removeNotification(overlay);return {width:Math.round(r.width),height:Math.round(r.height),blur,scrolls};})()`,returnByValue:true},sessionId);
  assert.deepEqual(desktop.result.value,{width:1040,height:680,blur:true,scrolls:true},'Desktop camera size and overlay layout');
  console.log('PASS expanded desktop camera and frosted scrolling overlay');
  await send('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]},sessionId);
  const reduced=await send('Runtime.evaluate',{expression:'(()=>{const notice=addNotification("reply","Preview reply.");const duration=getComputedStyle(notice).transitionDuration;removeNotification(notice);return duration;})()',returnByValue:true},sessionId);
  assert.equal(reduced.result.value,'0s','Overlay animation respects reduced motion');
  await send('Emulation.setEmulatedMedia',{features:[]},sessionId);
  console.log('PASS reduced-motion overlay behavior');
  // Reload with a real, unsent editor draft scoped to the resumed question.
  const draftBeforeReload=await send('Runtime.evaluate',{expression:`(async()=>{fixture.state={...fixture.state,session_id:'s7',interview_started:true,execution_phase:'waiting_for_answer',question_number:7,current_question:{question:'Resumed after refresh.'}};await synchronize();ui.attachmentOpen.click();ui.attachmentText.value='const x = 1;\\n    const y = 2;';ui.attachmentText.dispatchEvent(new Event('input'));return editingAttachment && sessionStorage.getItem(attachmentStorageKey)?.includes('s7');})()`,awaitPromise:true,returnByValue:true},sessionId);
  assert.equal(draftBeforeReload.result.value,true,'Unsent attachment draft stored for current question');
  const snapshot=await send('Runtime.evaluate',{expression:'JSON.stringify(fixture.state)',returnByValue:true},sessionId);
  const active=JSON.parse(snapshot.result.value);
  await send('Page.addScriptToEvaluateOnNewDocument',{source:'fixture.state='+JSON.stringify(active)},sessionId);
  await send('Page.reload',{},sessionId);
  await new Promise(r=>setTimeout(r,250));
  const refreshed=await send('Runtime.evaluate',{expression:`(async()=>{for(let i=0;i<100&&!session;i++)await new Promise(r=>setTimeout(r,10));return !ui.resume.hidden && !fixture.calls.devices && $('questionText').textContent==='Resumed after refresh.' && editingAttachment && ui.attachmentText.value==='const x = 1;\\n    const y = 2;' && ui.record.disabled;})()`,awaitPromise:true,returnByValue:true},sessionId);
  assert.equal(refreshed.result.value,true,'Refresh restores question without requesting camera');
  console.log('PASS actual page refresh restores unsent draft and Resume button');
  const savedBeforeReload=await send('Runtime.evaluate',{expression:`(()=>{ui.attachmentSave.click();return attachedText === 'const x = 1;\\n    const y = 2;' && !editingAttachment;})()`,returnByValue:true},sessionId);
  assert.equal(savedBeforeReload.result.value,true,'Draft attaches after refresh');
  await send('Page.reload',{},sessionId);
  await new Promise(r=>setTimeout(r,250));
  const savedAfterReload=await send('Runtime.evaluate',{expression:`(async()=>{for(let i=0;i<100&&!session;i++)await new Promise(r=>setTimeout(r,10));return attachedText === 'const x = 1;\\n    const y = 2;' && !editingAttachment && !$('attachedPreview').hidden;})()`,awaitPromise:true,returnByValue:true},sessionId);
  assert.equal(savedAfterReload.result.value,true,'Saved attachment survives refresh');
  console.log('PASS saved attachment survives page refresh');
  const questionChanged=await send('Runtime.evaluate',{expression:`(async()=>{fixture.state.question_number=8;fixture.state.current_question={question:'A different question.'};await synchronize();return !attachedText && !sessionStorage.getItem(attachmentStorageKey) && $('questionText').textContent==='A different question.';})()`,awaitPromise:true,returnByValue:true},sessionId);
  assert.equal(questionChanged.result.value,true,'Question change clears attachment');
  console.log('PASS question change clears attachment');
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true},sessionId);
  const layout=await send('Runtime.evaluate',{expression:`(()=>{ui.attachmentOpen.click();ui.attachmentText.value='    '+ 'longVariableName'.repeat(30);ui.attachmentText.dispatchEvent(new Event('input'));const fits=document.documentElement.scrollWidth<=window.innerWidth;const nearby=document.querySelector('.question-card').contains($('questionText'))&&document.querySelector('.question-card').contains($('attachmentEditor'));return {fits,nearby,editorVisible:!$('attachmentEditor').hidden};})()`,returnByValue:true},sessionId);
  assert.deepEqual(layout.result.value,{fits:true,nearby:true,editorVisible:true},'Mobile editor stays with question without horizontal overflow');
  console.log('PASS mobile attachment editor fits beside the question');
  const mobileNotices=await send('Runtime.evaluate',{expression:`(()=>{const stack=$('candidateNotifications'),stage=document.querySelector('.video-stage');const first=addNotification('transcript','Long spoken answer. '.repeat(100));const second=addNotification('reply','Long interviewer response. '.repeat(100));const a=stack.getBoundingClientRect(),b=stage.getBoundingClientRect();const result={ordered:stack.firstElementChild===first&&stack.lastElementChild===second,bounded:a.top>=b.top&&a.bottom<=b.bottom,scrolls:stack.scrollHeight>stack.clientHeight,documentFits:document.documentElement.scrollWidth<=window.innerWidth};removeNotification(first);removeNotification(second);return result;})()`,returnByValue:true},sessionId);
  assert.deepEqual(mobileNotices.result.value,{ordered:true,bounded:true,scrolls:true,documentFits:true},'Mobile notification stack stays within camera panel');
  console.log('PASS mobile camera notifications stack and scroll within the preview');
  await send('Page.navigate',{url:'http://127.0.0.1:'+server.address().port+'/observer.html'},sessionId);
  let observerReady=false;
  for(let i=0;i<100&&!observerReady;i++){
    await new Promise(r=>setTimeout(r,30));
    const ready=await send('Runtime.evaluate',{expression:'typeof update === "function"'},sessionId);
    observerReady=ready.result.value;
  }
  assert(observerReady,'Observer script loaded');
  const observer=await send('Runtime.evaluate',{expression:`(()=>{const quick={session:{mode:'quick',mode_label:'Quick',interview_started:true,main_question_budget:4,max_followups_total:2,max_followups_per_skill:1,adaptive_turn_budget:6},planner_state:{main_questions_used:4,followups_used:1,evaluated_turn_count:5,execution_phase:'waiting_for_answer',current_question:{is_followup:true,followup_depth:1}}};update(quick);const quickRemaining=$('answersRemaining').textContent;const standard={session:{mode:'standard',mode_label:'Standard',interview_started:true,main_question_budget:8,max_followups_total:4,max_followups_per_skill:1,adaptive_turn_budget:12},planner_state:{main_questions_used:2,followups_used:1,evaluated_turn_count:3,execution_phase:'waiting_for_answer',current_question:{is_followup:false}}};update(standard);const standardRemaining=$('answersRemaining').textContent;const standardDetail=$('remainingDetail').textContent;const thorough={session:{mode:'thorough',mode_label:'Thorough',interview_started:true,main_question_budget:12,max_followups_total:6,max_followups_per_skill:2,adaptive_turn_budget:18},planner_state:{main_questions_used:12,followups_used:5,evaluated_turn_count:17,execution_phase:'waiting_for_answer',current_question:{is_followup:true,followup_depth:2}}};update(thorough);return {quickRemaining,standardRemaining,standardDetail,thoroughRemaining:$('answersRemaining').textContent,followups:$('followups').textContent};})()`,returnByValue:true},sessionId);
  assert.deepEqual(observer.result.value,{quickRemaining:'Up to 1',standardRemaining:'Up to 9',standardDetail:'6 main remaining · up to 3 follow-ups possible',thoroughRemaining:'Up to 1',followups:'5'});
  console.log('PASS observer separate main and follow-up progress in every bounded mode');
  const observerAttachment=await send('Runtime.evaluate',{expression:`(()=>{const answer='I explained the approach.\\n\\nAttached text/code:\\nfunction sum() {\\n    return 2;\\n}';update({session:{interview_started:true},planner_state:{execution_phase:'waiting_for_answer'},entries:[{question:'Show code.',answer,is_intro:false,score:8}]});const line=document.querySelector('.conversation-entry section:nth-of-type(2) p');return {text:line?.textContent,whiteSpace:getComputedStyle(line).whiteSpace};})()`,returnByValue:true},sessionId);
  assert.deepEqual(observerAttachment.result.value,{text:'I explained the approach.\n\nAttached text/code:\nfunction sum() {\n    return 2;\n}',whiteSpace:'pre-wrap'},'Observer transcript preserves attached code');
  console.log('PASS observer transcript displays attached code with line breaks');
} finally {
  socket?.close();
  chrome.kill('SIGTERM');
  await new Promise(r=>{
    if(chrome.exitCode!==null || chrome.signalCode!==null)return r();
    const timer=setTimeout(()=>chrome.kill('SIGKILL'),5000);
    chrome.once('exit',()=>{clearTimeout(timer);r();});
  });
  server.closeAllConnections();
  await new Promise(r=>server.close(r));
  // Chromium helpers can briefly recreate files after the parent exits.
  await rm(profile,{recursive:true,force:true,maxRetries:10,retryDelay:100});
}
