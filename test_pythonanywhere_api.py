"""No live hosting calls: the PythonAnywhere API is fully mocked here."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from zipfile import ZipFile

from deploy.pythonanywhere_api import (ApiClient, DeploymentError, VerificationError,
    SOURCE_FILES, _NoRedirect, deploy, prepare_bundle, runtime_zip, validate_account, verify_public)


class PythonAnywhereTests(unittest.TestCase):
    def test_fixed_origins_and_default_free_domains(self):
        self.assertEqual(validate_account('owner','https://www.pythonanywhere.com')[1], 'owner.pythonanywhere.com')
        self.assertEqual(validate_account('owner','https://eu.pythonanywhere.com')[1], 'owner.eu.pythonanywhere.com')
        for origin in ['http://www.pythonanywhere.com', 'https://evil.example', 'https://www.pythonanywhere.com@evil.example']:
            with self.assertRaises(DeploymentError):validate_account('owner',origin)
        for username in ['../other', 'owner/other', '']:
            with self.assertRaises(DeploymentError):validate_account(username,'https://www.pythonanywhere.com')

    def test_token_only_to_fixed_api_and_multipart_upload(self):
        opener=Mock()
        opener.open.side_effect=[io.BytesIO(b'[]'),io.BytesIO(b'{}')]
        client=ApiClient('owner','test-private-key',opener=opener)
        self.assertEqual(client.json('GET','webapps/'),[])
        client.upload('/home/owner/Kaiser-api/app.py',b'public-source')
        first=opener.open.call_args_list[0].args[0]
        second=opener.open.call_args_list[1].args[0]
        self.assertEqual(first.full_url,'https://www.pythonanywhere.com/api/v0/user/owner/webapps/')
        self.assertEqual(first.get_header('Authorization'),'Token test-private-key')
        self.assertIn(b'name="content"',second.data)
        self.assertIn(b'public-source',second.data)
        self.assertEqual(opener.open.call_args.kwargs,{'timeout':25})
        with self.assertRaises(DeploymentError):client.request('GET','https://evil.example/')
        self.assertIsNone(_NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example/'))
        for token in ['', 'with space', 'unicode-\u00e9', 'x'*4097]:
            with self.assertRaises(DeploymentError):ApiClient('owner',token,opener=opener)

    def test_api_errors_do_not_expose_credentials_or_bodies(self):
        opener=Mock()
        opener.open.side_effect=HTTPError('https://www.pythonanywhere.com',403,'private-token',{},io.BytesIO(b'secret body'))
        client=ApiClient('owner','test-private-key',opener=opener)
        with self.assertRaises(DeploymentError) as result:client.json('GET','webapps/')
        self.assertNotIn('private-token',str(result.exception))
        self.assertNotIn('secret body',str(result.exception))

    def test_existing_unknown_site_is_not_modified(self):
        client=Mock(username='owner',domain='owner.pythonanywhere.com')
        client.json.return_value=[{'domain_name':'owner.pythonanywhere.com'}]
        client.request.return_value=None
        with self.assertRaises(DeploymentError):deploy(client,files=self.files())
        client.upload.assert_not_called()
        self.assertTrue(all(call.args[0]=='GET' for call in client.request.call_args_list))

    @staticmethod
    def files():
        return {name:b'test source' for name in (*SOURCE_FILES,'runtime.zip')}

    def test_deployment_uses_standard_free_webapp_endpoints(self):
        client=Mock(username='owner',domain='owner.pythonanywhere.com')
        def json_response(method,endpoint,**kwargs):
            if endpoint=='webapps/':return []
            if endpoint.startswith('files/tree/'):return []
            return {'domain_name':'owner.pythonanywhere.com','wsgi_configuration_file':'/var/www/owner_pythonanywhere_com_wsgi.py'}
        client.json.side_effect=json_response
        client.request.return_value=None
        verifier=Mock()
        url=deploy(client,files=self.files(),verifier=verifier)
        self.assertEqual(url,'https://owner.pythonanywhere.com/')
        verifier.assert_called_once_with('owner.pythonanywhere.com')
        mutations=[call for call in client.request.call_args_list if call.args[0]!='GET']
        self.assertEqual(len(mutations),3)
        self.assertEqual(mutations[0].args,('POST','webapps/'))
        self.assertEqual(mutations[0].kwargs['fields'],{'domain_name':'owner.pythonanywhere.com','python_version':'python312'})
        self.assertEqual(mutations[-1].args,('POST','webapps/owner.pythonanywhere.com/reload/'))
        self.assertTrue(all(not any(x in call.args[1] for x in ['billing','account','always_on','schedule','consoles']) for call in mutations))
        wsgi=client.upload.call_args_list[-1].args[1]
        self.assertIn(b'runtime.zip',wsgi)
        self.assertNotIn(b'QUOTE_HOST_API_KEY',wsgi)

    def test_public_checks_never_include_host_auth(self):
        opener=Mock()
        health=io.BytesIO(b'{"status":"ok"}');health.status=200
        page=io.BytesIO(b'<title>Quote Desk</title>');page.status=200
        opener.open.side_effect=[health,page]
        verify_public('owner.pythonanywhere.com',opener=opener)
        for call in opener.open.call_args_list:
            self.assertIsNone(call.args[0].get_header('Authorization'))
        opener.open.side_effect=URLError('403 Forbidden')
        with self.assertRaises(VerificationError) as result:verify_public('owner.pythonanywhere.com',opener=opener)
        self.assertIn('unverified',str(result.exception))

    def test_bundle_contains_only_approved_code_and_imports_without_site_packages(self):
        files=prepare_bundle()
        self.assertEqual(set(files),{*SOURCE_FILES,'runtime.zip'})
        with ZipFile(io.BytesIO(files['runtime.zip'])) as archive:
            names=archive.namelist()
            self.assertIn('flask/sansio/',names)
            self.assertFalse(any(n.endswith(('.so','.pyc','.dll')) for n in names))
            self.assertFalse(any('session.key' in n or '.sqlite3' in n for n in names))
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'runtime.zip';archive.write_bytes(files['runtime.zip'])
            code='import sys;sys.path.insert(0,sys.argv[1]);from flask import Flask;from markupsafe import escape;assert str(escape("<x>"))=="&lt;x&gt;";assert Flask("probe").test_client().get("/").status_code==404'
            result=subprocess.run([sys.executable,'-S','-c',code,str(archive)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
