# Quote Desk

A running enquiry and quote management application for trade businesses. Flask and Waitress serve the dashboard and API; SQLite stores accounts and enquiries on the server. Each account has its own business workspace and shareable customer enquiry form. There are no seeded customers or browser-only records.

## Run

Requires Python 3.12 or newer.

```sh
cd /workspace/Kaiser
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open the app on port 8000 in a browser and select **Create an account**. Supply your business name, email and a password of at least 12 characters. No preconfigured password or administrator account exists. The server defaults to loopback; use `--host 0.0.0.0 --port 8000` only when deliberately exposing it through a configured host or reverse proxy.

## Working features

- Account registration, sign-in, sign-out, password hashing and eight-hour signed sessions.
- Separate business workspaces, with ownership checked on every private API operation.
- Persistent enquiry creation, editing and deletion, with stale-edit detection.
- Server validation of fields, statuses, amounts and dates.
- Open enquiry totals, quoted pipeline, won values, search, filters and follow-up scheduling.
- A customer form at `/request/<business-public-id>`, linked from the dashboard. Customers need no account; submissions appear in the owning business's workspace. Refresh the dashboard to retrieve new submissions.
- CSV export of all enquiries, including protection against spreadsheet formula injection.
- CSRF protection and rate limiting of account creation, login and public submissions. Limits use the direct socket address; behind a proxy they conservatively share a quota. Configure a trusted proxy strategy before scaling.
- Responsive layout and server health check at `/api/health`.

Follow-ups appear in the dashboard and the Automations page. Optional email and SMS integrations send queued customer messages and owner reminders when provider accounts are connected. Quote values are recorded amounts, not collected payments. Existing browser-local demo data is not automatically imported.

## Data, backups and recovery

By default, private data is in `instance/`, ignored by Git. Set `QUOTE_DESK_DATA` to an absolute persistent data directory when hosting. Keep this directory private and retain it across restarts and deployments. It contains the SQLite database and a generated session signing key. Never serve it as static files or commit it. Use the application server, not `python -m http.server`.

Create a consistent online database backup, including integrity verification:

```sh
.venv/bin/python manage.py backup /secure/path/quotedesk-backup.sqlite3
```

Store backups outside the checkout on protected storage. Automate this command using your hosting scheduler and retention policy. To restore, stop the app, preserve the current database, remove old database WAL/SHM files, and restore a verified backup as `quotedesk.sqlite3` in the data directory. Restart and verify login and enquiries. The session key can be retained or regenerated to sign everyone out.

An operator with trusted server access can recover an account password:

```sh
.venv/bin/python manage.py reset-password owner@example.com
```

Passwords are requested through a hidden terminal prompt. Restart afterward to activate the rotated session key and sign out existing sessions. Self-service password reset and email verification require an email provider and are not implemented.

## Validation

```sh
.venv/bin/python -m unittest -v
curl --fail http://127.0.0.1:8000/api/health
```

The test suite exercises authentication, CSRF, CRUD, persistent data across application instances, business isolation, stale updates, validation, customer submissions and rate limits using temporary databases. Browser workflow validation during implementation additionally covered account creation, editing, public submission in another browser context, reload, search, quote totals, CSV content, deletion and sign-in/out.

## Hosting

See [DEPLOY.md](DEPLOY.md) for the free PythonAnywhere setup with persistent
SQLite storage and an optional hourly GitHub scheduler. The earlier paid Render
example is in `deploy/render-paid.yaml`; it is not the default deployment. The app has not been
deployed publicly; opening this repository's HTML file or GitHub Pages cannot
run the backend.

This environment runs the app locally; it does not publish a public site. For public use, provide HTTPS, a persistent volume, protected backups, monitoring and a hosting process manager. Set `COOKIE_SECURE=1` under HTTPS. Do not set it for plain HTTP local development, because browsers will not return secure cookies. Never use the Flask debug server publicly. Waitress is the application server used by `app.py`.

Customers can use the shareable enquiry link once the server has a reachable public HTTPS address. Provider integrations are included; connecting credentials and verified sender identities is required for outbound delivery. Public deployment is not configured here.


## AI, customer messages and follow-up reminders

Open **Automations** to see due follow-ups, message history, provider setup status and business settings. Existing accounts and enquiries are upgraded in place; existing enquiries start with contact permissions off. Owner email reminders and automatic customer messages are off until enabled.

1. Open an enquiry, record the customer's actual permission for email and/or SMS, and save. SMS numbers must use international format, such as `+61412345678`.
2. Choose **Message**, select email or SMS and a message purpose, then **Generate draft**. When OpenAI is connected this generates an editable AI draft; otherwise the app explicitly labels a standard template. AI receives customer name, service, job details, status and amount, without contact addresses/numbers. Review generated text before choosing **Queue message**.
3. For automatic quote follow-ups, set status **Quoted**, a follow-up date, contact permission and the desired customer channel in Automations. Enable AI for automatic messages only if you want those drafts sent without individual review. Otherwise a standard template is used.
4. For owner reminders, enable **Email me when follow-ups are due**. The recipient is your account email. All active statuses (New, Contacted and Quoted) are included. On-screen reminders work without a delivery provider. **Tomorrow** moves the follow-up date forward.
5. Choose your business timezone (default `Australia/Darwin`) and local send hour (default 09:00). The standard `app.py` launcher scans every 60 seconds. Free PythonAnywhere hosting uses the optional hourly external scheduler instead. It sends each enabled channel once per enquiry's follow-up date, including overdue dates. Changing a date schedules a new reminder. Closed enquiries do not trigger automatic follow-ups.

The same database retains the message outbox and history across restarts. **Queued**, **Needs setup**, **Provider accepted**, **Failed**, **Cancelled**, and **Delivery uncertain** have different meanings. Provider acceptance is not confirmation of recipient delivery; delivery receipts and incoming replies are not synced into this app. Look at the provider dashboard for final delivery and reply status. Check messages with an uncertain result before attempting any new send; the app does not automatically retry failures or interrupted sends. Messages blocked before contacting a provider have an explicit **Retry after setup** action. Repeating a manual request with the same request ID does not send twice.

Automatic jobs are rechecked before dispatch: changes to the follow-up date, closed status, contact permission or recipient cancel stale jobs. Keep customer contact permissions updated if they ask to stop. SMS messages identify the business and include STOP; configure your Twilio sender's opt-out handling and monitor the Twilio inbox. Customer email replies are directed to the business account email, and templates provide that address for stopping follow-ups. Custom manual drafts remain the sender's responsibility.

### Provider configuration

Supply credentials securely through environment settings or your host's secret manager; do not place them in Git or chat. All integrations use HTTPS with certificate verification. Each delivery channel needs its own configured sender:

| Variable | Purpose |
| --- | --- |
| `QUOTE_EMAIL_API_KEY` | Resend API key, scoped for sending at `api.resend.com` |
| `QUOTE_EMAIL_FROM` | A sender address on your verified Resend domain |
| `QUOTE_SMS_ACCOUNT_SID` | Twilio Account SID (`AC` followed by 32 hex characters) |
| `QUOTE_SMS_AUTH_TOKEN` | Twilio Auth Token for `api.twilio.com` |
| `QUOTE_SMS_FROM` | Your SMS-capable Twilio number, including country code |
| `QUOTE_AI_API_KEY` | OpenAI API key for `api.openai.com` |
| `QUOTE_AI_MODEL` | Optional OpenAI model; defaults to `gpt-4.1-mini` |

Allow those three API hostnames in restricted network settings. Provider setup indicators show whether required bindings are present, not a verified account, sender or successful delivery. Verified senders, Twilio destination permissions/trial restrictions, sufficient provider credit and working network access are also prerequisites. Twilio uses HTTP Basic authentication: the runtime must supply a usable token to that adapter. If a secret proxy provides only a placeholder it cannot replace inside Base64 authorization, bind the real token through the hosting platform's supported secure runtime mechanism; do not extract proxy credentials. Email and AI keys use Bearer headers.

Restart the application after changing runtime environment variables. The standard `app.py` command starts the scheduler automatically; custom WSGI launchers must also start `automations.start_worker(app)` once per process or call `process_automations(app)` from a trusted periodic job. No external cron is required with the standard launcher. On free WSGI hosting, use the authenticated hourly GitHub workflow documented in DEPLOY.md instead of a background thread. Keep the server continuously running for timely reminders. The database outbox coordinates multiple workers; provider errors with ambiguous results are retained for operator review.

Run `.venv/bin/python -m unittest -v` for the provider-mocked automation tests alongside the original application suite. Tests exercise payloads, consent, owner reminders, timezone scheduling, deduplication, isolation, missing-provider states and send failures without contacting live customers or providers. Live delivery and AI generation still need credentials and a controlled verification using your own recipients.
