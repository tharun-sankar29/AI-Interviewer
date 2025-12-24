class AudioRecorder {
  constructor() {
    this.mediaRecorder = null;
    this.audioChunks = [];
    this.isRecording = false;
    this.onStartRecording = null;
    this.onStopRecording = null;
    this.onRecordingFailed = null;
    this.stream = null;
  }

  async startRecording() {
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });

      // Use default MediaRecorder settings which will use the browser's default codec (usually Opus in WebM)
      this.audioChunks = [];
      
      // Set up MediaRecorder with default settings
      this.mediaRecorder = new MediaRecorder(this.stream, {
        audioBitsPerSecond: 128000
      });

      this.audioChunks = [];

      this.mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          this.audioChunks.push(event.data);
        }
      };

      this.mediaRecorder.onstop = async () => {
        // Create audio blob with WebM/Opus format
        const audioBlob = new Blob(this.audioChunks, { type: 'audio/webm;codecs=opus' });
        const formData = new FormData();
        formData.append('audio', audioBlob, 'recording.opus');

        // Stop the stream tracks
        if (this.stream) {
          this.stream.getTracks().forEach((track) => track.stop());
          this.stream = null;
        }

        this.isRecording = false;
        if (this.onStopRecording) this.onStopRecording(formData);
      };

      this.mediaRecorder.start();
      this.isRecording = true;
      if (this.onStartRecording) this.onStartRecording();
    } catch (error) {
      console.error('Failed to start recording:', error);
      if (this.onRecordingFailed) this.onRecordingFailed(error);
    }
  }

  stopRecording() {
    if (this.mediaRecorder && this.isRecording) {
      this.mediaRecorder.stop();
      this.isRecording = false;
    }
  }
}
