"""Automation tests use temporary data and mocks; no customer messages leave tests."""
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from app import create_app


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.env = patch.dict(os.environ, {
            'QUOTE_DESK_DATA': self.temp.name,
            'QUOTE_EMAIL_API_KEY': '', 'QUOTE_EMAIL_FROM': '',
            'QUOTE_SMS_ACCOUNT_SID': '', 'QUOTE_SMS_AUTH_TOKEN': '',
            'QUOTE_SMS_FROM': '', 'QUOTE_AI_API_KEY': '',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)
        network_guard = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Live provider HTTP is forbidden in tests'))
        self.network_guard = network_guard.start()
        self.addCleanup(network_guard.stop)
        self.addCleanup(self.network_guard.assert_not_called)
        self.app = create_app(self.path, testing=True)
        self.a = self.app.test_client()
        self.b = self.app.test_client()
        self.payload = dict(name='Customer One', email='customer@example.com',
                            phone='+61412345678', suburb='Gillen', service='Plumbing',
                            job='Replace kitchen tap', status='Quoted', amount='450.00',
                            followup='2026-10-22', email_consent=True, sms_consent=True)
        self.register(self.a, 'owner@example.com')

    def send(self, client, method, path, data):
        csrf = client.get('/api/session').json['csrf']
        return client.open(path, method=method, json=data,
                           headers={'X-CSRF-Token': csrf})

    def register(self, client, email):
        response = self.send(client, 'POST', '/api/register',
                             dict(email=email, password='test-password-long', business='Test Plumbing'))
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['user']

    def create(self, **overrides):
        response = self.send(self.a, 'POST', '/api/enquiries', dict(self.payload, **overrides))
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['id']

    def settings(self, **changes):
        response = self.send(self.a, 'PUT', '/api/automations', changes)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def rows(self, table):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute('SELECT * FROM ' + table)]

    def process(self, hour=0, day=22, dispatch=True):
        from automations import process_automations
        return process_automations(self.app, now=datetime(2026, 10, day, hour, tzinfo=timezone.utc), dispatch=dispatch)

    def messages(self, client=None):
        response = (client or self.a).get('/api/messages')
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['messages']

    def queue_manual(self, identifier, channel='email', request_id='request-test-001', **changes):
        payload = dict(channel=channel, subject='Your plumbing quote', body='Hello, following up on your quote.', request_id=request_id)
        payload.update(changes)
        return self.send(self.a, 'POST', '/api/enquiries/' + identifier + '/messages', payload)

    def update(self, identifier, **changes):
        entry = next(e for e in self.a.get('/api/enquiries').json['entries'] if e['id'] == identifier)
        entry.update(changes)
        for channel in ('email', 'sms'):
            entry[channel + '_consent'] = bool(entry[channel + '_consent'])
        response = self.send(self.a, 'PUT', '/api/enquiries/' + identifier, entry)
        self.assertEqual(response.status_code, 200, response.json)
        return entry

    def test_settings_defaults_and_validation(self):
        result = self.a.get('/api/automations')
        self.assertEqual(result.status_code, 200, result.json)
        settings = result.json['settings']
        self.assertEqual(settings['timezone'], 'Australia/Darwin')
        self.assertEqual(settings['reminder_hour'], 9)
        for key in ('owner_email_enabled', 'customer_email_enabled', 'customer_sms_enabled', 'ai_enabled'):
            self.assertFalse(settings[key])
        for channel in ('email', 'sms', 'ai'):
            self.assertFalse(result.json['providers'][channel]['configured'])
        for invalid in [dict(timezone='Invalid/Zone'), dict(reminder_hour=-1), dict(reminder_hour=24),
                        dict(owner_email_enabled='yes'), dict(customer_sms_enabled=1)]:
            with self.subTest(invalid=invalid):
                self.assertEqual(self.send(self.a, 'PUT', '/api/automations', invalid).status_code, 400)
        self.settings(timezone='Australia/Sydney', reminder_hour=10)
        self.assertEqual(self.a.get('/api/automations').json['settings']['timezone'], 'Australia/Sydney')

    def test_authentication_csrf_and_business_isolation(self):
        self.register(self.b, 'other@example.com')
        identifier = self.create()
        self.settings(customer_email_enabled=True)
        anon = self.app.test_client()
        for path in ('/api/automations', '/api/reminders', '/api/messages'):
            self.assertEqual(anon.get(path).status_code, 401)
        self.assertEqual(self.a.put('/api/automations', json={}).status_code, 403)
        self.assertEqual(self.b.get('/api/automations').json['settings']['customer_email_enabled'], False)
        self.assertEqual(self.b.get('/api/messages').json['messages'], [])
        self.assertEqual(self.b.get('/api/reminders').json['reminders'], [])
        for suffix, payload in [('draft', dict(channel='email', purpose='followup')),
                                ('messages', dict(channel='email', subject='Hello', body='Hello there', request_id='other-request-001'))]:
            self.assertIn(self.send(self.b, 'POST', f'/api/enquiries/{identifier}/{suffix}', payload).status_code, (403, 404))

    def test_consent_roundtrip_validation_and_public_default(self):
        identifier = self.create(email_consent=True, sms_consent=False)
        entry = self.a.get('/api/enquiries').json['entries'][0]
        self.assertTrue(entry['email_consent'])
        self.assertFalse(entry['sms_consent'])
        self.assertEqual(self.queue_manual(identifier, 'sms').status_code, 400)
        self.update(identifier, email_consent=False)
        self.assertEqual(self.queue_manual(identifier).status_code, 400)
        for invalid in ('true', 1, None):
            response = self.send(self.a, 'POST', '/api/enquiries', dict(self.payload, sms_consent=invalid))
            self.assertEqual(response.status_code, 400)
        public = self.a.get('/api/session').json['user']['public_id']
        payload = dict(self.payload)
        payload.pop('email_consent')
        payload.pop('sms_consent')
        response = self.send(self.b, 'POST', '/api/public/' + public, payload)
        self.assertEqual(response.status_code, 201)
        fresh = next(e for e in self.a.get('/api/enquiries').json['entries'] if e['id'] != identifier)
        self.assertFalse(fresh['email_consent'])
        self.assertFalse(fresh['sms_consent'])

    def test_manual_queue_idempotency_missing_provider_and_explicit_retry(self):
        identifier = self.create()
        first = self.queue_manual(identifier)
        self.assertEqual(first.status_code, 201, first.json)
        self.assertEqual(self.queue_manual(identifier).status_code, 200)
        self.assertEqual(self.queue_manual(identifier, body='Different body').status_code, 409)
        self.assertEqual(len(self.messages()), 1)
        self.process()
        blocked = self.messages()[0]
        self.assertEqual(blocked['status'], 'blocked')
        self.assertTrue(blocked['error'])
        calls = []
        self.app.config['QUOTE_EMAIL_SEND'] = lambda message: calls.append(message) or 'provider-email-001'
        self.process()
        self.assertEqual(calls, [], 'Blocked jobs must not silently send when configuration changes')
        retry = self.send(self.a, 'POST', f"/api/messages/{blocked['id']}/retry", {})
        self.assertEqual(retry.status_code, 200, retry.json)
        self.process()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.messages()[0]['provider_id'], 'provider-email-001')
        self.assertEqual(self.messages()[0]['status'], 'accepted')
        self.process()
        self.assertEqual(len(calls), 1)

    def test_automated_reminder_timezone_and_once_per_followup_date(self):
        self.create()
        self.settings(owner_email_enabled=True, customer_email_enabled=True, customer_sms_enabled=True)
        email, sms = [], []
        self.app.config['QUOTE_EMAIL_SEND'] = lambda message: email.append(message) or 'email-accepted'
        self.app.config['QUOTE_SMS_SEND'] = lambda message: sms.append(message) or 'sms-accepted'
        # Midnight UTC is 09:30 Darwin: jobs due today are eligible after local 09:00.
        self.process(hour=23, day=21)
        self.assertEqual(len(email), 0)
        self.process(hour=0)
        self.assertEqual(len(email), 2)
        self.assertEqual(len(sms), 1)
        self.assertIn('owner@example.com', [m['recipient'] for m in email])
        self.assertIn('customer@example.com', [m['recipient'] for m in email])
        self.process(hour=1)
        self.process(hour=0, day=23)
        self.assertEqual((len(email), len(sms)), (2, 1), 'An overdue date must not repeatedly send messages')

    def test_automated_customer_only_quotes_and_explicit_consent(self):
        self.create(status='New')
        self.create(status='Contacted')
        self.create(status='Won')
        self.create(status='Lost')
        self.create(email_consent=False, sms_consent=False)
        self.settings(customer_email_enabled=True, customer_sms_enabled=True)
        self.app.config['QUOTE_EMAIL_SEND'] = Mock(return_value='email')
        self.app.config['QUOTE_SMS_SEND'] = Mock(return_value='sms')
        self.process()
        self.app.config['QUOTE_EMAIL_SEND'].assert_not_called()
        self.app.config['QUOTE_SMS_SEND'].assert_not_called()
        self.assertEqual(self.messages(), [])

    def test_queued_automation_suppressed_after_status_consent_or_schedule_changes(self):
        self.settings(customer_email_enabled=True)
        self.app.config['QUOTE_EMAIL_SEND'] = Mock(return_value='email')
        for change in [dict(status='Won'), dict(status='Lost'), dict(email_consent=False), dict(followup='2026-11-01'), dict(amount='900.00')]:
            identifier = self.create()
            self.process(dispatch=False)
            self.update(identifier, **change)
            self.process()
        self.app.config['QUOTE_EMAIL_SEND'].assert_not_called()
        self.assertTrue(all(m['status'] == 'cancelled' for m in self.messages()))

    def test_delete_or_disabled_settings_cancel_pending_automations(self):
        identifier = self.create()
        self.settings(customer_sms_enabled=True)
        self.app.config['QUOTE_SMS_SEND'] = Mock(return_value='sms')
        self.process(dispatch=False)
        self.settings(customer_sms_enabled=False)
        self.process()
        self.app.config['QUOTE_SMS_SEND'].assert_not_called()
        self.assertEqual(self.messages()[0]['status'], 'cancelled')
        self.settings(customer_sms_enabled=True)
        identifier = self.create()
        self.process(dispatch=False)
        self.assertEqual(self.send(self.a, 'DELETE', '/api/enquiries/' + identifier, dict(version=1)).status_code, 200)
        self.process()
        self.app.config['QUOTE_SMS_SEND'].assert_not_called()
        self.assertTrue(all(m['status'] == 'cancelled' for m in self.messages()))

    def test_provider_failure_never_becomes_fake_success_or_auto_retries(self):
        identifier = self.create()
        send = Mock(side_effect=TimeoutError('provider timed out'))
        self.app.config['QUOTE_EMAIL_SEND'] = send
        self.assertEqual(self.queue_manual(identifier).status_code, 201)
        self.process()
        message = self.messages()[0]
        self.assertIn(message['status'], ('unknown', 'failed'))
        self.assertFalse(message['provider_id'])
        self.assertTrue(message['error'])
        self.process()
        send.assert_called_once()
        self.assertEqual(self.send(self.a, 'POST', f"/api/messages/{message['id']}/retry", {}).status_code, 409)

    def test_scheduler_dedupe_survives_application_restart(self):
        self.create()
        self.settings(owner_email_enabled=True)
        calls = []
        self.app.config['QUOTE_EMAIL_SEND'] = lambda message: calls.append(message) or 'provider-001'
        self.process()
        self.assertEqual(len(calls), 1)
        restored = create_app(self.path, testing=True)
        restored.config['QUOTE_EMAIL_SEND'] = lambda message: calls.append(message) or 'provider-002'
        from automations import process_automations
        process_automations(restored, now=datetime(2026, 10, 23, tzinfo=timezone.utc))
        self.assertEqual(len(calls), 1, 'An application restart must not resend old due-date reminders')

    def test_background_worker_dispatches_without_browser_requests(self):
        from automations import start_worker
        self.create()
        self.settings(owner_email_enabled=True)
        accepted = threading.Event()
        self.app.config['QUOTE_EMAIL_SEND'] = lambda message: accepted.set() or 'worker-email-001'
        self.app.config['AUTOMATIONS_NOW'] = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.app.testing = False
        stopped = start_worker(self.app)
        try:
            self.assertTrue(accepted.wait(5), 'Worker must process due reminders without a browser request')
        finally:
            stopped.set()
            thread = self.app.extensions['automation_worker']['thread']
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.app.testing = True
        self.assertEqual(self.messages()[0]['status'], 'accepted')

    def test_interrupted_sending_becomes_unknown_without_resend(self):
        identifier = self.create()
        queued = self.queue_manual(identifier)
        self.assertEqual(queued.status_code, 201)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE messages SET status='sending',updated_at='2026-10-21T00:00:00+00:00'")
        sender = Mock(return_value='unexpected')
        self.app.config['QUOTE_EMAIL_SEND'] = sender
        self.process()
        sender.assert_not_called()
        message = self.messages()[0]
        self.assertEqual(message['status'], 'unknown')
        self.assertTrue(message['error'])

    def test_manual_message_checks_destination_and_content(self):
        identifier = self.create()
        for changes in [dict(channel='fax'), dict(subject=''), dict(subject='Hello\nInjected'),
                        dict(body=''), dict(body='x'*5001), dict(request_id='short')]:
            with self.subTest(changes=next(iter(changes))):
                self.assertEqual(self.queue_manual(identifier, **changes).status_code, 400)
        self.update(identifier, sms_consent=False, phone='0412345678')
        self.assertEqual(self.queue_manual(identifier, 'sms').status_code, 400)

    def test_draft_fallback_ai_and_review_before_sending(self):
        identifier = self.create()
        path = '/api/enquiries/' + identifier + '/draft'
        fallback = self.send(self.a, 'POST', path, dict(channel='email', purpose='followup'))
        self.assertEqual(fallback.status_code, 200, fallback.json)
        self.assertEqual(fallback.json['source'], 'template')
        self.assertTrue(fallback.json['body'])
        self.assertEqual(self.messages(), [], 'Drafting alone must not queue or send')
        self.settings(ai_enabled=True)
        mock = Mock(return_value=dict(subject='A considered follow-up', body='Hello, can we help with your quote?'))
        self.app.config['QUOTE_AI_DRAFT'] = mock
        drafted = self.send(self.a, 'POST', path, dict(channel='email', purpose='followup'))
        self.assertEqual(drafted.status_code, 200, drafted.json)
        self.assertEqual(drafted.json['source'], 'ai')
        self.assertEqual(drafted.json['subject'], 'A considered follow-up')
        self.assertEqual(self.messages(), [])
        context = mock.call_args.args[0]
        self.assertNotIn('email', context)
        self.assertNotIn('phone', context)
        mock.side_effect = TimeoutError('AI unavailable')
        failed = self.send(self.a, 'POST', path, dict(channel='sms', purpose='quote'))
        self.assertEqual(failed.status_code, 200, failed.json)
        self.assertEqual(failed.json['source'], 'template')
        self.assertTrue(failed.json.get('warning'))
        for payload in [dict(channel='fax', purpose='quote'), dict(channel='email', purpose='invent-an-offer')]:
            self.assertEqual(self.send(self.a, 'POST', path, payload).status_code, 400)


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'QUOTE_EMAIL_API_KEY': 'test-email-key', 'QUOTE_EMAIL_FROM': 'Office <office@example.com>',
            'QUOTE_SMS_ACCOUNT_SID': 'AC' + '1' * 32, 'QUOTE_SMS_AUTH_TOKEN': 'test-sms-token',
            'QUOTE_SMS_FROM': '+61400000000', 'QUOTE_AI_API_KEY': 'test-ai-key',
            'QUOTE_AI_MODEL': 'test-model',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.message = dict(id='message-001', recipient='customer@example.com',
                            subject='Your quote', body='Hello, following up on your quote.')
        guard = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Live network is forbidden'))
        network = guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(network.assert_not_called)

    def test_resend_https_payload_and_idempotency(self):
        from automations import send_email
        with patch('automations.urlopen', return_value=io.BytesIO(b'{"id":"resend-001"}')) as post:
            self.assertEqual(send_email(self.message), 'resend-001')
        req = post.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.resend.com/emails')
        self.assertEqual(req.get_method(), 'POST')
        payload = json.loads(req.data)
        self.assertEqual(payload['to'], ['customer@example.com'])
        self.assertEqual(payload['subject'], 'Your quote')
        self.assertEqual(payload['text'], self.message['body'])
        self.assertEqual(req.get_header('Idempotency-key'), 'quotedesk-message-001')
        self.assertEqual(post.call_args.kwargs, {'timeout': 25})

    def test_twilio_https_form_payload(self):
        from automations import send_sms
        message = dict(self.message, recipient='+61412345678')
        with patch('automations.urlopen', return_value=io.BytesIO(b'{"sid":"SM001"}')) as post:
            self.assertEqual(send_sms(message), 'SM001')
        req = post.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.twilio.com/2010-04-01/Accounts/AC' + '1' * 32 + '/Messages.json')
        self.assertEqual(parse_qs(req.data.decode()), {'From': ['+61400000000'], 'To': ['+61412345678'], 'Body': [self.message['body']]})
        self.assertTrue(req.get_header('Authorization').startswith('Basic '))

    def test_invalid_sid_and_missing_acceptance_are_not_success(self):
        from automations import DeliveryError, send_email, send_sms
        with patch.dict(os.environ, {'QUOTE_SMS_ACCOUNT_SID': 'invalid/account'}), patch('automations.urlopen') as post:
            with self.assertRaises(DeliveryError):
                send_sms(self.message)
            post.assert_not_called()
        for sender in (send_email, send_sms):
            with self.subTest(sender=sender.__name__), patch('automations.urlopen', return_value=io.BytesIO(b'{}')):
                with self.assertRaises(DeliveryError) as result:
                    sender(self.message)
                self.assertTrue(result.exception.uncertain)

    def test_provider_errors_are_sanitized_and_uncertainty_preserved(self):
        from automations import DeliveryError, send_email
        for code in (401, 429, 500):
            with self.subTest(code=code), patch('automations.urlopen', side_effect=HTTPError('https://api.resend.com/emails', code, 'error', {}, io.BytesIO(b'private-token-private-customer'))):
                with self.assertRaises(DeliveryError) as result:
                    send_email(self.message)
                self.assertNotIn('private-token', str(result.exception))
                self.assertEqual(result.exception.uncertain, code >= 500)
        for failure in (TimeoutError('secret details'), URLError('secret details')):
            with self.subTest(failure=type(failure).__name__), patch('automations.urlopen', side_effect=failure):
                with self.assertRaises(DeliveryError) as result:
                    send_email(self.message)
                self.assertTrue(result.exception.uncertain)
                self.assertNotIn('secret details', str(result.exception))

    def test_openai_request_limits_fields_and_uses_structured_response(self):
        from automations import _draft
        entry = dict(name='Customer One', email='private@example.com', phone='+61412345678',
                     service='Plumbing', job='Repair tap', status='Quoted', amount='450.00')
        result = {'choices': [{'message': {'content': json.dumps({'subject': 'A quick follow-up', 'body': 'Do you have any questions about your quote?'})}}]}
        with patch('automations.urlopen', return_value=io.BytesIO(json.dumps(result).encode())) as post:
            drafted = _draft(SimpleNamespace(config={}), entry, 'Test Plumbing', 'email', 'followup', True)
        self.assertEqual(drafted['source'], 'ai')
        req = post.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.openai.com/v1/chat/completions')
        payload = json.loads(req.data)
        self.assertEqual(payload['model'], 'test-model')
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertNotIn('private@example.com', req.data.decode())
        self.assertNotIn('+61412345678', req.data.decode())
        self.assertTrue(payload['max_tokens'] <= 1000)


if __name__ == '__main__':
    unittest.main()
