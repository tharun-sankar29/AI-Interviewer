# 🤖 AI Interviewer

An AI-powered mock interview platform that conducts realistic, adaptive interviews using your resume. It asks relevant questions, evaluates your answers in real time, and generates a detailed performance report when the session ends.

---

## ✨ Features

- **Resume-aware questions** — Builds a RAG (Retrieval-Augmented Generation) index from your resume and knowledge files to generate targeted, relevant interview questions.
- **Adaptive follow-ups** — Detects short or low-scoring answers and automatically asks follow-up questions to probe deeper.
- **Real-time answer evaluation** — Scores each response (0–10) and provides instant feedback using Gemini 2.5 Flash.
- **Voice input** — Speak your answers via the browser microphone; Google Speech-to-Text transcribes them automatically.
- **Text-to-speech** — Interview questions are read aloud using Google Cloud Text-to-Speech.
- **Post-interview analysis** — Generates a comprehensive `analysis.txt` report covering performance summary, technical proficiency, communication skills, and per-question breakdowns.
- **Multiple interview modes** — Choose between `quick` (2 questions), `standard` (5 questions), or `thorough` (3 questions with more follow-ups).
- **Session logging** — Every interview is saved as a timestamped JSON file in `interview_logs/`.

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python, Flask |
| LLM | Google Gemini 2.5 Flash (via `llama-index-llms-google-genai`) |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2` (HuggingFace) |
| RAG Framework | LlamaIndex |
| Speech-to-Text | Google Cloud Speech-to-Text |
| Text-to-Speech | Google Cloud Text-to-Speech |
| Frontend | Vanilla HTML, CSS, JavaScript |

---

## 📁 Project Structure

```
AI-Interviewer/
├── app.py                      # Flask app — API routes & server entry point
├── backend/
│   ├── interview_session.py    # Interview state machine, Q&A flow, logging
│   └── analysis_module.py      # RAG index, LLM setup, question generation, scoring
├── frontend/
│   ├── index.html              # Chat UI
│   ├── script.js               # Chat logic, API calls
│   ├── audioRecorder.js        # Browser microphone recording
│   ├── style.css               # Main styles
│   └── audio-styles.css        # Voice button styles
├── knowledge_files/            # Drop your resume + any context files here
│   └── resume.md               # Resume used to generate questions
├── interview_logs/             # Auto-generated interview transcripts & analysis
│   ├── interview_<timestamp>.json
│   ├── log.txt
│   └── analysis.txt
├── .env                        # API keys (see setup below)
└── test_interview.py           # Manual test script
```

---

## 🚀 Getting Started

### 1. Prerequisites

- Python 3.10+
- A Google Cloud project with **Speech-to-Text** and **Text-to-Speech** APIs enabled
- A Google Gemini API key

### 2. Clone the repository

```bash
git clone https://github.com/your-username/AI-Interviewer.git
cd AI-Interviewer
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

> Core packages: `flask`, `python-dotenv`, `google-cloud-speech`, `google-cloud-texttospeech`, `llama-index`, `llama-index-llms-google-genai`, `llama-index-embeddings-huggingface`, `sentence-transformers`

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
GEMINI_API_KEY=your_gemini_api_key_here
GOOGLE_APPLICATION_CREDENTIALS=/path/to/your/google-service-account.json
```

### 5. Add your resume

Place your resume (Markdown, TXT, or PDF) inside the `knowledge_files/` directory. This is the context the AI uses to generate interview questions and evaluate your answers.

```bash
cp your_resume.md knowledge_files/resume.md
```

You can also add any other relevant context documents (e.g., a job description, technical concepts) to the same folder.

### 6. Run the server

```bash
python app.py
```

The app will start on `http://localhost:5000`. Open it in your browser.

---

## 🎮 How to Use

1. **Open** `http://localhost:5000` in your browser.
2. The interview **starts automatically** — the AI greets you and asks the first question.
3. **Type** your answer in the input box and click **Send**, or click **🎤 Speak** to answer by voice.
4. The AI evaluates your answer, gives brief feedback, and asks the next question.
5. After all questions are answered, the interview closes and a detailed **analysis report** is generated in `interview_logs/analysis.txt`.

---

## 📊 Interview Modes

| Mode | Main Questions | Follow-ups per Question |
|---|---|---|
| `quick` | 2 | 1 |
| `standard` | 5 | 1 |
| `thorough` | 3 | 2 |

The default mode is `quick`. To change it, modify the `mode` parameter in the `start_interview` call in `app.py`, or pass it via the `/api/start` endpoint.

---

## 🔌 API Reference

| Endpoint | Method | Description |
|---|---|---|
| `POST /api/start` | POST | Start a new interview session |
| `POST /api/answer` | POST | Submit a text answer |
| `POST /api/interview` | POST | Submit answer and get next question |
| `POST /api/tts` | POST | Convert text to speech (MP3) |
| `POST /api/transcribe` | POST | Transcribe an audio file to text |
| `GET /api/log` | GET | Retrieve the full interview log |

### Example: Start an interview

```bash
curl -X POST http://localhost:5000/api/start \
  -H "Content-Type: application/json" \
  -d '{"context": "Backend Engineering Role"}'
```

### Example: Submit an answer

```bash
curl -X POST http://localhost:5000/api/interview \
  -H "Content-Type: application/json" \
  -d '{"content": "I have 3 years of experience with REST APIs and database optimization."}'
```

---

## 📄 Output Files

After each session, three files are written to `interview_logs/`:

- **`interview_<timestamp>.json`** — Full structured log of every question, answer, score, and evaluation.
- **`log.txt`** — Human-readable transcript appended after each exchange.
- **`analysis.txt`** — Comprehensive post-interview report including overall score, score distribution, LLM-generated analysis, and a question-by-question breakdown.

---

## ⚙️ Configuration Notes

- The RAG index is built at startup from all files in `knowledge_files/`. Adding more documents (e.g., a job description) improves question relevance and evaluation accuracy.
- Questions are generated fresh for each session. If the RAG index fails to load, built-in fallback questions are used automatically.
- Voice recording uses the browser's `MediaRecorder` API and sends WebM/Opus audio to the server. Google STT must be configured for this to work.

---

## 🔒 Security Note

Never commit your `.env` file or Google service account JSON to version control. Both are listed in `.gitignore` by default.

---

## 📝 License

MIT License. Feel free to use, modify, and distribute.
