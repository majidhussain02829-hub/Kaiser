# Free deployment: PythonAnywhere

The website must run its Python server. Opening `index.html` or a GitHub Pages
copy cannot provide login, a database or message delivery.

PythonAnywhere's **Beginner/free** account can host this app with a persistent
SQLite database. No paid disk or paid web service is selected by this setup.
No hosting account has been created or website deployed by publishing these files.

## Create the free website

1. Create a free account at https://www.pythonanywhere.com/registration/register/beginner/.
   Choose the free Beginner account. Open a Bash console from its dashboard.
2. Clone the app and run its setup helper:

   ```sh
   git clone https://github.com/majidhussain02829-hub/Kaiser.git ~/Kaiser
   cd ~/Kaiser
   bash deploy/setup-pythonanywhere.sh
   ```

   If `~/Kaiser` already exists, update your existing checkout after preserving
   any local changes. Do not clone over it. The helper preserves existing private
   settings and creates a Python virtual environment, installs pinned packages
   and runs the test suite. It reports which Python version to select.
3. In the **Web** tab, choose **Add a new web app**, use the free domain assigned
   to your account, and choose **Manual configuration** with the same Python
   version reported by setup (3.12 or newer).
4. Set the virtualenv field to `/home/YOUR_USERNAME/Kaiser/.venv`.
5. Open the WSGI configuration link in that Web tab. Replace the generated sample
   WSGI code with the contents of `deploy/pythonanywhere_wsgi.py`, then save.
   The configuration automatically uses your home directory and loads the app
   from `~/Kaiser`.
6. Click **Reload**. Open the exact HTTPS website address shown in the Web tab
   and create your business account. Confirm `/api/health` returns
   `{"status":"ok"}`. Create an enquiry, reload the web app and sign in to verify
   it remains saved.

The database and session key live at `~/.local/share/quote-desk/`, outside the
checkout. Provider keys and the optional scheduler token live in the private
file `~/.config/quote-desk/environment.json` (permissions 0600). This file starts
empty; no API credentials are needed for the basic website. Never commit these
private files. Reload the web app after changing private settings.

Operator commands must point to that same data directory. In a Bash console:

```sh
cd ~/Kaiser
QUOTE_DESK_DATA="$HOME/.local/share/quote-desk" .venv/bin/python manage.py backup "$HOME/quote-desk-backup.sqlite3"
QUOTE_DESK_DATA="$HOME/.local/share/quote-desk" .venv/bin/python manage.py reset-password owner@example.com
```

Choose a new backup filename each time, store backups privately and reload the
web app after password recovery to activate its new session signing key.

Free accounts currently have a small CPU/disk allowance and one web app; new
accounts have 512 MiB of disk. Renew the web app before its one-month expiry
using the button on the Web tab. Back up the database as described in README.md.
This is small-scale free hosting with quotas, not an always-on service guarantee.

## Free hourly follow-up checks

Free PythonAnywhere accounts do not include always-on tasks, and new free
accounts do not include scheduled tasks. The WSGI configuration intentionally
does not start an unreliable background thread. On-screen reminders still work.

The included GitHub Actions workflow `.github/workflows/followups.yml` can
invoke the authenticated scheduler endpoint approximately once an hour. It is
inactive until a repository variable is configured. No third-party cron account
or paid hosting worker is required.

1. Generate a long random scheduler token in your own secure password manager,
   or in a private console, and store it in the PythonAnywhere private JSON
   configuration as `QUOTE_SCHEDULER_TOKEN`. Use at least 32 random printable
   characters without spaces. Do not paste the token into chat or Git.
2. In the Kaiser repository, open **Settings → Secrets and variables → Actions**.
   Add a repository secret named `QUOTE_SCHEDULER_TOKEN` with the same token.
   Add a repository variable named `QUOTE_SCHEDULER_URL` with your actual website
   address followed by `/api/automations/tick` (HTTPS).
3. Reload the PythonAnywhere web app. In GitHub's **Actions** tab, enable the
   workflow and run **Hourly quote follow-ups → Run workflow** once. Confirm a
   successful result and inspect the app's message history.
4. In the app's Automations page, enable the channels you want. Customer
   follow-ups still require recorded permission and a due quoted enquiry.

The token can trigger only due automation processing; it does not grant access
to business records or browser sessions. A missing token disables the endpoint.
Browser API routes retain CSRF protection. Do not put the token in a URL.
Each tick processes at most 10 messages within a cooperative 45-second budget;
remaining jobs stay queued for a later check. Provider socket timeouts are
limited by the time remaining. Large backlogs need additional manual checks or
an appropriately sized hosting plan.

GitHub schedules can be delayed; reminders may run on the next hourly check
rather than exactly at the selected hour. Public repositories can have scheduled
workflows disabled after 60 days without activity. Standard hosted-runner jobs
are free for public repositories. Private repositories use included Actions
minutes; this workflow has a two-minute limit per hourly run. Review your account's
remaining allowance and keep paid overage disabled/a zero spending budget if
applicable. The workflow does not purchase capacity or change billing settings.

## Email, SMS and AI

The website and on-screen reminders work without provider accounts. Live
messages and AI use the provider bindings described in README.md. Configure them
in the private JSON file, then reload. Hosting being free does not make SMS or
AI API usage free; leave those integrations unconfigured to avoid provider costs.

PythonAnywhere free outbound Internet access is restricted to its allowlist.
Twilio proxy use is documented by the provider. Resend/OpenAI availability must
be checked on the deployed account; their reachability has not been verified.
The application's HTTPS client uses the host's proxy environment configuration
and retains TLS verification. Do not enable automations until a controlled test
with your own recipient succeeds.

## Optional paid Render configuration

The earlier paid Render example was moved to `deploy/render-paid.yaml` so there
is no paid default Blueprint at the repository root. Do not use that file when
you want free hosting. Render's free local filesystem does not preserve this
app's SQLite database across restarts, so simply changing its plan to `free`
would risk losing business records.

Official provider references:

- https://help.pythonanywhere.com/pages/FreeAccountsFeatures/
- https://help.pythonanywhere.com/pages/AlwaysOnTasks/
- https://help.pythonanywhere.com/pages/TwilioBehindTheProxy/
- https://www.pythonanywhere.com/whitelist/
