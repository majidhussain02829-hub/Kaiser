"""Exercise free-host WSGI setup without a hosting account or live API calls."""
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


class FreeHostingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = Path(__file__).resolve().parent
        (self.home / 'Kaiser').symlink_to(self.root, target_is_directory=True)
        self.config = self.home / '.config' / 'quote-desk' / 'environment.json'
        self.config.parent.mkdir(parents=True, mode=0o700)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ('QUOTE_DESK_ROOT', 'QUOTE_DESK_DATA', 'QUOTE_SCHEDULER_TOKEN'):
            os.environ.pop(name, None)

    def load(self):
        with patch.object(Path, 'home', return_value=self.home):
            return runpy.run_path(str(self.root / 'deploy' / 'pythonanywhere_wsgi.py'))['application']

    def test_free_wsgi_uses_persistent_private_storage_and_secure_cookies(self):
        self.config.write_text(json.dumps({'QUOTE_SCHEDULER_TOKEN': 'a' * 64}))
        self.config.chmod(0o600)
        app = self.load()
        self.assertEqual(app.test_client().get('/api/health').status_code, 200)
        self.assertTrue((self.home / '.local/share/quote-desk/quotedesk.sqlite3').exists())
        self.assertTrue(app.config['SESSION_COOKIE_SECURE'])
        self.assertNotIn('automation_worker', app.extensions)

    def test_wsgi_rejects_readable_or_unexpected_private_configuration(self):
        self.config.write_text('{}')
        self.config.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, '0600'):
            self.load()
        self.config.write_text(json.dumps({'PATH': 'untrusted'}))
        self.config.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, 'unsupported fields'):
            self.load()

    def test_paid_blueprint_is_not_the_default(self):
        self.assertFalse((self.root / 'render.yaml').exists())
        self.assertTrue((self.root / 'deploy/render-paid.yaml').exists())


if __name__ == '__main__':
    unittest.main()
