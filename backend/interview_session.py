from backend.analysis_module import (
    build_rag_index,
    analyze_with_rag,
    get_llm,
    generate_questions_from_resume
)
import re
import json
import datetime
import os
import threading
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Suppress Google Cloud and other noisy loggers
for logger_name in ['google', 'google.cloud', 'google.auth', 'urllib3', 'asyncio']:
    logging.getLogger(logger_name).setLevel(logging.WARNING)

# =======================
#   HELPER FUNCTIONS
# =======================
def parse_gemini_response(raw_response: str):
    """Safely parse Gemini JSON output."""
    try:
        cleaned = re.sub(r"^```json|```$", "", raw_response.strip(), flags=re.MULTILINE).strip()
        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        if json_match:
            cleaned = json_match.group(0)
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "reaction": "I'm sorry, could you repeat that?",
            "next_question": "Let's move to another topic.",
            "analysis_score": 0,
            "evaluation": "Invalid model output."
        }


# =======================
#   CONFIGURATIONS
# =======================
INTERVIEW_MODES = {
    "standard": {"max_questions": 5, "allow_followups": True, "max_followups_per_question": 1},
    "quick": {"max_questions": 2, "allow_followups": True, "max_followups_per_question": 1},
    "thorough": {"max_questions": 3, "allow_followups": True, "max_followups_per_question": 2}
}

INTERVIEW_STATE = {
    "context": "",
    "log": [],
    "next_questions": [],
    "rag_index": None,
    "candidate_answers": "",
    "question_count": 0,
    "mode": "standard",
    "max_questions": INTERVIEW_MODES["standard"]["max_questions"],
    "log_file": "",
    "main_questions": [],
    "current_main_index": 0,
    "awaiting_followup": False
}


# =======================
#   LOGGING FUNCTIONS
# =======================
def save_interview_log(log_entries, log_file, final=False):
    """
    Save logs during the interview.
    - During interview: update JSON + append to log.txt
    - After interview (final=True): generate analysis.txt once
    """
    try:
        log_dir = Path(log_file).parent
        log_dir.mkdir(exist_ok=True)
        logger.info(f"Saving interview log to {log_file}")

        # Save JSON log
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(log_entries, f, indent=2, ensure_ascii=False)
        logger.debug(f"Saved JSON log with {len(log_entries)} entries")

        # Append latest Q/A to log.txt
        if log_entries:
            update_interaction_log(log_entries[-1], log_dir)

        # Generate full analysis only at the very end
        if final:
            logger.info("Generating final analysis...")
            generate_analysis(log_entries, log_dir)
            logger.info(f"Analysis generated at {log_dir}/analysis.txt")

    except Exception as e:
        logger.error(f"Error saving log: {e}", exc_info=True)


def update_interaction_log(log_entry, log_dir):
    """Append the latest interaction to log.txt in interview_logs/"""
    try:
        log_file_path = log_dir / "log.txt"
        log_line = (
            f"\n{'='*80}\n"
            f"[{log_entry.get('timestamp')}]\n"
            f"Q: {log_entry.get('question', '')}\n"
            f"A: {log_entry.get('answer', '')}\n"
            f"Score: {log_entry.get('score', 'N/A')}/10\n"
            f"Evaluation: {log_entry.get('evaluation', '').split('\\n')[0]}\n"
        )
        
        # Read existing content to check for duplicates
        existing_content = ""
        if log_file_path.exists():
            with open(log_file_path, 'r', encoding='utf-8') as f:
                existing_content = f.read()
        
        # Only append if this exact log entry doesn't already exist
        if log_line.strip() not in existing_content:
            with open(log_file_path, 'a', encoding='utf-8') as f:
                f.write(log_line)
            logger.debug(f"Updated interaction log at {log_file_path}")
        else:
            logger.debug(f"Skipping duplicate log entry for question: {log_entry.get('question', '')}")
            
    except Exception as e:
        logger.error(f"Error updating interaction log: {e}", exc_info=True)


# =======================
#   FINAL ANALYSIS (RUNS ONLY AFTER INTERVIEW)
# =======================
def generate_analysis(log_entries, log_dir):
    """Generate a detailed analysis of the interview in analysis.txt"""
    if not log_entries:
        return
        
    try:
        # Basic metrics
        scores = [entry.get('score', 0) for entry in log_entries if isinstance(entry.get('score'), (int, float))]
        avg_score = sum(scores) / len(scores) if scores else 0
        total_questions = len(log_entries)
        
        # Categorize questions
        followups = [e for e in log_entries if e.get('is_followup', False)]
        main_questions = [e for e in log_entries if not e.get('is_followup', False)]
        
        # Score analysis
        main_scores = [e.get('score', 0) for e in main_questions if isinstance(e.get('score'), (int, float))]
        followup_scores = [e.get('score', 0) for e in followups if isinstance(e.get('score'), (int, float))]
        
        avg_main_score = sum(main_scores) / len(main_scores) if main_scores else 0
        avg_followup_score = sum(followup_scores) / len(followup_scores) if followup_scores else 0
        
        # Response length analysis
        response_lengths = [len(e.get('answer', '').split()) for e in log_entries]
        avg_response_length = sum(response_lengths) / len(response_lengths) if response_lengths else 0
        
        # Prepare transcript for LLM analysis
        transcript = "\n".join([
            f"Q{e.get('is_followup', '') and ' (Follow-up)'}: {e.get('question', '')}\n"
            f"A: {e.get('answer', '')}\n"
            f"Score: {e.get('score', 'N/A')}/10\n"
            f"Evaluation: {e.get('evaluation', '').split('\\n')[0]}\n"
            for e in log_entries
        ])

        # Generate detailed analysis using LLM
        llm, _ = get_llm()
        analysis_prompt = f"""
        Analyze this interview transcript in detail and provide a comprehensive report with these sections:
        
        1. PERFORMANCE SUMMARY
           - Overall assessment of candidate's performance
           - Key strengths demonstrated
           - Main areas for improvement
           
        2. TECHNICAL PROFICIENCY
           - Depth of technical knowledge shown
           - Problem-solving approach
           - Technical communication skills
           
        3. COMMUNICATION SKILLS
           - Clarity and structure of responses
           - Ability to explain complex concepts
           - Engagement and interaction quality
           
        4. DETAILED QUESTION ANALYSIS
           - Best answered question and why
           - Most challenging question and why
           - Consistency across responses
           
        5. RECOMMENDATIONS
           - Specific areas for improvement
           - Suggested learning resources
           - General career advice
        
        INTERVIEW TRANSCRIPT:
        {transcript}
        """

        try:
            response = llm.complete(analysis_prompt, max_tokens=1200, temperature=0.7)
            llm_analysis = response.text.strip()
        except Exception as e:
            print(f"⚠️ LLM Analysis Error: {e}")
            llm_analysis = "Detailed analysis unavailable due to an error."

        # Generate content with detailed metrics and analysis
        content = (
            f"{'='*80}\n"
            f"FINAL INTERVIEW ANALYSIS - {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"{'='*80}\n\n"
            f"📊 INTERVIEW METRICS\n"
            f"{'='*80}\n"
            f"• Total Questions: {total_questions} ({len(main_questions)} main, {len(followups)} follow-ups)\n"
            f"• Overall Score: {avg_score:.1f}/10\n"
            f"• Main Questions Average: {avg_main_score:.1f}/10\n"
            f"• Follow-up Questions Average: {avg_followup_score:.1f}/10\n"
            f"• Average Response Length: {avg_response_length:.1f} words\n\n"
            f"📈 SCORE DISTRIBUTION\n"
            f"{'='*80}\n"
        )
        
        # Add score distribution
        score_counts = {i: 0 for i in range(11)}
        for score in scores:
            score_rounded = round(score)
            score_counts[score_rounded] = score_counts.get(score_rounded, 0) + 1
            
        for score in range(10, -1, -1):
            count = score_counts.get(score, 0)
            if count > 0:
                bar = '█' * count
                content += f"{score:2d}/10 | {bar} ({count})\n"
        
        content += f"\n{llm_analysis}\n\n"
        
        # Add question-by-question breakdown
        content += f"\n{'='*80}\n"
        content += f"📝 QUESTION-BY-QUESTION ANALYSIS\n"
        for i, entry in enumerate(log_entries, 1):
            q_type = "(Follow-up)" if entry.get('is_followup', False) else "(Main)"
            content += (
                f"\n{'='*80}\n"
                f"Q{i} {q_type}: {entry.get('question', '')}\n"
                f"Score: {entry.get('score', 'N/A')}/10\n"
                f"Evaluation: {entry.get('evaluation', '')}\n"
            )
        
        content += f"\n{'='*80}\n"
        content += f"END OF ANALYSIS\n"
        analysis_path = log_dir / "analysis.txt"
        with open(analysis_path, 'w', encoding='utf-8') as f:
            f.write(content)

        print(f"\n📘 Generated detailed analysis.txt → {analysis_path}")
    except Exception as e:
        print(f"⚠️ Error generating analysis.txt: {e}")
        import traceback
        traceback.print_exc()


# =======================
#   INTERVIEW START
# =======================
def start_interview(context: str, seed_questions=None, mode="quick"):
    """Initialize a new interview session."""
    if mode not in INTERVIEW_MODES:
        mode = "standard"

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path("interview_logs")
    log_dir.mkdir(exist_ok=True)

    # Clear existing log files
    log_file = log_dir / f"interview_{timestamp}.json"
    log_txt = log_dir / "log.txt"
    analysis_file = log_dir / "analysis.txt"

    # Clear files if they exist
    for file in [log_txt, analysis_file]:
        try:
            if file.exists():
                file.write_text("")
                logger.info(f"Cleared existing {file.name}")
        except Exception as e:
            logger.warning(f"Could not clear {file.name}: {e}")

    logger.info(f"Starting new interview. Log file: {log_file}")

    llm, _ = get_llm()
    rag_index = INTERVIEW_STATE["rag_index"] or build_rag_index()

    if not seed_questions:
        try:
            generated = generate_questions_from_resume(rag_index, num_questions=5)
        except Exception:
            generated = []
        seed_questions = [q["question"] for q in generated] or [
            "Tell me about your experience with backend systems.",
            "Can you describe a challenging problem you solved recently?",
            "How do you approach learning new technologies?"
        ]

    main_questions = [{"question": q, "is_followup": False} for q in seed_questions[:INTERVIEW_MODES[mode]["max_questions"]]]

    print("\n📋 GENERATED INTERVIEW QUESTIONS:")
    for i, q in enumerate(main_questions, 1):
        print(f"{i}. {q['question']}")
    print("\n" + "="*80 + "\n")

    INTERVIEW_STATE.update({
        "context": context,
        "log": [],
        "next_questions": main_questions.copy(),
        "candidate_answers": "",
        "question_count": 0,
        "mode": mode,
        "max_questions": INTERVIEW_MODES[mode]["max_questions"],
        "log_file": str(log_file),
        "main_questions": main_questions,
        "current_main_index": 0,
        "awaiting_followup": False,
        "rag_index": rag_index
    })

    print("\n" + "=" * 80)
    print(f"📋 INTERVIEW STARTED [{mode.upper()} MODE]")
    print(f"🕒 {timestamp}")
    print(f"💾 Log File: {log_file}")
    print("=" * 80 + "\n")

    return {
        "message": "Interview started.",
        "welcome_message": "Welcome! Let's begin.",
        "next_question": main_questions[0]["question"]
    }


# =======================
#   ANSWER PROCESSING
# =======================
def submit_answer(candidate_answer: str):
    """Process candidate's answer, evaluate it, and determine the next logical question."""
    logger.info("Processing candidate's answer...")

    if not INTERVIEW_STATE["next_questions"]:
        logger.info("No questions in queue. Ending interview.")
        return generate_interview_closing()

    # Fetch current question and remove it from the queue
    current_question = INTERVIEW_STATE["next_questions"].pop(0)
    INTERVIEW_STATE["candidate_answers"] += f"\nQ: {current_question['question']}\nA: {candidate_answer}"

    print("\n" + "=" * 80)
    print(f"💬 QUESTION ({'FOLLOW-UP' if current_question.get('is_followup') else 'MAIN'}): {current_question['question']}")
    print(f"📝 ANSWER: {candidate_answer}")
    print("=" * 80 + "\n")

    # Run analysis using RAG
    logger.info("Running RAG-based analysis...")
    try:
        analysis = analyze_with_rag(current_question["question"], candidate_answer, INTERVIEW_STATE["rag_index"])
        score_match = re.search(r"(\d+)/10", analysis)
        score = int(score_match.group(1)) if score_match else 0
    except Exception as e:
        logger.error(f"RAG analysis failed: {e}", exc_info=True)
        analysis = f"Fallback analysis: {candidate_answer}"
        score = 0

    # Log analysis summary
    logger.info(f"📊 Question: {current_question['question']}")
    logger.info(f"🧠 Score: {score}/10")
    logger.info(f"🗒️ Evaluation: {analysis}")

    # Generate interviewer feedback and follow-up using LLM
    llm, _ = get_llm()
    try:
        prompt = f"""
        You are an interviewer assessing a candidate.
        Question: {current_question['question']}
        Answer: {candidate_answer}
        Analsyis : {analysis}
        Provide JSON with:
        {{
            "reaction": "One-sentence natural feedback",
            "next_question": "Relevant follow-up question if needed"
        }}
        """
        raw_response = llm.complete(prompt, max_tokens=150, temperature=0.6).text.strip()
        resp = parse_gemini_response(raw_response)
    except Exception as e:
        logger.warning(f"Follow-up generation failed: {e}")
        resp = {"reaction": "Interesting point.", "next_question": "Could you elaborate on that?"}

    # Log entry for this question
    log_entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "question": current_question["question"],
        "answer": candidate_answer,
        "evaluation": analysis,
        "score": score,
        "is_followup": current_question.get("is_followup", False)
    }

    INTERVIEW_STATE["log"].append(log_entry)
    save_interview_log(INTERVIEW_STATE["log"], INTERVIEW_STATE["log_file"])

    # =======================
    #  CONTEXT-AWARE LOGIC
    # =======================
    next_question = None
    mode = INTERVIEW_STATE["mode"]
    allow_followups = INTERVIEW_MODES[mode]["allow_followups"]

    # Determine if follow-up is needed
    short_answer = len(candidate_answer.strip()) < 20 or score < 5
    missing_context = ""

    # Example domain-based follow-up (you can extend this)
    if "database" in current_question["question"].lower() and "index" not in candidate_answer.lower():
        missing_context = "Could you elaborate on a specific database optimization technique you applied, such as indexing or query refactoring?"

    if missing_context:
        logger.info("Context-aware follow-up triggered.")
        next_question = {"question": missing_context, "is_followup": True}
        INTERVIEW_STATE["awaiting_followup"] = True

    elif allow_followups and short_answer and not INTERVIEW_STATE["awaiting_followup"]:
        logger.info("Short or low-score answer → generating LLM-based follow-up.")
        next_question = {"question": resp["next_question"], "is_followup": True}
        INTERVIEW_STATE["awaiting_followup"] = True

    else:
        # Move to next main question
        INTERVIEW_STATE["awaiting_followup"] = False
        INTERVIEW_STATE["current_main_index"] += 1
        if INTERVIEW_STATE["current_main_index"] < len(INTERVIEW_STATE["main_questions"]):
            next_question = INTERVIEW_STATE["main_questions"][INTERVIEW_STATE["current_main_index"]]

    # End if no next question remains
    if not next_question:
        logger.info("No more questions → ending interview.")
        return generate_interview_closing()

    # Prepare for next turn
    INTERVIEW_STATE["next_questions"] = [next_question]

    logger.info(f"➡️ Next Question: {next_question['question']} ({'Follow-up' if next_question['is_followup'] else 'Main'})")

    # Return interviewer reaction and next question
    return {
        "reaction": resp["reaction"],
        "next_question": next_question["question"],
        "evaluation": analysis,
        "score": score,
        "status": "ongoing"
    }


# =======================
#   INTERVIEW ENDING
# =======================
def generate_interview_closing():
    """Generate a natural closing and then background analysis."""
    log_file = INTERVIEW_STATE["log_file"]
    log_dir = Path(log_file).parent

    # Save final log (no analysis yet)
    save_interview_log(INTERVIEW_STATE["log"], log_file, final=False)

    llm, _ = get_llm()
    prompt = """
    The interview is complete.
    Write exactly ONE short, professional closing message (1–2 sentences).
    Do not provide multiple options or enumerations.
    The message should thank the candidate.
    """

    try:
        response = llm.complete(prompt, max_tokens=80, temperature=0.7)
        closing_message = response.text.strip() or "Thank you for your time. We'll review your responses and follow up soon."
    except Exception:
        closing_message = "Thank you for your time. We'll review your responses and follow up soon."

    print("\n🏁 INTERVIEW COMPLETE")
    print(f"💬 Final Message: {closing_message}")
    print("=" * 80 + "\n")

    # Run analysis in background thread
    def generate_final_analysis():
        try:
            logger.info("\n📊 Generating final analysis...")
            analysis_file = log_dir / "analysis.txt"
            save_interview_log(INTERVIEW_STATE["log"], log_file, final=True)
            logger.info(f"✅ Analysis saved to: {analysis_file.absolute()}")
        except Exception as e:
            logger.error(f"❌ Error generating analysis: {e}", exc_info=True)

    threading.Thread(target=generate_final_analysis, daemon=True).start()

    return {
        "reaction": closing_message,
        "response": closing_message,  # Frontend looks for this field
        "next_question": "",
        "evaluation": "Interview completed. Thank you for your time.",
        "score": 0,
        "status": "completed",
        "message": closing_message  # Keep for backward compatibility
    }


# =======================
#   LOG RETRIEVAL
# =======================
def get_full_log():
    return INTERVIEW_STATE["log"]
