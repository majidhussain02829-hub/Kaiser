"""The external scheduler is tested without sending provider requests."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from scheduler import register_scheduler


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.token = 'test-scheduler-token-' + 'a' * 40
        self.env = patch.dict(os.environ, {'QUOTE_SCHEDULER_TOKEN': self.token})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = Flask(__name__)
        self.app.testing = True
        register_scheduler(self.app)
        self.client = self.app.test_client()
        processor_patch = patch('scheduler.process_automations', return_value={
            'queued': 2, 'accepted': 1, 'blocked': 1, 'failed': 0,
            'cancelled': 0, 'unknown': 0,
        })
        self.processor = processor_patch.start()
        self.addCleanup(processor_patch.stop)

    def headers(self, token=None):
        return {'Authorization': 'Bearer ' + (self.token if token is None else token)}

    def test_valid_tick_needs_no_browser_session_and_returns_summary(self):
        response = self.client.post('/api/automations/tick', json={}, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['queued'], 2)
        self.assertEqual(response.json['accepted'], 1)
        self.processor.assert_called_once_with(self.app, time_budget=45, max_jobs=10)

    def test_missing_wrong_and_non_ascii_authorization_do_not_process(self):
        for headers in ({}, self.headers('incorrect'), {'Authorization': self.token},
                        {'Authorization': 'bearer ' + self.token}, self.headers('é' * 40)):
            with self.subTest(header_present=bool(headers)):
                response = self.client.post('/api/automations/tick', json={}, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(self.token, response.get_data(as_text=True))
        self.processor.assert_not_called()

    def test_missing_weak_or_invalid_configuration_disables_scheduler(self):
        for value in ('', 'short', 'é' * 40, 'x' * 31, 'x' * 513, 'x' * 31 + '\n', 'x' * 31 + ' '):
            with self.subTest(token_length=len(value)), patch.dict(os.environ, {'QUOTE_SCHEDULER_TOKEN': value}):
                response = self.client.post('/api/automations/tick', json={}, headers=self.headers())
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(self.token, response.get_data(as_text=True))
                self.assertNotIn('QUOTE_SCHEDULER_TOKEN', response.get_data(as_text=True))
        self.processor.assert_not_called()

    def test_only_empty_json_object_is_accepted(self):
        for payload in ([], None, {'user_id': 1}, '', 0):
            with self.subTest(payload=payload):
                response = self.client.post('/api/automations/tick', data=json.dumps(payload),
                                            content_type='application/json', headers=self.headers())
                self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/automations/tick', data='{}', headers=self.headers())
        self.assertEqual(response.status_code, 415)
        malformed = self.client.post('/api/automations/tick', data='{', content_type='application/json', headers=self.headers())
        self.assertEqual(malformed.status_code, 400)
        self.processor.assert_not_called()

    def test_get_cannot_trigger_scheduler(self):
        response = self.client.get('/api/automations/tick', headers=self.headers())
        self.assertEqual(response.status_code, 405)
        self.processor.assert_not_called()

    def test_authentication_uses_constant_time_comparison(self):
        with patch('scheduler.secrets.compare_digest', return_value=False) as compare:
            response = self.client.post('/api/automations/tick', json={}, headers=self.headers())
        self.assertEqual(response.status_code, 401)
        compare.assert_called_once_with(('Bearer ' + self.token).encode(), ('Bearer ' + self.token).encode())
        self.processor.assert_not_called()

    def test_regular_api_still_requires_csrf(self):
        from app import create_app
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'QUOTE_DESK_DATA': directory}):
            app = create_app(Path(directory) / 'test.sqlite3', testing=True)
            client = app.test_client()
            response = client.post('/api/register', json={}, headers=self.headers())
            self.assertEqual(response.status_code, 403)

    def test_real_application_tick_uses_bearer_instead_of_session_csrf(self):
        from app import create_app
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'QUOTE_DESK_DATA': directory}):
            app = create_app(Path(directory) / 'test.sqlite3', testing=True)
            client = app.test_client()
            response = client.post('/api/automations/tick', json={}, headers=self.headers())
            self.assertEqual(response.status_code, 200)
            self.processor.assert_called_once_with(app, time_budget=45, max_jobs=10)
            self.assertEqual(client.post('/api/register', json={}, headers=self.headers()).status_code, 403)


if __name__ == '__main__':
    unittest.main()
