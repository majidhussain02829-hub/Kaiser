"""Deploy Quote Desk to a free PythonAnywhere WSGI web app.

Official API: https://help.pythonanywhere.com/pages/API/
Official upload example: https://github.com/pythonanywhere/upload-website

Only standard v0 webapps/files endpoints are used. No billing, plan, task or
console endpoints are called. Dependencies are bundled for zipimport, so no
remote shell or paid always-on process is needed. Running without --deploy
only prepares files locally; --deploy requires the operator's secure API key.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sys
import tempfile
import time
from importlib import metadata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
API_ORIGINS = {'https://www.pythonanywhere.com', 'https://eu.pythonanywhere.com'}
PACKAGES = {'Flask': 'flask', 'Werkzeug': 'werkzeug', 'Jinja2': 'jinja2',
            'click': 'click', 'blinker': 'blinker', 'itsdangerous': 'itsdangerous',
            'MarkupSafe': 'markupsafe', 'waitress': 'waitress'}
SOURCE_FILES = ('app.py', 'automations.py', 'scheduler.py', 'index.html', 'quote_document.py')
MARKER = '.quote-desk-deployment.json'


class DeploymentError(Exception):
    """Sanitized deployment failure, safe to display without provider bodies."""


class VerificationError(DeploymentError):
    """Files were deployed but the public app has not been verified."""


class _NoRedirect(HTTPRedirectHandler):
    # Never forward an account token to another host, including via redirects.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_account(username, origin):
    origin = origin.rstrip('/')
    if origin not in API_ORIGINS:
        raise DeploymentError('Use the official US or EU PythonAnywhere API origin.')
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,32}', username):
        raise DeploymentError('Set QUOTE_HOST_USERNAME to your PythonAnywhere username.')
    suffix = '.eu.pythonanywhere.com' if origin == 'https://eu.pythonanywhere.com' else '.pythonanywhere.com'
    return origin, username + suffix


def runtime_zip():
    """Bundle only approved installed distributions, with no compiled extensions.

    MarkupSafe includes a supported pure-Python fallback. Explicit directory
    entries are important: Flask's sansio directory is a namespace package.
    No environment, application database or arbitrary site-package is bundled.
    """
    output = io.BytesIO()
    written = set()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for distribution_name, package in PACKAGES.items():
            try:
                distribution = metadata.distribution(distribution_name)
            except metadata.PackageNotFoundError:
                raise DeploymentError('Install requirements.txt and run this helper with .venv/bin/python.') from None
            site = Path(distribution.locate_file('')).resolve()
            package_root = site / package
            if not package_root.is_dir() or package_root.is_symlink():
                raise DeploymentError('An approved dependency could not be bundled safely.')
            roots = [package_root]
            metadata_roots = {Path(distribution.locate_file(item)).parts for item in distribution.files or []
                              if '.dist-info/' in str(item) and str(item).endswith('/METADATA')}
            for parts in metadata_roots:
                roots.append(Path(*parts).parent)
            for root in roots:
                for path in [root, *sorted(root.rglob('*'))]:
                    relative = path.relative_to(site)
                    if path.is_symlink() or path.resolve().is_relative_to(site) is False:
                        raise DeploymentError('A dependency contains an unsupported linked file.')
                    if '__pycache__' in relative.parts or path.suffix in {'.so', '.pyc', '.pyo', '.dll', '.dylib'}:
                        continue
                    # Include source, package data and license/metadata only.
                    name = relative.as_posix() + ('/' if path.is_dir() else '')
                    if name in written:
                        continue
                    written.add(name)
                    archive.write(path, name)
    return output.getvalue()


def prepare_bundle(root=ROOT):
    files = {}
    for name in SOURCE_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise DeploymentError('An application source file is missing or linked.')
        files[name] = path.read_bytes()
    files['runtime.zip'] = runtime_zip()
    return files


def wsgi_source(username, root=ROOT):
    project = f'/home/{username}/Kaiser-api'
    prefix = ('# Generated Quote Desk WSGI entry point. No deployment token is stored here.\n'
              'import os\nimport sys\n'
              f'os.environ["QUOTE_DESK_ROOT"] = {project!r}\n'
              f'sys.path.insert(0, {project + "/runtime.zip"!r})\n')
    return (prefix + (root / 'deploy' / 'pythonanywhere_wsgi.py').read_text()).encode()


class ApiClient:
    def __init__(self, username, token, origin='https://www.pythonanywhere.com', opener=None):
        self.origin, self.domain = validate_account(username, origin)
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(not '!' <= c <= '~' for c in token):
            raise DeploymentError('Set QUOTE_HOST_API_KEY securely in environment settings.')
        self.username = username
        self._token = token
        self.base = self.origin + '/api/v0/user/' + quote(username, safe='') + '/'
        self.opener = opener or build_opener(_NoRedirect())

    def request(self, method, endpoint, fields=None, file_bytes=None, filename='upload', missing_ok=False):
        if endpoint.startswith('/') or '..' in endpoint.split('/') or '://' in endpoint:
            raise DeploymentError('Invalid PythonAnywhere API endpoint.')
        headers = {'Authorization': 'Token ' + self._token}
        body = None
        if file_bytes is not None:
            if not re.fullmatch(r'[A-Za-z0-9_.-]+', filename):
                raise DeploymentError('Invalid upload name.')
            boundary = 'quotedesk' + secrets.token_hex(16)
            headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="content"; filename="{filename}"\r\n'
                    'Content-Type: application/octet-stream\r\n\r\n').encode() + file_bytes + f'\r\n--{boundary}--\r\n'.encode()
        elif fields is not None:
            body = urlencode({k: ('true' if v else 'false') if isinstance(v, bool) else v for k, v in fields.items()}).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif method in {'POST', 'PATCH'}:
            body = b''
        req = Request(self.base + endpoint, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=25) as response:
                return response.read()
        except HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            raise DeploymentError(f'PythonAnywhere API request failed (HTTP {exc.code}). No provider response body was displayed.') from None
        except (URLError, OSError, TimeoutError):
            raise DeploymentError('Could not connect to the PythonAnywhere API. Check network access and account configuration.') from None

    def json(self, method, endpoint, fields=None, missing_ok=False):
        value = self.request(method, endpoint, fields=fields, missing_ok=missing_ok)
        if value is None:
            return None
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            raise DeploymentError('PythonAnywhere returned an unexpected API response.') from None

    def upload(self, absolute_path, contents):
        if not absolute_path.startswith((f'/home/{self.username}/Kaiser-api/', '/var/www/')):
            raise DeploymentError('Upload path is outside the application.')
        endpoint = 'files/path' + quote(absolute_path, safe='/')
        return self.request('POST', endpoint, file_bytes=contents, filename=PurePosixPath(absolute_path).name)


def verify_public(domain, opener=None, pause=time.sleep):
    """Check public endpoints without ever attaching the hosting-account token."""
    opener = opener or build_opener(_NoRedirect())
    for attempt in range(3):
        try:
            with opener.open(Request('https://' + domain + '/api/health'), timeout=15) as response:
                if response.status != 200 or json.load(response).get('status') != 'ok':
                    raise VerificationError('Public application health could not be confirmed.')
            with opener.open(Request('https://' + domain + '/'), timeout=15) as response:
                if response.status != 200 or b'Quote Desk' not in response.read():
                    raise VerificationError('Public sign-in page could not be confirmed.')
            return
        except HTTPError as exc:
            if exc.code in {502, 503, 504} and attempt < 2:
                pause(2)
                continue
            raise VerificationError(f'Files were uploaded and reload requested, but public verification returned HTTP {exc.code}. Deployment remains unverified.') from None
        except (URLError, OSError, TimeoutError):
            raise VerificationError('Files were uploaded and reload requested, but public verification is blocked or unreachable from this network. Deployment remains unverified.') from None
        except (ValueError, AttributeError):
            raise VerificationError('Files were uploaded and reload requested, but the public health response was unexpected. Deployment remains unverified.') from None


def deploy(client, files=None, root=ROOT, verifier=verify_public):
    """Create/update only the marked Quote Desk app on the default free domain."""
    files = files or prepare_bundle(root)
    if set(files) != {*SOURCE_FILES, 'runtime.zip'}:
        raise DeploymentError('The deployment bundle must contain the complete approved application.')
    project = f'/home/{client.username}/Kaiser-api'
    marker = dict(application='quote-desk', version=1, username=client.username, domain=client.domain, project=project)
    sites = client.json('GET', 'webapps/')
    if not isinstance(sites, list) or any(not isinstance(site, dict) for site in sites):
        raise DeploymentError('PythonAnywhere returned an unexpected web-app list.')
    if any(site.get('domain_name') != client.domain for site in sites):
        raise DeploymentError('This account already has another website. Nothing was changed; free accounts support one web app.')
    existing_marker = client.request('GET', 'files/path' + quote(project + '/' + MARKER, safe='/'), missing_ok=True)
    owned = False
    if existing_marker:
        try:
            owned = json.loads(existing_marker) == marker
        except (ValueError, TypeError):
            pass
    if sites and not owned:
        raise DeploymentError('An existing website is not marked as this Quote Desk deployment. Nothing was changed.')
    if not owned:
        tree = client.json('GET', 'files/tree/?' + urlencode({'path': project}), missing_ok=True)
        if tree:
            raise DeploymentError('The target directory contains existing files. Nothing was changed.')
    # A marker is written only after all application files were uploaded.
    for name, contents in files.items():
        if name not in {*SOURCE_FILES, 'runtime.zip'}:
            raise DeploymentError('The deployment bundle contains an unsupported file.')
        client.upload(project + '/' + name, contents)
    client.upload(project + '/' + MARKER, json.dumps(marker, sort_keys=True).encode())
    if not sites:
        client.request('POST', 'webapps/', fields={'domain_name': client.domain, 'python_version': 'python312'})
    endpoint = 'webapps/' + quote(client.domain, safe='') + '/'
    config = client.json('GET', endpoint)
    if not isinstance(config, dict) or config.get('domain_name', client.domain) != client.domain:
        raise DeploymentError('The created website configuration was unexpected.')
    # The documented official uploader uses this exact canonical filename.
    canonical = '/var/www/' + client.domain.replace('.', '_').lower() + '_wsgi.py'
    wsgi_path = config.get('wsgi_configuration_file') or canonical
    if wsgi_path != canonical:
        raise DeploymentError('The hosting service reported an unexpected WSGI path. No WSGI file was overwritten.')
    client.upload(wsgi_path, wsgi_source(client.username, root))
    client.request('PATCH', endpoint, fields={'source_directory': project, 'force_https': True})
    client.request('POST', endpoint + 'reload/')
    verifier(client.domain)
    return 'https://' + client.domain + '/'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--prepare', action='store_true', help='Prepare a local upload bundle (default).')
    actions.add_argument('--deploy', action='store_true', help='Deploy to the configured free PythonAnywhere account.')
    parser.add_argument('--output', type=Path, help='New empty directory for a prepared bundle; never contains credentials.')
    args = parser.parse_args(argv)
    try:
        if args.deploy:
            username = os.environ.get('QUOTE_HOST_USERNAME', '')
            origin = os.environ.get('QUOTE_HOST_API_ORIGIN', 'https://www.pythonanywhere.com')
            client = ApiClient(username, os.environ.get('QUOTE_HOST_API_KEY', ''), origin)
            url = deploy(client)
            print('Public app verified: ' + url)
        else:
            files = prepare_bundle()
            destination = args.output or Path(tempfile.mkdtemp(prefix='quotedesk-pythonanywhere-'))
            if destination.exists() and any(destination.iterdir()):
                raise DeploymentError('Choose a new empty output directory; existing files will not be overwritten.')
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            for name, contents in files.items():
                (destination / name).write_bytes(contents)
            print('Prepared application and pure-Python dependency bundle at ' + str(destination))
            print('No hosting resources were created and no credentials were saved.')
    except DeploymentError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
