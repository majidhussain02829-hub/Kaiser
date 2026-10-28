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

    def test_quote_preview_is_private_and_excludes_internal_fields(self):
        self.register(self.a)
        self.register(self.b, 'other@example.com')
        payload = dict(self.payload, job='PRIVATE NOTE: supplier margin 40%',
                       quote_scope='Replace tap <script>alert(1)</script>',
                       quote_terms='Valid for 14 days. Total includes applicable tax.')
        identifier = self.send(self.a, 'POST', '/api/enquiries', payload).json['id']
        path = '/quotes/' + identifier
        self.assertEqual(self.app.test_client().get(path).status_code, 401)
        self.assertEqual(self.b.get(path).status_code, 404)
        response = self.a.get(path)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for expected in ('A$120.50', 'Test Business', 'Valid for 14 days',
                         '&lt;script&gt;alert(1)&lt;/script&gt;', 'Revision 1'):
            self.assertIn(expected, html)
        for private in ('PRIVATE NOTE', '2026-12-01', '<script>alert(1)</script>'):
            self.assertNotIn(private, html)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.send(self.a, 'DELETE', '/api/enquiries/' + identifier, {'version': 1})
        self.assertEqual(self.a.get(path).status_code, 404)

    def test_quote_requires_saved_scope_and_amount_and_allows_zero(self):
        self.register(self.a)
        identifier = self.send(self.a, 'POST', '/api/enquiries', self.payload).json['id']
        path = '/quotes/' + identifier
        self.assertEqual(self.a.get(path).status_code, 400)
        for version, amount, code in [(1, '', 400), (2, '0', 200)]:
            result = self.send(self.a, 'PUT', '/api/enquiries/' + identifier,
                               dict(self.payload, version=version, amount=amount,
                                    quote_scope='Inspection only'))
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.a.get(path).status_code, code)
        self.assertIn('A$0.00', self.a.get(path).get_data(as_text=True))

    def test_public_submit_cannot_supply_quote_content(self):
        owner = self.register(self.a)
        self.assertEqual(self.send(self.b, 'POST', '/api/public/' + owner['public_id'],
                                   dict(self.payload, quote_scope='Fake offer', quote_terms='Free')).status_code, 201)
        entry = self.a.get('/api/enquiries').json['entries'][0]
        self.assertEqual(entry['quote_scope'], '')
        self.assertEqual(entry['quote_terms'], '')

    def test_quote_fields_validate_and_survive_restart(self):
        self.register(self.a)
        for field, value in [('quote_scope', 'x' * 4001), ('quote_terms', 'x' * 2001),
                             ('quote_scope', 42), ('quote_terms', None)]:
            self.assertEqual(self.send(self.a, 'POST', '/api/enquiries',
                                       dict(self.payload, **{field: value})).status_code, 400)
        self.send(self.a, 'POST', '/api/enquiries', dict(self.payload, quote_scope='Customer scope', quote_terms='Terms'))
        client = create_app(self.path, testing=True).test_client()
        self.send(client, 'POST', '/api/login', dict(email='owner@example.com', password='long-test-password'))
        entry = client.get('/api/enquiries').json['entries'][0]
        self.assertEqual(entry['quote_scope'], 'Customer scope')
        self.assertEqual(entry['quote_terms'], 'Terms')

    def test_existing_database_gains_quote_fields_without_losing_records(self):
        import sqlite3
        self.register(self.a)
        self.send(self.a, 'POST', '/api/enquiries', self.payload)
        with sqlite3.connect(self.path) as conn:
            conn.execute('ALTER TABLE enquiries DROP COLUMN quote_scope')
            conn.execute('ALTER TABLE enquiries DROP COLUMN quote_terms')
        client = create_app(self.path, testing=True).test_client()
        self.send(client, 'POST', '/api/login', dict(email='owner@example.com', password='long-test-password'))
        entry = client.get('/api/enquiries').json['entries'][0]
        self.assertEqual(entry['name'], 'Customer')
        self.assertEqual(entry['amount'], '120.50')
        self.assertEqual(entry['quote_scope'], '')
        self.assertEqual(entry['quote_terms'], '')

    def test_throttle(self):
        owner = self.register(self.a)
        path = '/api/public/' + owner['public_id']
        for _ in range(10):
            self.assertEqual(self.send(self.b, 'POST', path, self.payload).status_code, 201)
        self.assertEqual(self.send(self.b, 'POST', path, self.payload).status_code, 429)


if __name__ == '__main__':
    unittest.main()
