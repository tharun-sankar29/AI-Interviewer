from flask import Flask, request, jsonify, send_from_directory, make_response
from dotenv import load_dotenv
from google.cloud import speech, texttospeech
from google.cloud.speech import RecognitionConfig, RecognitionAudio
from backend.interview_session import start_interview, submit_answer, get_full_log, INTERVIEW_STATE
from backend.analysis_module import build_rag_index

import logging

# Configure root logger to only show warnings and above
logging.basicConfig(level=logging.WARNING)

# Disable verbose logging for specific libraries
logging.getLogger('google').setLevel(logging.WARNING)
logging.getLogger('google.cloud').setLevel(logging.WARNING)
logging.getLogger('google.api_core').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

try:
    STT_CLIENT = speech.SpeechClient()
except Exception as e:
    print(f"Failed to initialize Google SpeechClient: {e}")
    STT_CLIENT = None # Handle client initialization error

load_dotenv()


app = Flask(__name__, static_folder="frontend")

# Serve frontend
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(app.static_folder, path)

# API routes
@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json
    context = data.get("context", "General AI Interview")
    seed_questions = data.get("seed_questions")
    result = start_interview(context, seed_questions)
    return jsonify(result)

@app.route("/api/answer", methods=["POST"])
def api_answer():
    data = request.json
    candidate_answer = data.get("answer")
    if not candidate_answer:
        return jsonify({"error": "No answer provided."})
    result = submit_answer(candidate_answer)
    return jsonify(result)

@app.route("/api/log", methods=["GET"])
def api_log():
    return jsonify(get_full_log())


@app.route("/api/tts", methods=["POST"])
def tts():
    try:
        data = request.get_json()
        if not data or 'text' not in data:
            return jsonify({"error": "No text provided"}), 400
            
        text = data['text']
        if not text:
            return jsonify({"error": "Empty text provided"}), 400

        client = texttospeech.TextToSpeechClient()
        synthesis_input = texttospeech.SynthesisInput(text=text)

        voice = texttospeech.VoiceSelectionParams(
            language_code="en-US",
            name="en-US-Standard-B",
            ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL
        )

        # Slightly increase the speaking rate (1.0 is normal, 1.1 is 10% faster)
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=1.1,  # 10% faster than normal
            pitch=0.0,         # Keep normal pitch
            volume_gain_db=0.0 # Keep normal volume
        )

        tts_response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config
        )

        # Return the audio content directly as a response
        response = make_response(tts_response.audio_content)
        response.headers['Content-Type'] = 'audio/mp3'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response
        
    except Exception as e:
        print(f"Error in TTS: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    # Safety check for client initialization
    if not STT_CLIENT:
        return jsonify({"error": "Speech-to-Text service is unavailable."}), 503
        
    try:
        if "audio" not in request.files:
            return jsonify({"error": "No audio file provided"}), 400
            
        audio_file = request.files["audio"]
        
        # 1. Read the audio content directly into memory
        audio_content = audio_file.read()
        
        # 2. Create the RecognitionAudio object from the byte content
        audio = RecognitionAudio(content=audio_content)

        # 3. Configure the speech recognition settings for WebM/Opus
        config = RecognitionConfig(
            encoding=RecognitionConfig.AudioEncoding.WEBM_OPUS,
            sample_rate_hertz=48000,
            language_code="en-US",
            enable_automatic_punctuation=True,
            model="latest_short"
        )
        
        # 4. Detect speech (Synchronous API call for audio < ~60s)
        response = STT_CLIENT.recognize(config=config, audio=audio)
        
        # 5. Extract the transcript
        if response.results:
            transcript_text = response.results[0].alternatives[0].transcript
        else:
            transcript_text = ""
            
        return jsonify({
            "transcript": transcript_text,
            "status": "success"
        })
            
    except Exception as e:
        print(f"Google STT API error: {str(e)}")
        # Note: No file cleanup is necessary since we didn't write to disk
        return jsonify({
            "error": "Failed to transcribe audio. Please check server logs."
        }), 500
    

@app.route("/api/interview", methods=["POST"])
def api_interview():
    try:
        data = request.json
        answer = data.get("content")
        if not answer:
            return jsonify({"error": "No answer provided."}), 400
        
        # Get only the essential response
        result = submit_answer(answer)
        
        if not isinstance(result, dict) or "reaction" not in result:
            print(f"Unexpected result format: {result}")
            return jsonify({"error": "Invalid response format"}), 500
        
        # Prepare the response data with all possible fields
        response_data = {
            "message": result.get("reaction", "").strip(),  # Main response message
            "response": result.get("reaction", "").strip(),  # For backward compatibility
            "next_question": result.get("next_question", "")  # Next question if any
        }
        
        # Add analysis data if available
        if "analysis_score" in result:
            response_data["analysis_score"] = result["analysis_score"]
        
        # Add evaluation to the response if it exists
        if "evaluation" in result and result["evaluation"]:
            response_data["evaluation"] = result["evaluation"]
        
        return jsonify(response_data)
        
    except Exception as e:
        print(f"Error in api_interview: {str(e)}")
        return jsonify({"error": "An error occurred while processing your answer"}), 500



@app.after_request
def add_csp(response):
    response.headers['Content-Security-Policy'] = "script-src 'self' 'unsafe-eval';"
    return response

def initialize_rag_index():
    """Initialize the RAG index when the application starts"""
    try:
        print("\n🔍 Initializing RAG index on startup...")
        rag_index = build_rag_index()
        if rag_index is not None:
            INTERVIEW_STATE["rag_index"] = rag_index
            print("✅ RAG index initialized successfully")
        else:
            print("⚠️ Failed to initialize RAG index. Some features may be limited.")
    except Exception as e:
        print(f"❌ Error initializing RAG index: {e}")

# Initialize RAG index when the module is imported
initialize_rag_index()

if __name__ == "__main__":
    app.run(port=5000, debug=True, use_reloader=False)
