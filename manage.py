"""Local operator tools. Run only with trusted access to the data directory."""
import argparse
import getpass
import os
import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash

parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest='command', required=True)
backup = sub.add_parser('backup')
backup.add_argument('destination', type=Path)
reset = sub.add_parser('reset-password')
reset.add_argument('email')
args = parser.parse_args()
root = Path(os.environ.get('QUOTE_DESK_DATA', Path(__file__).parent / 'instance'))
source = root / 'quotedesk.sqlite3'
if not source.exists():
    parser.error('Database not found. Start the app first.')
with sqlite3.connect(source) as conn:
    if args.command == 'backup':
        if args.destination.exists():
            parser.error('Destination already exists; choose a new file.')
        with args.destination.open('xb'):
            os.chmod(args.destination, 0o600)
        with sqlite3.connect(args.destination) as target:
            conn.backup(target)
            assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        print('Verified database backup created.')
    else:
        password = getpass.getpass('New password (at least 12 characters): ')
        if len(password) < 12 or len(password) > 256 or password != getpass.getpass('Confirm password: '):
            parser.error('Passwords must match and contain 12–256 characters.')
        cursor = conn.execute('UPDATE users SET password=? WHERE email=?', (generate_password_hash(password), args.email.lower()))
        if not cursor.rowcount:
            parser.error('Account not found.')
        conn.commit()
        # Invalidate existing signed sessions after operator-assisted recovery.
        (root / 'session.key').write_text(__import__('secrets').token_hex(32))
        print('Password reset. Restart the app to invalidate all existing sessions.')
