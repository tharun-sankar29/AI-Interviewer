class AudioRecorder {
  constructor() {
    this.vad = null;
    this.isRecording = false;
    this.initialSpeechTimer = null;
    this.maxAnswerTimer = null;
    this.onStartRecording = null;
    this.onSpeechStart = null;
    this.onStopRecording = null;
    this.onRecordingFailed = null;
    this.capturedFrames = [];
    this.fallbackSpeechDetected = false;
    this.fallbackVoiceFrames = 0;
    this.lastFallbackVoiceAt = 0;
    this.fallbackFinishPending = false;
    this.speechStartNotified = false;
    this.generation = 0;
    this.initializing = null;
    this.micStream = null;
  }

  static wavBlob(samples, sampleRate = 16000) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const write = (offset, value) => [...value].forEach((char, i) => view.setUint8(offset + i, char.charCodeAt(0)));
    write(0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    write(8, 'WAVE'); write(12, 'fmt ');
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    write(36, 'data'); view.setUint32(40, samples.length * 2, true);
    samples.forEach((sample, i) => {
      const clamped = Math.max(-1, Math.min(1, sample));
      view.setInt16(44 + i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    });
    return new Blob([view], { type: 'audio/wav' });
  }

  async ensureVad() {
    if (this.vad) return;
    if (this.initializing) return this.initializing;
    if (!window.vad?.MicVAD) throw new Error('Local voice detector assets are unavailable. Please refresh and try again.');
    const generation = this.generation;
    const pending = window.vad.MicVAD.new({
      getStream: async () => {
        const stream = await navigator.mediaDevices.getUserMedia({audio: true});
        if (generation !== this.generation) {
          stream.getTracks().forEach(track => track.stop());
          throw new Error('Recording cancelled.');
        }
        this.micStream = stream;
        stream.getTracks().forEach(track => {
          track.onended = () => {
            if (generation === this.generation) this.onRecordingFailed?.(new Error('Microphone disconnected. Reconnect it, then retry.'), true);
          };
        });
        return stream;
      },
      pauseStream: async () => {},
      resumeStream: async stream => stream,
      model: 'v5', baseAssetPath: '/vendor/', onnxWASMBasePath: '/vendor/',
      startOnLoad: false,
      positiveSpeechThreshold: 0.3, negativeSpeechThreshold: 0.25,
      redemptionMs: 1400, preSpeechPadMs: 800, minSpeechMs: 400,
      submitUserSpeechOnPause: true,
      onFrameProcessed: (probabilities, frame) => {
        if (generation === this.generation) this.captureFrame(probabilities, frame);
      },
      onSpeechStart: () => {
        if (generation === this.generation) this.markSpeechStarted();
      },
      onSpeechEnd: audio => {
        if (generation === this.generation) {
          return this.finish(audio).catch(error => {
            if (generation === this.generation) this.onRecordingFailed?.(error, true);
          });
        }
      },
      onVADMisfire: () => {},
    });
    this.initializing = pending;
    try {
      const detector = await pending;
      if (generation !== this.generation) {
        try { await detector.destroy(); } catch { /* VAD may not have started yet. */ }
        return;
      }
      this.vad = detector;
    } finally {
      if (this.initializing === pending) this.initializing = null;
    }
  }

  resetCapture() {
    this.capturedFrames = [];
    this.fallbackSpeechDetected = false;
    this.fallbackVoiceFrames = 0;
    this.lastFallbackVoiceAt = 0;
    this.fallbackFinishPending = false;
    this.speechStartNotified = false;
  }

  markSpeechStarted() {
    clearTimeout(this.initialSpeechTimer);
    if (!this.speechStartNotified) {
      this.speechStartNotified = true;
      this.onSpeechStart?.();
    }
  }

  captureFrame(probabilities, frame) {
    if (!this.isRecording || !frame?.length) return;
    this.capturedFrames.push(frame.slice());

    let energy = 0;
    for (const sample of frame) energy += sample * sample;
    const rms = Math.sqrt(energy / frame.length);
    const speechProbability = Number(probabilities?.isSpeech || 0);
    const soundsLikeVoice = speechProbability >= 0.15 || rms >= 0.008;

    if (soundsLikeVoice) {
      this.fallbackVoiceFrames += 1;
      this.lastFallbackVoiceAt = performance.now();
      if (this.fallbackVoiceFrames >= 4) {
        this.fallbackSpeechDetected = true;
        this.markSpeechStarted();
      }
      return;
    }

    this.fallbackVoiceFrames = 0;
    if (
      this.fallbackSpeechDetected
      && !this.fallbackFinishPending
      && performance.now() - this.lastFallbackVoiceAt >= 1400
    ) {
      this.fallbackFinishPending = true;
      const generation = this.generation;
      void this.finish(this.capturedAudio()).catch(error => {
        if (generation === this.generation) this.onRecordingFailed?.(error, true);
      });
    }
  }

  capturedAudio() {
    const sampleCount = this.capturedFrames.reduce((total, frame) => total + frame.length, 0);
    const audio = new Float32Array(sampleCount);
    let offset = 0;
    for (const frame of this.capturedFrames) {
      audio.set(frame, offset);
      offset += frame.length;
    }
    return audio;
  }

  async startRecording() {
    const generation = this.generation;
    try {
      await this.ensureVad();
      if (generation !== this.generation || !this.vad) return;
      this.resetCapture();
      this.isRecording = true;
      await this.vad.start();
      if (!this.isRecording || generation !== this.generation) return;
      this.initialSpeechTimer = setTimeout(() => this.cancel('No speech detected within 10 seconds.'), 10000);
      this.maxAnswerTimer = setTimeout(() => this.cancel('The 120-second answer limit was reached.'), 120000);
      this.onStartRecording?.();
    } catch (error) {
      if (generation !== this.generation) return;
      clearTimeout(this.initialSpeechTimer); clearTimeout(this.maxAnswerTimer);
      this.isRecording = false;
      this.onRecordingFailed?.(error, true);
      throw error;
    }
  }

  async finish(audio) {
    if (!this.isRecording) return;
    const generation = this.generation;
    clearTimeout(this.initialSpeechTimer); clearTimeout(this.maxAnswerTimer);
    this.isRecording = false;
    await this.vad.pause();
    if (generation !== this.generation) return;
    if (!audio || audio.length < 6400) {
      this.onRecordingFailed?.(new Error('No usable speech was detected. Try speaking closer to the microphone.'), false);
      return;
    }
    const formData = new FormData();
    formData.append('audio', AudioRecorder.wavBlob(audio), 'answer.wav');
    this.onStopRecording?.(formData);
  }

  async stopRecording() {
    if (!this.vad || !this.isRecording) return;
    const generation = this.generation;
    await this.vad.pause();
    if (generation !== this.generation) return;
    if (this.isRecording) {
      clearTimeout(this.initialSpeechTimer); clearTimeout(this.maxAnswerTimer);
      this.isRecording = false;
      const audio = this.capturedAudio();
      // A manual stop is an explicit request to submit. Do not discard a valid
      // recording merely because the local VAD did not recognize a quiet voice;
      // Whisper is the final authority on whether the audio contains speech.
      if (audio.length >= 6400) {
        const formData = new FormData();
        formData.append('audio', AudioRecorder.wavBlob(audio), 'answer.wav');
        this.onStopRecording?.(formData);
      } else {
        this.onRecordingFailed?.(new Error('No usable speech was detected. Check the selected microphone and speak closer to it.'), false);
      }
    }
  }

  async cancel(message = 'Recording cancelled.') {
    clearTimeout(this.initialSpeechTimer); clearTimeout(this.maxAnswerTimer);
    this.isRecording = false;
    if (this.vad) await this.vad.pause();
    this.onRecordingFailed?.(new Error(message), false);
  }

  async pauseForPlayback() {
    if (this.vad) await this.vad.pause();
  }

  async destroy() {
    this.generation += 1;
    this.isRecording = false;
    clearTimeout(this.initialSpeechTimer);
    clearTimeout(this.maxAnswerTimer);
    const detector = this.vad;
    this.vad = null;
    this.initializing = null;
    this.micStream?.getTracks().forEach(track => track.stop());
    this.micStream = null;
    this.resetCapture();
    try {
      if (detector) await detector.destroy();
    } catch {
      // MicVAD.destroy requires an initialized processor. The stream is also
      // stopped directly above, including when initialization is interrupted.
    }
  }
}
