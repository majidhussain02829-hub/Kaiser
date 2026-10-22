# Open Quote Desk outside the cloud workspace

The server in the coding workspace is private. Its successful internal health
check does not create a public website. Workspace file links, opening
`index.html`, and GitHub Pages cannot run this application's Python API.

## Render deployment

`render.yaml` prepares one Python web service with a persistent disk, HTTPS
cookies, a health check and the reminder worker. The `starter` service and
persistent disk incur Render charges; review current pricing in Render before
creating them. No hosting account, paid service or public deployment has been
created by adding this file.

1. Publish the completed application files to your GitHub repository. The
   source in this workspace includes the new Python backend; a host cannot
   deploy those files until they exist in the branch it builds. Do not commit
   `.venv`, `instance`, private databases, session keys or provider credentials.
2. In Render, create a Blueprint connected to that repository and select the
   branch containing `render.yaml`. Review the service and persistent-disk
   charges before creating the resources. Automatic deployments are off so
   future repository changes require an explicit deployment.
3. Wait for the build and health check to finish. Open the HTTPS address that
   Render assigns in its dashboard. This is the address to use on your phone
   or computer. Confirm `/api/health` returns `{"status":"ok"}`, then create
   your business account in the app.
4. Create a sample enquiry, restart the service from Render, and sign in to
   confirm it remains saved. The disk keeps the database and session signing
   key at `/var/data/quote-desk`. The deployment starts with a new database;
   transferring existing workspace data requires a private backup and restore.
5. Add the Resend, Twilio and OpenAI settings documented in `README.md` through
   Render's secret environment settings when ready to enable those features.
   They are optional for opening the app and using enquiries and on-screen
   reminders. Restart the service after changing environment variables.

Keep the service running continuously for timely automated reminders and take
database backups. Use the `app.py` launcher: it starts both Waitress and the
background reminder worker. Static hosting and a plain Flask/Gunicorn factory
without a worker are insufficient for the complete application.

## Other Python hosting

Use Python 3.12 or newer and install `requirements.txt`. Set `QUOTE_DESK_DATA`
to a private directory on persistent storage and `COOKIE_SECURE=1` behind
HTTPS. Run one always-on application instance with:

```sh
python app.py --host 0.0.0.0 --port "$PORT"
```

The host supplies `PORT` and the public HTTPS address. Use `/api/health` as its
readiness check. Starting this command inside the private coding workspace
alone does not make it publicly reachable.
