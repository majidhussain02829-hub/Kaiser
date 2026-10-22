"""Paste this into the WSGI configuration of a free PythonAnywhere web app.

Checkout: ~/Kaiser. Virtualenv: ~/Kaiser/.venv (select it in the Web tab).
Optional credentials: ~/.config/quote-desk/environment.json, mode 0600.
"""
import json
import os
from pathlib import Path
import sys

home = Path.home()
repository = Path(os.environ.get('QUOTE_DESK_ROOT', home / 'Kaiser'))
sys.path.insert(0, str(repository))

private_config = home / '.config' / 'quote-desk' / 'environment.json'
allowed = {
    'QUOTE_EMAIL_API_KEY', 'QUOTE_EMAIL_FROM', 'QUOTE_SMS_ACCOUNT_SID',
    'QUOTE_SMS_AUTH_TOKEN', 'QUOTE_SMS_FROM', 'QUOTE_AI_API_KEY',
    'QUOTE_AI_MODEL', 'QUOTE_SCHEDULER_TOKEN',
}
if private_config.exists():
    if private_config.stat().st_mode & 0o077:
        raise RuntimeError('Private configuration must have mode 0600.')
    settings = json.loads(private_config.read_text())
    if not isinstance(settings, dict) or set(settings) - allowed:
        raise RuntimeError('Private configuration contains unsupported fields.')
    for name, value in settings.items():
        if not isinstance(value, str):
            raise RuntimeError('Private configuration values must be strings.')
        os.environ.setdefault(name, value)

os.environ.setdefault('QUOTE_DESK_DATA', str(home / '.local' / 'share' / 'quote-desk'))
os.environ['COOKIE_SECURE'] = '1'

from app import create_app

# Free WSGI workers do not provide reliable background threads. The optional
# authenticated GitHub scheduler invokes the same durable job processing.
application = create_app()
