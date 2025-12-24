import os
import sys
import json
import re
import logging
from dotenv import load_dotenv
from llama_index.core import (
    VectorStoreIndex,
    SimpleDirectoryReader,
    Settings,
    PromptTemplate
)
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.retrievers import VectorIndexRetriever
from llama_index.llms.google_genai import GoogleGenAI
from llama_index.embeddings.huggingface import HuggingFaceEmbedding


# =======================
#   INITIAL SETUP
# =======================
logging.getLogger().setLevel(logging.WARNING)
load_dotenv()

llm = None
embed_model = None


# =======================
#   HELPER FUNCTIONS
# =======================
def parse_gemini_response(raw_response: str):
    """Safely parse Gemini JSON output wrapped in ```json ...```."""
    try:
        cleaned = re.sub(r"^```json|```$", "", raw_response.strip(), flags=re.MULTILINE).strip()
        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        if json_match:
            cleaned = json_match.group(0)
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "reaction": "I'm sorry, could you repeat that?",
            "next_question": "Let's move to another topic — tell me about your experience working in a team.",
            "analysis_score": 0,
            "evaluation": "Invalid model output.",
        }


# =======================
#   LLM INITIALIZATION
# =======================
def get_llm():
    """Initialize and return LLM and embedding models (lazy-loaded)."""
    global llm, embed_model
    if llm is None or embed_model is None:
        try:
            llm = GoogleGenAI(
                model="gemini-2.5-flash",
                api_key=os.getenv("GEMINI_API_KEY"),
                temperature=0.7,
                top_p=0.8,
            )
            embed_model = HuggingFaceEmbedding("sentence-transformers/all-MiniLM-L6-v2")
            Settings.llm = llm
            Settings.embed_model = embed_model
            print("✅ LLM and embedding models initialized.")
        except Exception as e:
            print(f"FATAL: Failed to initialize LLM/embeddings: {e}", file=sys.stderr)
            sys.exit(1)
    return llm, embed_model


# Initialize once
get_llm()


# =======================
#   BUILD RAG INDEX
# =======================
def build_rag_index(data_dir="knowledge_files"):
    """Load documents and build RAG vector index."""
    try:
        print(f"\n🔍 Building RAG Index from: {os.path.abspath(data_dir)}")
        if not os.path.exists(data_dir):
            print(f"❌ Directory not found: {data_dir}")
            return None

        documents = SimpleDirectoryReader(data_dir).load_data()
        if not documents:
            print("⚠️ No documents loaded.")
            return None

        index = VectorStoreIndex.from_documents(documents)
        print(f"✅ RAG Index built successfully with {len(documents)} documents.")
        return index

    except Exception as e:
        print(f"❌ Error during RAG index creation: {e}", file=sys.stderr)
        return None


# =======================
#   QUESTION GENERATION
# =======================
def generate_questions_from_resume(rag_index, num_questions=5, context=None):
    """Generate interview questions from resume content using RAG."""
    fallback_questions = [
        {"question": "Can you walk me through your most recent experience?", "source": "fallback"},
        {"question": "What technical skills are you most proficient in?", "source": "fallback"},
        {"question": "Tell me about a challenging project you've worked on.", "source": "fallback"},
        {"question": "What technologies or tools have you used in your recent projects?", "source": "fallback"},
        {"question": "What interests you about this position/role?", "source": "fallback"},
    ]

    if rag_index is None:
        print("⚠️ No RAG index — using fallback questions.")
        return fallback_questions[:num_questions]

    try:
        print("🔍 Generating questions using RAG...")
        retriever = VectorIndexRetriever(index=rag_index, similarity_top_k=2)
        context_nodes = retriever.retrieve("candidate's experience and skills")
        context_str = "\n".join([n.text for n in context_nodes])

        prompt = f"""
        Based on this resume, generate {num_questions} concise, technical interview questions.
        Focus on the candidate's experience and key skills.
        Return a pure JSON list of strings.
        
        RESUME CONTEXT:
        {context_str}
        """

        llm, _ = get_llm()
        response = llm.complete(prompt, max_tokens=300, temperature=0.3, timeout=10)
        response_text = response.text.strip()

        # Clean code fences
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].strip()

        questions = json.loads(response_text)
        if not isinstance(questions, list):
            questions = [questions]

        print(f"✅ Generated {len(questions)} questions from RAG.")
        return [{"question": q, "source": "resume"} for q in questions[:num_questions]]

    except Exception as e:
        print(f"⚠️ Using fallback questions due to error: {e}")
        return fallback_questions[:num_questions]


# =======================
#   RAG ANALYSIS
# =======================
def analyze_with_rag(question, candidate_answer, rag_index):
    """Run concise RAG-based analysis of a candidate’s response."""
    if rag_index is None:
        return "SCORE: 0/10\n\nRAG index unavailable."

    try:
        qa_template = PromptTemplate("""
        Evaluate this interview response concisely.

        QUESTION: {query_str}
        ANSWER: "{candidate_answer}"
        CONTEXT: {context_str}

        RESPONSE FORMAT:
        SCORE: X/10
        RESUME-MATCH: [Resume alignment in few words]
        TECH: [Technical accuracy]
        """)

        retriever = VectorIndexRetriever(index=rag_index, similarity_top_k=2)
        query_engine = RetrieverQueryEngine.from_args(
            retriever=retriever,
            response_mode="compact",
            text_qa_template=qa_template,
        )
        response = query_engine.query(f"Q: {question}\nA: {candidate_answer}")
        return str(response)

    except Exception as e:
        print(f"⚠️ RAG analysis error: {e}")
        return "SCORE: 5/10\nAnalysis unavailable."


# =======================
#   BEHAVIORAL ANALYSIS
# =======================
def analyze_behavioral(question, transcript):
    """Simple behavioral analysis with score and feedback."""
    prompt = f"""
    Rate this behavioral response (1-10) and provide one-line feedback.
    Q: {question}
    A: {transcript}

    FORMAT:
    SCORE: X/10
    FEEDBACK: [1 concise sentence]
    """
    try:
        llm, _ = get_llm()
        response = llm.complete(prompt, max_tokens=100, temperature=0.3)
        text = response.text.strip()
        if "SCORE:" not in text.upper():
            text = "SCORE: 5/10\nFEEDBACK: Response was too brief."
        return text
    except Exception:
        return "SCORE: 5/10\nFEEDBACK: Analysis unavailable."


# =======================
#   NEXT QUESTION GENERATION
# =======================
def generate_next_question(previous_context, candidate_answers, num_questions=1):
    """Generate next interview question(s) based on previous answers."""
    prompt = f"""
    You are an AI interviewer.
    These are previous Q&A exchanges:
    {candidate_answers}

    Context: {previous_context}

    Generate {num_questions} relevant next question(s).
    Keep them short and natural.
    """

    llm, _ = get_llm()
    response = llm.complete(prompt)
    if hasattr(response, "text"):
        return [q for q in response.text.strip().split("\n") if q.strip()]
    return ["Tell me about your background in software development."]


# =======================
#   INTERVIEW TURN PROCESSING
# =======================
def process_interview_turn(question, candidate_answer, rag_index=None, context=""):
    """Analyze one Q&A turn using RAG when available."""
    rag_context = ""
    if rag_index:
        try:
            retriever = VectorIndexRetriever(index=rag_index, similarity_top_k=2)
            nodes = retriever.retrieve(f"Question: {question}\nAnswer: {candidate_answer}")
            rag_context = "\n".join([n.text for n in nodes])
        except Exception as e:
            print(f"⚠️ RAG retrieval failed: {e}")

    analysis_prompt = f"""
    You are an AI interviewer. Analyze this response and provide:
    1. A short reaction (1 sentence)
    2. A score (1-10)
    3. A follow-up question

    QUESTION: {question}
    ANSWER: {candidate_answer}
    CONTEXT: {rag_context if rag_context else 'N/A'}

    Return JSON:
    {{
        "reaction": "...",
        "analysis_score": X,
        "evaluation": "...",
        "next_question": "..."
    }}
    """

    llm, _ = get_llm()
    try:
        response = llm.complete(analysis_prompt, max_tokens=300, temperature=0.5, timeout=10)
        raw_text = getattr(response, "text", str(response))
        result = parse_gemini_response(raw_text)
        result.setdefault("reaction", "Thank you for sharing that.")
        result.setdefault("next_question", "Could you elaborate on that?")
        result.setdefault("analysis_score", 5)
        result.setdefault("evaluation", "Analysis unavailable.")
        return result
    except Exception as e:
        print(f"❌ Error in process_interview_turn: {e}")
        return {
            "reaction": "Thank you for sharing that.",
            "next_question": "Could you elaborate on that?",
            "analysis_score": 5,
            "evaluation": "Analysis unavailable.",
        }
