// DOM Elements
const chatContainer = document.getElementById('chat');
const textInput = document.getElementById('textInput');
const sendBtn = document.getElementById('sendBtn');
const voiceBtn = document.getElementById('voiceBtn');

// State
let isProcessing = false;
let isListening = false;

// ===== Utility Functions =====

// Add message to chat
function addMessage(text, sender, isError = false) {
  const msg = document.createElement('div');
  msg.className = `message ${sender}${isError ? ' error' : ''}`;
  msg.innerText = text;
  chatContainer.appendChild(msg);
  chatContainer.scrollTop = chatContainer.scrollHeight;
  return msg;
}

// Set loading state
function setLoading(loading) {
  isProcessing = loading;
  [sendBtn, textInput, voiceBtn].forEach(el => el.disabled = loading);

  if (!('webkitSpeechRecognition' in window))
    voiceBtn.disabled = true;

  document.body.classList.toggle('processing', loading);
}

// ===== Audio State =====
const audioState = {
  currentAudio: null,
  audioContext: null,
  isAudioEnabled: false,
  pendingAudio: null,
  isPageVisible: true,
};

// Pause audio if tab hidden
document.addEventListener('visibilitychange', () => {
  audioState.isPageVisible = !document.hidden;
  if (!audioState.isPageVisible && audioState.currentAudio) {
    audioState.currentAudio.pause();
  }
});

// Enable audio on user interaction
function enableAudio() {
  if (!audioState.isAudioEnabled) {
    audioState.isAudioEnabled = true;
    document.body.classList.add('audio-enabled');
    console.log('Audio enabled by user interaction');

    if (audioState.pendingAudio) {
      const { text, resolve } = audioState.pendingAudio;
      audioState.pendingAudio = null;
      playTTS(text).then(resolve);
    }
  }

  ['click', 'keydown', 'touchstart'].forEach(e => document.removeEventListener(e, enableAudio));
}

// Initialize audio on page load
function initAudio() {
  try {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (AudioContext) {
      audioState.audioContext = new AudioContext();
      const resumeAudio = () => {
        if (audioState.audioContext.state === 'suspended')
          audioState.audioContext.resume();
      };
      ['click', 'keydown', 'touchstart'].forEach(e =>
        document.addEventListener(e, resumeAudio, { once: true })
      );
    }

    ['click', 'keydown', 'touchstart'].forEach(e =>
      document.addEventListener(e, enableAudio, { once: true })
    );

    // Prompt to enable audio
    const audioPrompt = document.createElement('div');
    audioPrompt.className = 'audio-prompt';
    audioPrompt.textContent = 'Click anywhere to enable audio';
    document.body.appendChild(audioPrompt);

    const removePrompt = () => {
      audioPrompt.remove();
      document.removeEventListener('click', removePrompt);
      document.removeEventListener('keydown', removePrompt);
    };

    document.addEventListener('click', removePrompt);
    document.addEventListener('keydown', removePrompt);
  } catch (err) {
    console.error('Audio initialization error:', err);
  }
}

document.readyState === 'loading'
  ? document.addEventListener('DOMContentLoaded', initAudio)
  : initAudio();

// ===== TTS (Text-to-Speech) =====
async function playTTS(text) {
  if (!text) return;

  if (!audioState.isAudioEnabled)
    return new Promise(resolve => (audioState.pendingAudio = { text, resolve }));

  if (!audioState.isPageVisible) return;

  if (audioState.currentAudio) {
    audioState.currentAudio.pause();
    audioState.currentAudio.remove();
    audioState.currentAudio = null;
  }

  try {
    const res = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });

    if (!res.ok) throw new Error(`TTS failed: ${res.status}`);
    const blob = await res.blob();
    if (!blob.size) throw new Error('Empty TTS response');

    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audioState.currentAudio = audio;
    audio.volume = 1.0;
    document.body.appendChild(audio);

    await audio.play();
    await new Promise((resolve, reject) => {
      audio.addEventListener('ended', resolve, { once: true });
      audio.addEventListener('error', reject, { once: true });
    });

    audio.remove();
    URL.revokeObjectURL(url);
    audioState.currentAudio = null;
  } catch (err) {
    console.error('TTS playback failed:', err);
  }
}

// ===== API Response Processing =====
async function processResponse(res) {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Server error ${res.status}: ${text}`);
  }

  const text = await res.text();
  console.log('Raw response:', text);

  try {
    const data = JSON.parse(text);
    
    // Handle interview completion
    if (data.status === 'completed') {
      textInput.disabled = true;
      sendBtn.disabled = true;
      voiceBtn.disabled = true;
      return data.response || data.message || 'Interview completed. Thank you for your time.';
    }

    // Combine all possible response fields
    const msg = [
      data.reaction,
      data.next_question,
      data.response,
      data.message
    ].filter(Boolean).join(' ').trim();

    if (data.evaluation) console.log('Evaluation:', data.evaluation);
    return msg || 'No response content';
  } catch (e) {
    console.error('Error parsing response:', e);
    const parts = text.split('\n').filter(Boolean);
    for (const part of parts) {
      try {
        const data = JSON.parse(part);
        const msg = [
          data.reaction,
          data.next_question,
          data.response,
          data.message
        ].filter(Boolean).join(' ').trim();
        if (msg) return msg;
      } catch (e) {
        console.warn('Skipping invalid JSON chunk:', part);
      }
    }
    throw new Error('Invalid server response');
  }
}

// ===== Send Text =====
async function sendText() {
  const text = textInput.value.trim();
  if (!text || isProcessing) return;

  setLoading(true);
  addMessage(text, 'user');
  textInput.value = '';

  const loadingMsg = addMessage('Processing...', 'ai');
  try {
    const res = await fetch('/api/answer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer: text }),
    });

    const reply = await processResponse(res);
    loadingMsg.textContent = reply;
    loadingMsg.className = 'message ai';
    playTTS(reply);
  } catch (err) {
    console.error('Send failed:', err);
    loadingMsg.textContent = err.message || 'Error processing message';
    loadingMsg.classList.add('error');
  } finally {
    setLoading(false);
  }
}

sendBtn.addEventListener('click', sendText);
textInput.addEventListener('keydown', e => e.key === 'Enter' && sendText());

// ===== Voice Recorder =====
const audioRecorder = new AudioRecorder();

audioRecorder.onStartRecording = () => {
  voiceBtn.classList.add('recording');
  voiceBtn.innerText = '⬛ Stop Recording';
  isRecording = true;
  addMessage('Recording... Click stop when finished.', 'system');
};

audioRecorder.onStopRecording = async formData => {
  voiceBtn.classList.remove('recording');
  voiceBtn.innerText = '🎤 Speak';
  isRecording = false;
  addMessage('Processing audio...', 'system');

  try {
    setLoading(true);
    const res = await fetch('/api/transcribe', { method: 'POST', body: formData });
    const data = await res.json();

    if (data.error) throw new Error(data.error);
    addMessage(data.transcript, 'user');

    console.log('Sending answer to server:', data.transcript);
    const ansRes = await fetch('/api/answer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer: data.transcript }),
    });

    if (!ansRes.ok) {
      const errorText = await ansRes.text();
      console.error('Server error:', errorText);
      throw new Error(`Server responded with ${ansRes.status}: ${errorText}`);
    }

    const result = await ansRes.json();
    console.log('Server response:', result);

    if (result.error) {
      console.error('Error in response:', result.error);
      throw new Error(result.error);
    }

    // Determine the messages to display (in order of preference)
    const message = result.message || result.response || result.reaction || "I'm not sure how to respond to that.";
    const nextQuestion = result.next_question;
    
    // Add the main response to chat and play it
    addMessage(message, 'ai');
    
    // Play messages sequentially with a delay
    if (nextQuestion) {
      // Play the first message, then the next question after it finishes
      playTTS(message).then(() => {
        // Add a small delay before showing and reading the next question
        setTimeout(() => {
          addMessage(nextQuestion, 'ai');
          playTTS(nextQuestion);
        }, 500); // 500ms delay between messages
      });
    } else {
      // If there's no next question, just play the message
      playTTS(message);
    }
  } catch (err) {
    addMessage(err.message || 'Voice processing failed', 'ai', true);
  } finally {
    setLoading(false);
  }
};

audioRecorder.onRecordingFailed = error => {
  console.error('Mic error:', error);
  addMessage('Microphone access failed. Check permissions.', 'system', true);
  voiceBtn.disabled = true;
  voiceBtn.innerText = 'Mic Error';
};

voiceBtn.addEventListener('click', async () => {
  if (isProcessing) return;
  if (!isListening) {
    try {
      await audioRecorder.startRecording();
      voiceBtn.classList.add('recording');
      isListening = true;
    } catch (err) {
      addMessage('Cannot start recording. Check microphone.', 'system', true);
      voiceBtn.classList.remove('recording');
    }
  } else {
    try {
      await audioRecorder.stopRecording();
    } finally {
      voiceBtn.classList.remove('recording');
      isListening = false;
    }
  }
});

// ===== Initialize Interview =====
async function initializeInterview() {
  try {
    const res = await fetch('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        context: 'General Technical Interview',
        mode: 'quick'  // This will use the quick mode which has max_questions: 2
      }),
    });

    const data = await res.json();
    if (data.error) throw new Error(data.error);

    if (data.welcome_message) {
      addMessage(data.welcome_message, 'ai');
      await playTTS(data.welcome_message);
      await new Promise(r => setTimeout(r, 1000));
    }

    if (data.next_question && data.next_question !== data.welcome_message) {
      addMessage(data.next_question, 'ai');
      await playTTS(data.next_question);
    }
  } catch (err) {
    console.error('Interview init failed:', err);
    addMessage('Failed to initialize. Please refresh.', 'ai', true);
  }
}

initializeInterview();
