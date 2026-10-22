"""Quote Desk: authenticated business workspaces and persistent enquiries."""
import argparse
import hashlib
import os
import re
import secrets
import sqlite3
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parent
STATUSES = {'New', 'Contacted', 'Quoted', 'Won', 'Lost'}
SERVICES = {'Plumbing', 'Electrical', 'Air conditioning', 'Carpentry', 'Landscaping', 'Other'}


def create_app(database=None, testing=False):
    app = Flask(__name__)
    storage = Path(os.environ.get('QUOTE_DESK_DATA', ROOT / 'instance'))
    storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret_file = storage / 'session.key'
    if not secret_file.exists():
        try:
            with secret_file.open('x') as f:
                os.chmod(secret_file, 0o600)
                f.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    app.config.update(SECRET_KEY=secret_file.read_text(), TESTING=testing,
                      MAX_CONTENT_LENGTH=16 * 1024, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == '1',
                      PERMANENT_SESSION_LIFETIME=28800)
    db_path = str(database or storage / 'quotedesk.sqlite3')

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(db_path, timeout=15)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        if 'db' in g:
            g.db.close()

    with app.app_context():
        db().executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
          password TEXT NOT NULL, business TEXT NOT NULL, public_id TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS enquiries (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
          suburb TEXT NOT NULL, service TEXT NOT NULL, job TEXT NOT NULL,
          status TEXT NOT NULL, amount TEXT NOT NULL, followup TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS rate_limits (
          bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires INTEGER NOT NULL);
        ''')
        # Additive upgrades preserve existing businesses and their enquiries.
        columns = {row['name'] for row in db().execute('PRAGMA table_info(enquiries)')}
        for column in ('email_consent', 'sms_consent'):
            if column not in columns:
                db().execute(f'ALTER TABLE enquiries ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')
        db().commit()

    @app.before_request
    def protect():
        # External cron uses its own deployment-scoped Bearer credential.
        # Browser APIs keep their existing session and CSRF protections.
        if request.endpoint == 'automation_tick':
            return
        if request.method in {'POST', 'PUT', 'DELETE'}:
            if request.headers.get('X-CSRF-Token') != session.get('csrf') or not session.get('csrf'):
                abort(403, description='Your session expired. Reload and try again.')
            if not request.is_json:
                abort(415, description='JSON is required.')

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        return response

    @app.errorhandler(Exception)
    def error(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        app.logger.exception('Request failed')
        return jsonify(error='An unexpected error occurred. Please try again.'), 500

    def data():
        value = request.get_json()
        if not isinstance(value, dict):
            abort(400, description='A JSON object is required.')
        return value

    def text(d, key, limit, required=False):
        value = d.get(key, '')
        if not isinstance(value, str):
            abort(400, description=f'Invalid {key}.')
        value = value.strip()
        if len(value) > limit or (required and not value):
            abort(400, description=f'{key.capitalize()} is required and must be at most {limit} characters.')
        return value

    def user():
        row = db().execute('SELECT * FROM users WHERE id=?', (session.get('user_id'),)).fetchone()
        if not row:
            abort(401, description='Please sign in.')
        return row

    def user_info(row):
        return {k: row[k] for k in ('email', 'business', 'public_id')}

    def throttle(bucket, limit, seconds):
        # Address comes from the socket, never an untrusted forwarded header.
        key = hashlib.sha256(f'{bucket}:{request.remote_addr}'.encode()).hexdigest()
        now = int(time.time())
        conn = db()
        with conn:
            conn.execute('DELETE FROM rate_limits WHERE expires<=?', (now,))
            conn.execute('INSERT INTO rate_limits VALUES (?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1', (key, now + seconds))
            count = conn.execute('SELECT count FROM rate_limits WHERE bucket=?', (key,)).fetchone()[0]
        if count > limit:
            abort(429, description='Too many requests. Please try again later.')

    def enquiry(d, public=False):
        result = {k: text(d, k, n, k in {'name', 'suburb', 'job'}) for k, n in
                  [('name',100), ('email',150), ('phone',40), ('suburb',100), ('service',50), ('job',2000), ('status',20), ('amount',20), ('followup',10)]}
        if result['email'] and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', result['email']):
            abort(400, description='Enter a valid email address.')
        for channel in ('email', 'sms'):
            value = d.get(channel + '_consent', False)
            if not isinstance(value, bool):
                abort(400, description='Contact permission must be true or false.')
            result[channel + '_consent'] = int(value)
        if result['email_consent'] and not result['email']:
            abort(400, description='An email address is required for email permission.')
        if result['sms_consent'] and not re.fullmatch(r'\+[1-9]\d{7,14}', result['phone']):
            abort(400, description='SMS requires an international number such as +61412345678.')
        if public:
            if not result['email']:
                abort(400, description='Email is required.')
            result.update(status='New', amount='', followup='')
        if result['service'] not in SERVICES or result['status'] not in STATUSES:
            abort(400, description='Choose a valid service and status.')
        if result['amount']:
            try:
                amount = Decimal(result['amount'])
                if not amount.is_finite() or amount < 0 or amount > 99999999 or amount.as_tuple().exponent < -2:
                    raise InvalidOperation
                result['amount'] = str(amount.quantize(Decimal('.01')))
            except InvalidOperation:
                abort(400, description='Enter a valid quote amount with at most two decimal places.')
        if result['followup']:
            try:
                date.fromisoformat(result['followup'])
            except ValueError:
                abort(400, description='Enter a valid follow-up date.')
        return result

    def insert(owner, entry):
        identifier = secrets.token_hex(16)
        with db():
            db().execute('INSERT INTO enquiries (id,user_id,' + ','.join(entry) + ') VALUES (' + ','.join('?' for _ in range(len(entry)+2)) + ')', (identifier, owner, *entry.values()))
        return identifier

    @app.get('/')
    @app.get('/request/<public_id>')
    def index(public_id=None):
        return send_file(ROOT / 'index.html')

    @app.get('/api/session')
    def current_session():
        session.setdefault('csrf', secrets.token_hex(32))
        row = db().execute('SELECT * FROM users WHERE id=?', (session.get('user_id'),)).fetchone()
        return jsonify(csrf=session['csrf'], user=user_info(row) if row else None)

    @app.post('/api/register')
    def register():
        throttle('register', 10, 3600)
        d = data()
        email = text(d, 'email', 150, True).lower()
        password = text(d, 'password', 256, True)
        business = text(d, 'business', 100, True)
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(password) < 12:
            abort(400, description='Enter a valid email and a password of at least 12 characters.')
        try:
            with db():
                cursor = db().execute('INSERT INTO users(email,password,business,public_id) VALUES (?,?,?,?)', (email, generate_password_hash(password), business, secrets.token_urlsafe(18)))
        except sqlite3.IntegrityError:
            abort(409, description='Unable to create this account. Try signing in.')
        session.clear()
        session.update(user_id=cursor.lastrowid, csrf=secrets.token_hex(32))
        session.permanent = True
        return jsonify(user=user_info(user()), csrf=session['csrf']), 201

    @app.post('/api/login')
    def login():
        throttle('login', 20, 900)
        d = data()
        row = db().execute('SELECT * FROM users WHERE email=?', (text(d, 'email', 150).lower(),)).fetchone()
        password = text(d, 'password', 256)
        # Perform a hash check for unknown accounts too.
        fallback = generate_password_hash('unused-password') if row is None else row['password']
        valid = check_password_hash(fallback, password)
        if not row or not valid:
            abort(401, description='Email or password is incorrect.')
        session.clear()
        session.update(user_id=row['id'], csrf=secrets.token_hex(32))
        session.permanent = True
        return jsonify(user=user_info(row), csrf=session['csrf'])

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get('/api/enquiries')
    def listing():
        entries = []
        for row in db().execute('SELECT * FROM enquiries WHERE user_id=? ORDER BY created_at DESC,id DESC', (user()['id'],)):
            entry = dict(row)
            entry.update(email_consent=bool(entry['email_consent']), sms_consent=bool(entry['sms_consent']))
            entries.append(entry)
        return jsonify(entries=entries)

    @app.post('/api/enquiries')
    def create():
        owner = user()['id']
        identifier = insert(owner, enquiry(data()))
        return jsonify(id=identifier), 201

    @app.route('/api/enquiries/<identifier>', methods=['PUT', 'DELETE'])
    def change(identifier):
        owner = user()['id']
        d = data()
        version = d.get('version')
        if not isinstance(version, int):
            abort(400, description='A record version is required. Refresh and try again.')
        with db():
            if request.method == 'DELETE':
                cursor = db().execute('DELETE FROM enquiries WHERE id=? AND user_id=? AND version=?', (identifier, owner, version))
            else:
                entry = enquiry(d)
                cursor = db().execute('UPDATE enquiries SET ' + ','.join(k+'=?' for k in entry) + ',version=version+1 WHERE id=? AND user_id=? AND version=?', (*entry.values(), identifier, owner, version))
        if not cursor.rowcount:
            abort(409, description='This enquiry changed or is unavailable. Refresh before trying again.')
        return jsonify(ok=True)

    @app.get('/api/public/<public_id>')
    def public_info(public_id):
        row = db().execute('SELECT business FROM users WHERE public_id=?', (public_id,)).fetchone()
        if not row:
            abort(404, description='This enquiry form is unavailable.')
        session.setdefault('csrf', secrets.token_hex(32))
        return jsonify(business=row['business'], csrf=session['csrf'])

    @app.post('/api/public/<public_id>')
    def public_create(public_id):
        throttle('public', 10, 3600)
        row = db().execute('SELECT id FROM users WHERE public_id=?', (public_id,)).fetchone()
        if not row:
            abort(404, description='This enquiry form is unavailable.')
        insert(row['id'], enquiry(data(), public=True))
        return jsonify(ok=True), 201

    @app.get('/api/health')
    def health():
        db().execute('SELECT 1').fetchone()
        return jsonify(status='ok')

    from automations import register_automations
    register_automations(app, db, user, data, text, throttle)
    from scheduler import register_scheduler
    register_scheduler(app)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    from waitress import serve
    from automations import start_worker
    application = create_app()
    start_worker(application)
    serve(application, host=args.host, port=args.port, threads=4)
