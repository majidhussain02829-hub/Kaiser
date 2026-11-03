"""Quote Desk: authenticated business workspaces and persistent enquiries."""
import argparse
import hashlib
import io
import json
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
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
from xml.sax.saxutils import escape

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
        CREATE TABLE IF NOT EXISTS quotes (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          enquiry_id TEXT NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
          quote_number TEXT NOT NULL UNIQUE, quote_date TEXT NOT NULL,
          valid_until TEXT NOT NULL, items_json TEXT NOT NULL,
          subtotal TEXT NOT NULL, gst TEXT NOT NULL, total TEXT NOT NULL,
          notes TEXT NOT NULL DEFAULT '', terms TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
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

    @app.post('/api/enquiries/<identifier>/quote')
    def create_quote(identifier):
        owner = user()
        d = data()
        enquiry_row = db().execute('SELECT * FROM enquiries WHERE id=? AND user_id=?', (identifier, owner['id'])).fetchone()
        if not enquiry_row:
            abort(404, description='This enquiry is unavailable.')
        raw_items = d.get('items')
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 40:
            abort(400, description='Add between 1 and 40 quote items.')
        items = []
        subtotal = Decimal('0.00')
        for raw in raw_items:
            if not isinstance(raw, dict):
                abort(400, description='Each quote item must be an object.')
            description = raw.get('description', '')
            if not isinstance(description, str) or not description.strip() or len(description.strip()) > 180:
                abort(400, description='Each item needs a description of up to 180 characters.')
            try:
                quantity = Decimal(str(raw.get('quantity', '')))
                unit_price = Decimal(str(raw.get('unit_price', '')))
                if (not quantity.is_finite() or quantity <= 0 or quantity > 9999 or
                    not unit_price.is_finite() or unit_price < 0 or unit_price > 999999.99 or
                    quantity.as_tuple().exponent < -2 or unit_price.as_tuple().exponent < -2):
                    raise InvalidOperation
                line_total = (quantity * unit_price).quantize(Decimal('.01'))
            except (InvalidOperation, ValueError, TypeError):
                abort(400, description='Enter valid quantities and unit prices with up to two decimal places.')
            items.append({'description': description.strip(), 'quantity': str(quantity),
                          'unit_price': str(unit_price.quantize(Decimal('.01'))), 'line_total': str(line_total)})
            subtotal += line_total
        subtotal = subtotal.quantize(Decimal('.01'))
        gst_enabled = d.get('gst', True)
        if not isinstance(gst_enabled, bool):
            abort(400, description='GST must be true or false.')
        gst = (subtotal * Decimal('.10')).quantize(Decimal('.01')) if gst_enabled else Decimal('0.00')
        total = subtotal + gst
        valid_until = d.get('valid_until', '')
        if not isinstance(valid_until, str):
            abort(400, description='Enter a valid quote expiry date.')
        try:
            valid_until = date.fromisoformat(valid_until).isoformat()
            if date.fromisoformat(valid_until) < date.today():
                abort(400, description='The quote expiry date cannot be in the past.')
        except ValueError:
            abort(400, description='Enter a valid quote expiry date.')
        notes = d.get('notes', '')
        terms = d.get('terms', '')
        if not isinstance(notes, str) or len(notes) > 1500 or not isinstance(terms, str) or len(terms) > 1500:
            abort(400, description='Notes and terms must each be 1,500 characters or fewer.')
        quote_id = secrets.token_hex(12)
        quote_number = 'QD-' + date.today().strftime('%Y%m%d') + '-' + secrets.token_hex(3).upper()
        with db():
            db().execute('INSERT INTO quotes (id,user_id,enquiry_id,quote_number,quote_date,valid_until,items_json,subtotal,gst,total,notes,terms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                         (quote_id, owner['id'], identifier, quote_number, date.today().isoformat(), valid_until,
                          json.dumps(items), str(subtotal), str(gst), str(total), notes.strip(), terms.strip()))
            db().execute("UPDATE enquiries SET amount=?,status='Quoted',version=version+1 WHERE id=? AND user_id=?", (str(total), identifier, owner['id']))
        return jsonify(id=quote_id, quote_number=quote_number, subtotal=str(subtotal), gst=str(gst), total=str(total), pdf_url='/api/quotes/'+quote_id+'.pdf'), 201

    @app.get('/api/quotes/<quote_id>.pdf')
    def quote_pdf(quote_id):
        owner = user()
        row = db().execute('SELECT q.*,e.name AS customer_name,e.email AS customer_email,e.phone AS customer_phone,e.suburb,e.service,e.job,u.business,u.email AS business_email FROM quotes q JOIN enquiries e ON e.id=q.enquiry_id JOIN users u ON u.id=q.user_id WHERE q.id=? AND q.user_id=?', (quote_id, owner['id'])).fetchone()
        if not row:
            abort(404, description='This quote is unavailable.')
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=20*mm, leftMargin=20*mm, topMargin=19*mm, bottomMargin=18*mm,
                                title='Quote '+row['quote_number'], author=row['business'])
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name='Brand', parent=styles['Title'], fontName='Helvetica-Bold', fontSize=23, leading=27, textColor=colors.HexColor('#174734'), alignment=0, spaceAfter=3))
        styles.add(ParagraphStyle(name='SmallMuted', parent=styles['Normal'], fontSize=8.5, leading=12, textColor=colors.HexColor('#66776d')))
        styles.add(ParagraphStyle(name='SectionQD', parent=styles['Heading2'], fontSize=10, leading=13, textColor=colors.HexColor('#174734'), spaceBefore=10, spaceAfter=5))
        styles.add(ParagraphStyle(name='RightQD', parent=styles['Normal'], fontSize=9, leading=13, alignment=TA_RIGHT))
        money_fmt = lambda value: chr(36) + format(Decimal(value), ',.2f')
        story = [Paragraph(escape(row['business']), styles['Brand']),
                 Paragraph(escape(row['business_email']), styles['SmallMuted']), Spacer(1, 12),
                 Paragraph('QUOTE', styles['Heading1'])]
        meta = [[Paragraph('<b>Quote number</b><br/>'+escape(row['quote_number']), styles['Normal']),
                 Paragraph('<b>Issued</b><br/>'+escape(row['quote_date']), styles['Normal']),
                 Paragraph('<b>Valid until</b><br/>'+escape(row['valid_until']), styles['Normal'])]]
        meta_table = Table(meta, colWidths=[58*mm, 48*mm, 48*mm])
        meta_table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#edf3ed')),('BOX',(0,0),(-1,-1),.5,colors.HexColor('#dce6dc')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),9),('RIGHTPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,-1),9),('BOTTOMPADDING',(0,0),(-1,-1),9)]))
        story += [meta_table, Paragraph('PREPARED FOR', styles['SectionQD']),
                  Paragraph('<b>'+escape(row['customer_name'])+'</b><br/>'+escape(row['customer_email'] or '')+'<br/>'+escape(row['customer_phone'] or '')+'<br/>'+escape(row['suburb']), styles['Normal']),
                  Paragraph('JOB DESCRIPTION', styles['SectionQD']), Paragraph(escape(row['job']).replace('\\n','<br/>'), styles['Normal']), Spacer(1, 12)]
        data_rows = [[Paragraph('<b>Description</b>', styles['Normal']), Paragraph('<b>Qty</b>', styles['RightQD']), Paragraph('<b>Unit price</b>', styles['RightQD']), Paragraph('<b>Amount</b>', styles['RightQD'])]]
        for item in json.loads(row['items_json']):
            data_rows.append([Paragraph(escape(item['description']).replace('\\n','<br/>'), styles['Normal']),
                              Paragraph(escape(item['quantity']), styles['RightQD']),
                              Paragraph(money_fmt(item['unit_price']), styles['RightQD']),
                              Paragraph(money_fmt(item['line_total']), styles['RightQD'])])
        quote_table = Table(data_rows, colWidths=[83*mm, 18*mm, 34*mm, 34*mm], repeatRows=1)
        quote_table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#174734')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),.35,colors.HexColor('#dfe7df')),('VALIGN',(0,0),(-1,-1),'TOP'),('ALIGN',(1,0),(-1,-1),'RIGHT'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f7f9f6')])]))
        story += [quote_table, Spacer(1, 8)]
        totals = [['Subtotal', money_fmt(row['subtotal'])], ['GST (10%)', money_fmt(row['gst'])], ['TOTAL (AUD)', money_fmt(row['total'])]]
        total_table = Table(totals, colWidths=[42*mm, 35*mm], hAlign='RIGHT')
        total_table.setStyle(TableStyle([('ALIGN',(1,0),(-1,-1),'RIGHT'),('FONTNAME',(0,0),(-1,-2),'Helvetica'),('FONTNAME',(0,-1),(-1,-1),'Helvetica-Bold'),('FONTSIZE',(0,-1),(-1,-1),12),('TEXTCOLOR',(0,-1),(-1,-1),colors.HexColor('#174734')),('LINEABOVE',(0,-1),(-1,-1),1,colors.HexColor('#174734')),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
        story += [total_table]
        if row['notes'].strip():
            story += [Paragraph('NOTES', styles['SectionQD']), Paragraph(escape(row['notes']).replace('\\n','<br/>'), styles['Normal'])]
        if row['terms'].strip():
            story += [Paragraph('TERMS &amp; CONDITIONS', styles['SectionQD']), Paragraph(escape(row['terms']).replace('\\n','<br/>'), styles['Normal'])]
        story += [Spacer(1, 16), Paragraph('Thank you for the opportunity to quote on this work.', styles['SmallMuted'])]
        doc.build(story)
        buffer.seek(0)
        return send_file(buffer, mimetype='application/pdf', as_attachment=True,
                         download_name='Quote-'+row['quote_number']+'.pdf')

    @app.get('/api/public/<public_id>')
