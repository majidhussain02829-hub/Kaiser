"""Authenticated scheduler entry point for hosts without background workers."""
import os
import secrets

from flask import abort, jsonify, request

from automations import process_automations


def register_scheduler(app):
    """Allow a trusted external scheduler to check due, opted-in automations.

    This route uses its own Bearer credential, so the application may exempt only
    the exact ``automation_tick`` endpoint from browser-session CSRF validation.
    The feature stays disabled until a sufficiently strong token is configured.
    """
    @app.post('/api/automations/tick', endpoint='automation_tick')
    def automation_tick():
        token = os.environ.get('QUOTE_SCHEDULER_TOKEN', '')
        if not 32 <= len(token) <= 512 or any(not '!' <= char <= '~' for char in token):
            abort(503, description='External scheduling is not configured.')
        supplied = request.headers.get('Authorization', '').encode('utf-8')
        expected = ('Bearer ' + token).encode('ascii')
        if not secrets.compare_digest(supplied, expected):
            abort(401, description='Scheduler authentication is required.')
        if not request.is_json:
            abort(415, description='Send an empty JSON object.')
        if request.get_json() != {}:
            abort(400, description='Send an empty JSON object.')
        return jsonify(process_automations(app, time_budget=45, max_jobs=10))
