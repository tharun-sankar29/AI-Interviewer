#!/usr/bin/env python3
"""Simple warmed local speech benchmark for the target laptop."""

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.speech_service import stt_service, tts_service


def measure(action, runs):
    values = []
    for _ in range(runs):
        started = time.perf_counter()
        action()
        values.append(time.perf_counter() - started)
    return statistics.median(values), values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, help="A mono 16-bit 16 kHz WAV, ideally 30 seconds")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    # Warm both singleton models before recording timings.
    tts_service.synthesize("Welcome to the interview.")
    tts_median, _ = measure(lambda: tts_service.synthesize("Please explain a technical problem you solved and the tradeoffs you considered."), args.runs)
    print(f"Piper median: {tts_median:.3f}s (target <1.000s)")

    if args.audio:
        audio = args.audio.read_bytes()
        stt_service.transcribe(audio)
        stt_median, _ = measure(lambda: stt_service.transcribe(audio), args.runs)
        print(f"Whisper median: {stt_median:.3f}s (30-second target <3.000s)")


if __name__ == "__main__":
    main()
