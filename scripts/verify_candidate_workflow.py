"""Exercise the real AI/speech services through a complete Quick interview.

Logs are written into an isolated temporary directory. This does not touch the
session of a running server or use a physical microphone/camera.
"""
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import app as application
import numpy as np
from scipy.signal import resample_poly


def speech_answer(client, text):
    response = client.post('/api/tts', json={'text': text})
    assert response.status_code == 200, ('tts', response.status_code)
    with wave.open(io.BytesIO(response.data), 'rb') as wav:
        rate = wav.getframerate()
        assert wav.getsampwidth() == 2
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').reshape(-1, wav.getnchannels()).mean(axis=1)
    divisor = math.gcd(rate, 16000)
    samples = resample_poly(samples, 16000 // divisor, rate // divisor)
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(np.clip(samples, -32768, 32767).astype('<i2').tobytes())
    response = client.post('/api/transcribe', data={'audio': (io.BytesIO(output.getvalue()), 'answer.wav')})
    assert response.status_code == 200, ('transcribe', response.status_code, response.json)
    assert response.json['transcript'].strip()
    return response.json['transcript']


def main():
    client = application.app.test_client()
    report = client.get('/api/health')
    assert report.status_code == 200, report.json
    print('PASS asset readiness', flush=True)
    # Model cache/config paths must remain valid after isolating interview logs.
    for name, default in {
        'WHISPER_MODEL_DIR': 'models/faster-whisper', 'EMBEDDING_CACHE_DIR': 'models/huggingface',
        'PIPER_MODEL_PATH': 'models/piper/en_US-lessac-medium.onnx',
        'PIPER_CONFIG_PATH': 'models/piper/en_US-lessac-medium.onnx.json',
    }.items():
        os.environ[name] = str((ROOT / os.getenv(name, default)).resolve())
    with tempfile.TemporaryDirectory(prefix='interview-live-') as directory:
        os.chdir(directory)
        try:
            client.post('/api/session/reset')
            result = client.post('/api/session/config', json={'role_id': 'backend-engineer', 'mode': 'quick',
                'job_description': 'Design Python APIs, SQL transactions, and automated tests.'})
            session_id = result.json['session_id']
            assert client.post('/api/session/launch').status_code == 200
            assert client.post('/api/session/resume/upload', data={'resume': (
                io.BytesIO(b'Backend engineer with Python APIs, PostgreSQL transactions, testing, and monitoring experience.'), 'demo.txt')}).status_code == 200
            start = client.post('/api/session/start', json={'session_id': session_id})
            assert start.status_code == 200, start.json
            initial_graph = start.json['skill_graph']
            print(f"GRAPH source={start.json['graph_source']} skills={list(initial_graph['skills'])} edges={initial_graph['edges']}", flush=True)
            assert start.json['graph_source'] != 'question_fallback', 'Only a degraded graph was produced'
            assert len(initial_graph['skills']) >= 4, 'Quick interview has fewer than four skill targets'
            print('PASS observer setup, upload, real index and candidate start', flush=True)
            index = 0
            main_seen = 0
            followups_seen = 0
            while True:
                state = client.get('/api/session/state').json
                if state['execution_phase'] == 'completed':
                    break
                assert index < 7, 'Quick exceeded the introduction plus six scored answers'
                number = state['question_number']
                current = state['current_question']
                question = current['question']
                assert client.post('/api/tts', json={'text': question}).status_code == 200
                if current.get('is_followup'):
                    followups_seen += 1
                elif not current.get('is_intro'):
                    main_seen += 1
                weak_main = not current.get('is_intro') and not current.get('is_followup') and main_seen <= 2
                text = ('I build Python APIs backed by PostgreSQL. I test error cases and monitor latency.' if current.get('is_intro') else
                    'I define input validation and use parameterized queries. Database transactions group related writes so errors roll back the operation. I test successful requests and failure cases, and measure latency and error rates before release.')
                transcript = 'I do not know.' if weak_main else speech_answer(client, text)
                payload = {'session_id': session_id, 'question_number': number, 'request_id': f'live-{index}',
                           'answer': transcript, 'input_mode': 'text' if weak_main else 'voice', 'latency_ms': 1500}
                result = client.post('/api/answer', json=payload)
                assert result.status_code == 200, result.json
                repeated = client.post('/api/answer', json=payload)
                assert repeated.json == result.json, 'Receipt replay must return the same response'
                after = client.get('/api/session/state').json
                if result.json['status'] != 'completed':
                    assert after['question_number'] == number + 1
                    assert after['current_question']['question'] == result.json['next_question']
                else:
                    assert result.json['status'] == 'completed' and after['execution_phase'] == 'completed'
                print(f"PASS cycle {index + 1}: score={result.json.get('score')} follow-up={current.get('is_followup', False)} next={result.json.get('next_question', '')}", flush=True)
                index += 1
            log = client.get('/api/log').json
            assert log['planner_state']['execution_phase'] == 'completed'
            assert log['report']['narrative'].strip()
            saved_files = list(Path('interview_logs').glob('interview_*/session.json'))
            assert len(saved_files) == 1, saved_files
            saved = json.loads(saved_files[0].read_text(encoding='utf-8'))
            assert saved['status'] == 'completed'
            assert len(saved['entries']) == 7 and saved['entries'][0]['is_intro']
            assert saved['analysis_file'] == str(saved_files[0].parent / 'analysis.md')
            assert Path(saved['analysis_file']).is_file()
            analysis = Path(saved['analysis_file']).read_text(encoding='utf-8')
            assert analysis.startswith('# Final Interview Analysis')
            assert '## Question-by-question analysis' in analysis
            assert log['report']['narrative'] in analysis
            viewed = client.get('/api/session/report')
            downloaded = client.get('/api/session/report?download=1')
            assert viewed.status_code == downloaded.status_code == 200
            assert viewed.mimetype == downloaded.mimetype == 'text/markdown'
            assert viewed.data == downloaded.data == analysis.encode('utf-8')
            assert 'attachment' in downloaded.headers['Content-Disposition']
            assert 'analysis.md' in downloaded.headers['Content-Disposition']
            evaluated = [entry for entry in log['entries'] if not entry.get('is_intro')]
            tested = [entry['skill_id'] for entry in evaluated if not entry['is_followup']]
            assert len(tested) == 4 and len(set(tested)) == 4, tested
            assert len(evaluated) == 6 and followups_seen == 2, (len(evaluated), followups_seen)
            assert log['planner_state']['followups_used'] == 2
            assert result.json['score'] == evaluated[-1]['score'], 'Completion lost the final score'
            assert result.json['evaluation'] == evaluated[-1]['evaluation'], 'Completion lost the final evaluation'
            assert result.json['reaction'].strip() and result.json['message'].strip()
            assert all(log['skill_graph']['skills'][skill_id]['evidence_count'] >= 1 for skill_id in tested)
            print(f"PASS Quick completion: tested={tested} graph_source={log['graph_source']} edges={len(log['skill_graph']['edges'])}", flush=True)
            print('PASS observer completion, saved JSON and matching final analysis', flush=True)
            reset = client.post('/api/session/reset').json
            assert reset['session_id'] != session_id and not reset['candidate_resume_uploaded']
            assert not client.get('/api/log').json['report_available']
            assert client.get('/api/session/report').status_code == 404
            assert Path(saved['analysis_file']).is_file()
            print('PASS reset', flush=True)
        finally:
            os.chdir(ROOT)


if __name__ == '__main__':
    main()
