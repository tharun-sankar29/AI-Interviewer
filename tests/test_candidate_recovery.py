import copy
import io
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import app as application
from backend.interview_session import ANSWER_RECEIPTS, INTERVIEW_STATE, SESSION_CONFIG
from backend.llm_service import LLMServiceUnavailable
from backend.skill_graph import normalize_skill_graph
from llama_index.core.embeddings import MockEmbedding


class CandidateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.old_state = INTERVIEW_STATE.copy()
        self.old_config = copy.deepcopy(SESSION_CONFIG)
        self.old_receipts = copy.deepcopy(ANSWER_RECEIPTS)
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.directory.name)
        self.client = application.app.test_client()
        self.client.post('/api/session/reset')
        self.client.post('/api/session/config', json={'role_id': 'backend-engineer', 'mode': 'quick', 'job_description': 'Build APIs.'})
        self.session_id = SESSION_CONFIG['session_id']
        self.client.post('/api/session/launch')
        self.client.post('/api/session/resume/upload', data={'resume': (io.BytesIO(b'Python APIs and SQL projects.'), 'resume.txt')})

    def tearDown(self):
        os.chdir(self.cwd)
        self.directory.cleanup()
        INTERVIEW_STATE.clear()
        INTERVIEW_STATE.update(self.old_state)
        SESSION_CONFIG.clear()
        SESSION_CONFIG.update(self.old_config)
        ANSWER_RECEIPTS.clear()
        ANSWER_RECEIPTS.update(self.old_receipts)

    def activate(self):
        SESSION_CONFIG['candidate_started'] = True
        INTERVIEW_STATE.update({'execution_phase': 'waiting_for_answer', 'candidate_question_number': 1,
                                'current_question': {'question': 'Explain APIs.'}})

    def answer(self, **changes):
        return {'answer': 'I use reliable API design and database transactions.', 'session_id': self.session_id,
                'question_number': 1, 'request_id': 'answer-1', 'input_mode': 'voice', **changes}

    def test_state_is_private_and_ids_survive_launch_and_upload(self):
        state = self.client.get('/api/session/state')
        self.assertEqual(state.json['session_id'], self.session_id)
        self.assertTrue(state.json['candidate_ready'])
        self.assertNotIn('resume_content', state.json)
        self.assertNotIn('entries', state.json)
        self.assertNotIn('job_description', state.json)
        self.assertIn('no-store', state.headers['Cache-Control'])
        self.activate()
        resumed = self.client.get('/api/session/state').json
        self.assertEqual(resumed['current_question']['question'], 'Explain APIs.')
        self.assertEqual(resumed['question_number'], 1)

    @patch('backend.interview_session.build_rag_index', return_value='resume-index')
    @patch('backend.interview_session.extract_skill_graph', return_value={
        'version': 1,
        'skills': {'python': {'skill_id': 'python', 'name': 'Python', 'mastery': 0, 'confidence': 0}},
        'edges': [],
    })
    def test_start_returns_welcome_with_name_from_uploaded_resume(self, _graph, _index):
        uploaded = self.client.post('/api/session/resume/upload', data={
            'resume': (io.BytesIO(b'Name: Priya Sharma\npriya@example.com\nPython APIs'), 'resume.txt'),
        })
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(uploaded.json['candidate_first_name'], 'Priya')
        started = self.client.post('/api/session/start', json={'session_id': self.session_id})
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json['welcome_message'], "Welcome, Priya! Let's begin.")
        self.assertIn('introduce yourself', started.json['next_question'])

    @patch('backend.interview_session.build_rag_index', return_value='resume-index')
    @patch('backend.interview_session.extract_skill_graph', return_value={
        'version': 1,
        'skills': {'python': {'skill_id': 'python', 'name': 'Python', 'mastery': 0, 'confidence': 0}},
        'edges': [],
    })
    def test_entered_name_is_spoken_when_resume_name_cannot_be_found(self, _graph, _index):
        self.assertIsNone(self.client.get('/api/session/state').json['candidate_first_name'])
        invalid = self.client.post('/api/session/start', json={
            'session_id': self.session_id, 'candidate_first_name': '123',
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(SESSION_CONFIG['candidate_started'])
        started = self.client.post('/api/session/start', json={
            'session_id': self.session_id, 'candidate_first_name': 'Tharun',
        })
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json['welcome_message'], "Welcome, Tharun! Let's begin.")
        self.assertEqual(self.client.get('/api/session/state').json['candidate_first_name'], 'Tharun')

    def test_reset_and_reconfiguration_change_identity(self):
        self.activate()
        INTERVIEW_STATE['graph_source'] = 'gemini'
        reset = self.client.post('/api/session/reset').json
        self.assertNotEqual(reset['session_id'], self.session_id)
        self.assertFalse(reset['candidate_resume_uploaded'])
        self.assertIsNone(reset['candidate_first_name'])
        self.assertFalse(reset['interview_started'])
        self.assertIsNone(self.client.get('/api/log').json['graph_source'])
        reconfigured = self.client.post('/api/session/config', json={'role_id': 'software-engineer'}).json
        self.assertNotEqual(reconfigured['session_id'], reset['session_id'])

    @patch('app.submit_answer')
    def test_stale_session_and_question_never_evaluate(self, submit):
        self.activate()
        for changes, code in [({'session_id': 'old'}, 'SESSION_CHANGED'),
                              ({'question_number': 2}, 'QUESTION_CHANGED')]:
            response = self.client.post('/api/answer', json=self.answer(**changes))
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json['code'], code)
        submit.assert_not_called()

    @patch('app.submit_answer', return_value={'status': 'ongoing', 'next_question': 'Explain SQL.'})
    def test_lost_response_retry_returns_receipt(self, submit):
        self.activate()
        first = self.client.post('/api/answer', json=self.answer())
        second = self.client.post('/api/answer', json=self.answer())
        self.assertEqual(first.json, second.json)
        self.assertEqual(first.json['question_number'], 2)
        self.assertEqual(submit.call_count, 1)
        conflict = self.client.post('/api/answer', json=self.answer(answer='Different answer'))
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json['code'], 'REQUEST_ID_REUSED')

    @patch('app.submit_answer', return_value={'status': 'ongoing', 'next_question': 'Explain SQL.'})
    def test_spoken_answer_with_code_reaches_evaluation_without_latency(self, submit):
        self.activate()
        combined = 'I would sum the values.\n\nAttached text/code:\nfunction sum(items) {\n    return items.reduce((a, b) => a + b, 0);\n}'
        response = self.client.post('/api/answer', json=self.answer(answer=combined))
        self.assertEqual(response.status_code, 200)
        submit.assert_called_once_with(combined, latency_ms=None, input_mode='voice')


    @patch('app.submit_answer', side_effect=[LLMServiceUnavailable('timeout'), {'status': 'ongoing', 'next_question': 'SQL?'}])
    def test_failed_evaluation_can_retry_same_id(self, submit):
        self.activate()
        self.assertEqual(self.client.post('/api/answer', json=self.answer()).status_code, 503)
        self.assertEqual(ANSWER_RECEIPTS, {})
        self.assertEqual(INTERVIEW_STATE['candidate_question_number'], 1)
        self.assertEqual(self.client.post('/api/answer', json=self.answer()).status_code, 200)
        self.assertEqual(submit.call_count, 2)

    def test_final_report_retry_commits_once_and_replays_final_feedback(self):
        graph = normalize_skill_graph({'skills': [
            {'skill_id': name, 'name': name, 'category': 'technical', 'evidence': 'Built production APIs'}
            for name in ('api', 'sql', 'tests', 'monitoring')], 'edges': []})
        serial = 0
        feedback = {'reaction': 'You explained the trade-offs clearly.', 'score': 8,
                    'evaluation': 'Specific and accurate.', 'next_question': 'Can you give an example?'}

        def model(prompt, schema, options):
            nonlocal serial
            serial += 1
            return {'question': f'Explain production design {serial}?'} if set(schema['properties']) == {'question'} else feedback

        with patch('backend.interview_session.build_rag_index', return_value='index'), \
             patch('backend.interview_session.extract_skill_graph', return_value=graph), \
             patch('backend.interview_session.retrieve_evaluation_context', return_value='Project'), \
             patch('backend.interview_session.complete_json', side_effect=model), \
             patch('backend.interview_session.complete_text', side_effect=[LLMServiceUnavailable('timeout'), 'Final report.']):
            self.assertEqual(self.client.post('/api/session/start', json={'session_id': self.session_id}).status_code, 200)
            for turn in range(1, 5):
                self.assertEqual(self.client.post('/api/answer', json=self.answer(question_number=turn, request_id=f'turn-{turn}')).status_code, 200)
            before = copy.deepcopy(INTERVIEW_STATE)
            payload = self.answer(question_number=5, request_id='final')
            self.assertEqual(self.client.post('/api/answer', json=payload).status_code, 503)
            self.assertEqual(INTERVIEW_STATE, before)
            self.assertNotIn('final', ANSWER_RECEIPTS)
            final = self.client.post('/api/answer', json=payload)
            calls_after_completion = serial
            replay = self.client.post('/api/answer', json=payload)
            self.assertEqual(final.status_code, 200)
            self.assertEqual(final.json, replay.json)
            self.assertEqual(serial, calls_after_completion)
            self.assertEqual(len(INTERVIEW_STATE['log']), 4)
            self.assertEqual(final.json['score'], feedback['score'])
            self.assertEqual(final.json['evaluation'], feedback['evaluation'])

    def test_concurrent_duplicate_requests_evaluate_once(self):
        self.activate()
        barrier = threading.Barrier(2)

        def evaluate(*args, **kwargs):
            time.sleep(.05)
            return {'status': 'ongoing', 'next_question': 'SQL?'}

        def send():
            barrier.wait()
            return application.app.test_client().post('/api/answer', json=self.answer()).json

        with patch('app.submit_answer', side_effect=evaluate) as submit, ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: send(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(submit.call_count, 1)

    def test_active_resume_replacement_is_rejected(self):
        self.activate()
        result = self.client.post('/api/session/resume/upload', data={'resume': (io.BytesIO(b'New resume'), 'new.md')})
        self.assertEqual(result.status_code, 400)
        self.assertTrue(SESSION_CONFIG['candidate_started'])
        self.assertEqual(SESSION_CONFIG['resume_filename'], 'resume.txt')

    def test_concurrent_start_runs_initialization_once(self):
        barrier = threading.Barrier(2)

        def initialize(*args, **kwargs):
            time.sleep(.05)
            return {'next_question': 'Introduce yourself.'}

        def send():
            barrier.wait()
            return application.app.test_client().post('/api/session/start', json={'session_id': self.session_id}).status_code

        with patch('app.start_interview', side_effect=initialize) as start, ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: send(), range(2)))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(start.call_count, 1)

    @patch('app.start_interview')
    def test_start_and_upload_reject_reset_identity(self, start):
        result = self.client.post('/api/session/start', json={'session_id': 'old'})
        self.assertEqual(result.status_code, 409)
        result = self.client.post('/api/session/resume/upload', data={'session_id': 'old', 'resume': (io.BytesIO(b'New'), 'new.txt')})
        self.assertEqual(result.status_code, 409)
        start.assert_not_called()

    @patch('app.submit_answer')
    def test_scoped_answer_rejected_after_completion(self, submit):
        self.activate()
        INTERVIEW_STATE['execution_phase'] = 'completed'
        result = self.client.post('/api/answer', json=self.answer())
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json['code'], 'INTERVIEW_NOT_ACTIVE')
        submit.assert_not_called()

    def test_real_state_machine_progression_resume_stop_and_reset(self):
        graph = normalize_skill_graph({'skills': [
            {'skill_id': name, 'name': name, 'category': 'technical', 'evidence': 'resume'}
            for name in ('Python', 'SQL', 'APIs')], 'edges': []})
        question_count = 0

        def model(prompt, schema, options):
            nonlocal question_count
            if 'Assess only the selected skill' in prompt:
                question_count += 1
                return {'question': f'Explain a concrete design choice in project {question_count}.'}
            return {'reaction': 'You explained the trade-offs clearly.', 'score': 8,
                    'evaluation': 'Specific and accurate.', 'next_question': 'Could you add a concrete example?'}

        with patch('backend.analysis_module.get_embed_model', return_value=MockEmbedding(embed_dim=8)), \
             patch('backend.interview_session.extract_skill_graph', return_value=graph), \
             patch('backend.interview_session.complete_json', side_effect=model), \
             patch('backend.interview_session.complete_text', return_value='Final interview report.'):
            started = self.client.post('/api/session/start', json={'session_id': self.session_id})
            self.assertEqual(started.status_code, 200)
            self.assertIsNotNone(INTERVIEW_STATE['rag_index'])
            for number in range(1, 4):
                response = self.client.post('/api/answer', json=self.answer(question_number=number, request_id=f'turn-{number}'))
                self.assertEqual(response.status_code, 200)
                state = self.client.get('/api/session/state').json
                self.assertEqual(state['question_number'], number + 1)
                self.assertEqual(state['current_question']['question'], response.json['next_question'])
            stopped = self.client.post('/api/session/stop', json={'session_id': self.session_id})
            self.assertEqual(stopped.json['status'], 'completed')
            self.assertIn('Thank you for sharing your experience', stopped.json['closing_message'])
            self.assertEqual(stopped.json['reaction'], stopped.json['closing_message'])
            self.assertEqual(self.client.get('/api/session/state').json['execution_phase'], 'completed')
            self.assertEqual(self.client.get('/api/session/state').json['closing_message'], stopped.json['closing_message'])
            self.assertEqual(self.client.get('/api/log').json['report']['narrative'], 'Final interview report.')
            self.client.post('/api/session/reset')
            self.assertEqual(ANSWER_RECEIPTS, {})


if __name__ == '__main__':
    unittest.main()
