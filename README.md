# Quote Desk

A responsive, dependency-free demo for managing trade enquiries in Alice Springs.

## Run it

Download this repository and open `index.html` in a modern browser, or run:

```sh
python3 -m http.server 8000
```

Then open http://localhost:8000.

## Included

- Dashboard with open enquiries, due follow-ups, quoted pipeline and won value in AUD.
- Create, edit and delete enquiries; search and filter by status.
- Follow-up dates and a chronological follow-up view.
- Customer enquiry form preview feeding the local dashboard.
- CSV export and browser-local persistence.
- Mobile layout, accessible form labels and native validation.

## Demo boundary

All initial customers are fictional. Use sample details only. This version stores data in localStorage on one browser: it has no authentication, shared database, tenant isolation, email or SMS delivery, or live customer submission endpoint. Clearing browser data clears saved enquiries. Won value represents recorded quotes, not payments received. Export CSV to retain a copy of demo records; import is not included.

Before using with real businesses, add secure accounts, server-side validation, a database with per-business access controls, backups, privacy/retention settings and configured notifications. A hosted static copy remains a demo until those services are implemented.

## Optional static hosting

In GitHub repository Settings → Pages, select Deploy from a branch, then `main` and `/ (root)`. This repository does not automatically enable hosting.
