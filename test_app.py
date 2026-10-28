import tempfile
import unittest
from pathlib import Path
from app import create_app


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.app = create_app(self.path, testing=True)
        self.a = self.app.test_client()
        self.b = self.app.test_client()
        self.payload = dict(name='Customer', email='customer@example.com', phone='', suburb='Gillen', service='Plumbing', job='Fix tap', status='Quoted', amount='120.50', followup='2026-12-01')

    def tearDown(self):
        self.temp.cleanup()

    def send(self, client, method, path, data):
        csrf = client.get('/api/session').json['csrf']
        return client.open(path, method=method, json=data, headers={'X-CSRF-Token': csrf})

    def register(self, client, email='owner@example.com'):
        r = self.send(client, 'POST', '/api/register', dict(email=email, password='long-test-password', business='Test Business'))
        self.assertEqual(r.status_code, 201)
        return r.json['user']

    def test_authentication_csrf_and_logout(self):
        self.assertEqual(self.a.get('/api/enquiries').status_code, 401)
        self.assertEqual(self.a.post('/api/register', json={}).status_code, 403)
        self.register(self.a)
        self.assertEqual(self.send(self.a, 'POST', '/api/logout', {}).status_code, 200)
        self.assertEqual(self.a.get('/api/enquiries').status_code, 401)
        self.assertEqual(self.send(self.a, 'POST', '/api/login', dict(email='owner@example.com', password='wrong')).status_code, 401)
        self.assertEqual(self.send(self.a, 'POST', '/api/login', dict(email='owner@example.com', password='long-test-password')).status_code, 200)

    def test_crud_isolation_concurrency_and_persistence(self):
        self.register(self.a)
        self.register(self.b, 'other@example.com')
        r = self.send(self.a, 'POST', '/api/enquiries', self.payload)
        self.assertEqual(r.status_code, 201)
        path = '/api/enquiries/' + r.json['id']
        self.assertEqual(self.b.get('/api/enquiries').json['entries'], [])
        self.assertEqual(self.send(self.b, 'DELETE', path, {'version': 1}).status_code, 409)
        changed = dict(self.payload, amount='250.00', version=1)
        self.assertEqual(self.send(self.a, 'PUT', path, changed).status_code, 200)
        self.assertEqual(self.send(self.a, 'PUT', path, changed).status_code, 409)
        new_client = create_app(self.path, testing=True).test_client()
        self.send(new_client, 'POST', '/api/login', dict(email='owner@example.com', password='long-test-password'))
        self.assertEqual(new_client.get('/api/enquiries').json['entries'][0]['amount'], '250.00')
        self.assertEqual(self.send(self.a, 'DELETE', path, {'version': 2}).status_code, 200)
        self.assertEqual(self.a.get('/api/enquiries').json['entries'], [])

    def test_public_submission_and_private_data(self):
        owner = self.register(self.a)
        path = '/api/public/' + owner['public_id']
        self.assertEqual(self.b.get(path).json['business'], 'Test Business')
        self.assertEqual(self.send(self.b, 'POST', path, self.payload).status_code, 201)
        entry = self.a.get('/api/enquiries').json['entries'][0]
        self.assertEqual(entry['status'], 'New')
        self.assertEqual(entry['amount'], '')
        self.assertEqual(self.b.get('/api/enquiries').status_code, 401)
        self.assertEqual(self.send(self.b, 'POST', '/api/public/unknown', self.payload).status_code, 404)

    def test_validation(self):
        self.register(self.a)
        for field, value in [('amount','NaN'), ('amount','-1'), ('amount','1.234'), ('followup','2026-02-30'), ('service','invalid'), ('name',''), ('email','invalid'), ('job','x'*2001)]:
            with self.subTest(field=field, value=value[:25]):
                self.assertEqual(self.send(self.a, 'POST', '/api/enquiries', dict(self.payload, **{field:value})).status_code, 400)
        self.assertEqual(self.send(self.a, 'POST', '/api/enquiries', []).status_code, 400)

    def test_throttle(self):
        owner = self.register(self.a)
        path = '/api/public/' + owner['public_id']
        for _ in range(10):
            self.assertEqual(self.send(self.b, 'POST', path, self.payload).status_code, 201)
        self.assertEqual(self.send(self.b, 'POST', path, self.payload).status_code, 429)


    def test_business_profile_updates_public_form_and_preserves_workspace(self):
        owner = self.register(self.a)
        other = self.register(self.b, 'other@example.com')
        self.send(self.a, 'POST', '/api/enquiries', self.payload)
        result = self.send(self.a, 'PUT', '/api/account', {'business': 'Renamed Trades', 'email': 'ignored@example.com'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['user']['email'], 'owner@example.com')
        self.assertEqual(result.json['user']['public_id'], owner['public_id'])
        visitor = self.app.test_client()
        self.assertEqual(visitor.get('/api/public/' + owner['public_id']).json['business'], 'Renamed Trades')
        self.assertEqual(visitor.get('/api/public/' + other['public_id']).json['business'], 'Test Business')
        self.assertEqual(len(self.a.get('/api/enquiries').json['entries']), 1)
        for value in ('', ' ', 'x' * 101, None):
            self.assertEqual(self.send(self.a, 'PUT', '/api/account', {'business': value}).status_code, 400)
        self.assertEqual(self.send(visitor, 'PUT', '/api/account', {'business': 'Other'}).status_code, 401)

    def test_password_change_revokes_other_sessions_only_for_this_account(self):
        self.register(self.a)
        self.register(self.b, 'other@example.com')
        old_device = self.app.test_client()
        self.send(old_device, 'POST', '/api/login', {'email': 'owner@example.com', 'password': 'long-test-password'})
        old_csrf = old_device.get('/api/session').json['csrf']
        result = self.send(self.a, 'POST', '/api/account/password', {
            'current_password': 'long-test-password', 'new_password': 'replacement-password',
            'confirm_password': 'replacement-password'})
        self.assertEqual(result.status_code, 200)
        self.assertNotEqual(result.json['csrf'], old_csrf)
        self.assertEqual(self.a.get('/api/enquiries').status_code, 200)
        self.assertEqual(self.b.get('/api/enquiries').status_code, 200)
        self.assertEqual(old_device.get('/api/enquiries').status_code, 401)
        self.assertEqual(old_device.get('/api/automations').status_code, 401)
        self.assertEqual(old_device.put('/api/account', json={'business': 'Stolen'}, headers={'X-CSRF-Token': old_csrf}).status_code, 401)
        self.assertIsNone(old_device.get('/api/session').json['user'])
        credentials = {'email': 'owner@example.com', 'password': 'long-test-password'}
        self.assertEqual(self.send(old_device, 'POST', '/api/login', credentials).status_code, 401)
        credentials['password'] = 'replacement-password'
        self.assertEqual(self.send(old_device, 'POST', '/api/login', credentials).status_code, 200)
        restarted = create_app(self.path, testing=True).test_client()
        self.assertEqual(self.send(restarted, 'POST', '/api/login', credentials).status_code, 200)
        self.assertEqual(restarted.get('/api/enquiries').status_code, 200)

    def test_password_validation_csrf_and_throttle(self):
        self.register(self.a)
        payload = {'current_password': 'long-test-password', 'new_password': 'replacement-password', 'confirm_password': 'replacement-password'}
        self.assertEqual(self.a.post('/api/account/password', json=payload).status_code, 403)
        for changes in ({'current_password': 'wrong'}, {'new_password': 'short'},
                        {'confirm_password': 'does-not-match'}, {'new_password': 'long-test-password', 'confirm_password': 'long-test-password'},
                        {'new_password': 'x' * 257}, {'new_password': None}):
            self.assertEqual(self.send(self.a, 'POST', '/api/account/password', dict(payload, **changes)).status_code, 400)
        for _ in range(4):
            self.assertEqual(self.send(self.a, 'POST', '/api/account/password', dict(payload, current_password='wrong')).status_code, 400)
        self.assertEqual(self.send(self.a, 'POST', '/api/account/password', payload).status_code, 429)
        self.assertIsNotNone(self.a.get('/api/session').json['user'])

    def test_existing_database_upgrade_preserves_accounts(self):
        import sqlite3
        self.register(self.a)
        with sqlite3.connect(self.path) as conn:
            conn.execute('ALTER TABLE users DROP COLUMN session_version')
        upgraded = create_app(self.path, testing=True).test_client()
        result = self.send(upgraded, 'POST', '/api/login', {'email': 'owner@example.com', 'password': 'long-test-password'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['user']['business'], 'Test Business')


if __name__ == '__main__':
    unittest.main()
