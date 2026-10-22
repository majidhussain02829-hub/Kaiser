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


if __name__ == '__main__':
    unittest.main()
