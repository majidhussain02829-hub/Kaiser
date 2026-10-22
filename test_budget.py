"""Bounded WSGI scheduler passes preserve work without live provider calls."""
import io
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask, g

from app import create_app
from automations import AutomationBudgetExpired, _post, process_automations


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class SchedulerBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'budget.sqlite3'
        environment = patch.dict(os.environ, {
            'QUOTE_DESK_DATA': temporary.name,
            'QUOTE_EMAIL_API_KEY': '', 'QUOTE_EMAIL_FROM': '',
            'QUOTE_SMS_ACCOUNT_SID': '', 'QUOTE_SMS_AUTH_TOKEN': '',
            'QUOTE_SMS_FROM': '', 'QUOTE_AI_API_KEY': '',
        })
        environment.start()
        self.addCleanup(environment.stop)
        guard = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No live network in budget tests'))
        network = guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(network.assert_not_called)
        self.app = create_app(self.path, testing=True)
        self.client = self.app.test_client()
        self.clock = Clock()
        self.now = datetime(2026, 10, 23, tzinfo=timezone.utc)
        registered = self.send('POST', '/api/register', dict(email='owner@example.com', password='long-budget-test-password', business='Budget Test'))
        self.assertEqual(registered.status_code, 201)

    def send(self, method, path, payload):
        token = self.client.get('/api/session').json['csrf']
        return self.client.open(path, method=method, json=payload, headers={'X-CSRF-Token': token})

    def create(self, name='Customer'):
        response = self.send('POST', '/api/enquiries', dict(
            name=name, email='customer@example.com', phone='', suburb='Gillen', service='Plumbing',
            job='Repair kitchen tap', status='Quoted', amount='350.00', followup='2026-10-22',
            email_consent=True, sms_consent=False))
        self.assertEqual(response.status_code, 201)
        return response.json['id']

    def queue(self, identifier, number):
        response = self.send('POST', f'/api/enquiries/{identifier}/messages', dict(
            channel='email', subject='A reviewed message', body='Can we help with your quote?',
            request_id=f'budget-request-{number:03d}'))
        self.assertEqual(response.status_code, 201)

    def settings(self):
        response = self.send('PUT', '/api/automations', dict(customer_email_enabled=True, ai_enabled=True))
        self.assertEqual(response.status_code, 200)

    def messages(self):
        with sqlite3.connect(self.path) as database:
            database.row_factory = sqlite3.Row
            return [dict(row) for row in database.execute('SELECT * FROM messages')]

    def process(self, **kwargs):
        with patch('automations.time.monotonic', self.clock):
            return process_automations(self.app, now=self.now, **kwargs)

    def test_claim_limit_leaves_pending_messages_queued(self):
        identifier = self.create()
        for number in range(3):
            self.queue(identifier, number)
        sender = Mock(return_value='provider-accepted')
        self.app.config['QUOTE_EMAIL_SEND'] = sender
        first = self.process(time_budget=45, max_jobs=1)
        self.assertEqual(first['accepted'], 1)
        self.assertEqual(sender.call_count, 1)
        self.assertCountEqual([row['status'] for row in self.messages()], ['accepted', 'queued', 'queued'])
        self.process(time_budget=45, max_jobs=1)
        self.assertEqual(sender.call_count, 2)
        self.process()
        self.assertEqual(sender.call_count, 3)
        self.assertTrue(all(row['status'] == 'accepted' for row in self.messages()))

    def test_empty_budget_does_not_claim_or_draft(self):
        identifier = self.create()
        self.queue(identifier, 1)
        self.settings()
        sender = Mock(return_value='accepted')
        ai = Mock(return_value=dict(subject='Hello', body='How can we help?'))
        self.app.config.update(QUOTE_EMAIL_SEND=sender, QUOTE_AI_DRAFT=ai)
        for kwargs in (dict(time_budget=0), dict(max_jobs=0)):
            result = self.process(**kwargs)
            self.assertTrue(all(value == 0 for value in result.values()))
        sender.assert_not_called()
        ai.assert_not_called()
        self.assertEqual([row['status'] for row in self.messages()], ['queued'])

    def test_pending_messages_are_sent_before_new_ai_drafts(self):
        identifier = self.create()
        self.queue(identifier, 1)
        self.settings()
        order = []

        def send(message):
            order.append('send')
            self.clock.value += 1
            return 'accepted'

        def draft(context, channel):
            order.append('draft')
            self.clock.value += 50
            return dict(subject='A follow-up', body='Do you have any questions?')

        self.app.config.update(QUOTE_EMAIL_SEND=send, QUOTE_AI_DRAFT=draft)
        result = self.process(time_budget=45, max_jobs=10)
        self.assertEqual(order, ['send', 'draft'])
        self.assertEqual((result['accepted'], result['queued']), (1, 1))
        # An expensive completed draft is durable, ready for a later tick.
        self.assertCountEqual([row['status'] for row in self.messages()], ['accepted', 'queued'])

    def test_new_queue_limit_avoids_extra_ai_calls(self):
        for number in range(3):
            self.create(f'Customer {number}')
        self.settings()
        ai = Mock(return_value=dict(subject='A follow-up', body='Can we help with your quote?'))
        self.app.config['QUOTE_AI_DRAFT'] = ai
        result = self.process(dispatch=False, time_budget=45, max_jobs=2)
        self.assertEqual(result['queued'], 2)
        self.assertEqual(ai.call_count, 2)
        self.assertEqual(len(self.messages()), 2)
        self.assertTrue(all(row['status'] == 'queued' for row in self.messages()))

    def test_completed_send_is_recorded_when_it_uses_remaining_time(self):
        identifier = self.create()
        for number in range(3):
            self.queue(identifier, number)
        calls = []

        def send(message):
            calls.append(message['id'])
            self.clock.value += 46
            return 'accepted'

        self.app.config['QUOTE_EMAIL_SEND'] = send
        result = self.process(time_budget=45, max_jobs=10)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(len(calls), 1)
        self.assertCountEqual([row['status'] for row in self.messages()], ['accepted', 'queued', 'queued'])

    def test_expiry_before_provider_contact_releases_claim(self):
        identifier = self.create()
        self.queue(identifier, 1)
        sender = Mock(return_value='must-not-send')
        self.app.config['QUOTE_EMAIL_SEND'] = sender

        def expire_before_send(app):
            self.clock.value += 46
            return {'email': {'configured': True}}

        with patch('automations.provider_status', side_effect=expire_before_send):
            result = self.process(time_budget=45, max_jobs=10)
        sender.assert_not_called()
        self.assertEqual(result['accepted'], 0)
        self.assertEqual(self.messages()[0]['status'], 'queued')
        self.process(time_budget=45, max_jobs=10)
        sender.assert_called_once()
        self.assertEqual(self.messages()[0]['status'], 'accepted')

    def test_budget_arguments_reject_invalid_limits(self):
        for kwargs in (dict(time_budget=-1), dict(time_budget=float('inf')), dict(time_budget=True),
                       dict(max_jobs=-1), dict(max_jobs=1.5), dict(max_jobs=True)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.process(**kwargs)


class ProviderBudgetTests(unittest.TestCase):
    def test_provider_socket_timeout_uses_remaining_pass_budget(self):
        app = Flask(__name__)
        with app.app_context(), patch('automations.time.monotonic', return_value=100):
            for deadline, expected in ((105.5, 5.5), (140, 25), (None, 25)):
                with self.subTest(deadline=deadline):
                    g.automation_deadline = deadline
                    with patch('automations.urlopen', return_value=io.BytesIO(b'{}')) as post:
                        self.assertEqual(_post('https://api.example.com/test', {}, {}), {})
                    self.assertEqual(post.call_args.kwargs['timeout'], expected)

    def test_expired_budget_never_starts_http_request(self):
        app = Flask(__name__)
        with app.app_context(), patch('automations.time.monotonic', return_value=100):
            g.automation_deadline = 100
            with patch('automations.urlopen') as post, self.assertRaises(AutomationBudgetExpired):
                _post('https://api.example.com/test', {}, {})
            post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
