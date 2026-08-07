# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted bulk mailer: sends one individual personalized text+inline-image
email per recipient over the operator's own SMTP mailbox (no third-party
sending service). Two front ends over the same message-building core:

- **CLI** — `send_bulk_mail.py`, configured by `.env`, templates edited by hand.
- **Local web UI** — `webapp/app.py` (Flask), configured per-request by a form,
  message body composed from plain text. UI strings are Portuguese (pt-BR).

## Commands

```bash
pip install -r requirements.txt

# Web UI (localhost only; 5001 because macOS AirPlay takes 5000)
python3 webapp/app.py
PORT=8000 python3 webapp/app.py
RUN_RETENTION_DAYS=30 python3 webapp/app.py   # run-dir GC window (default 7)

# CLI: dry run -> single test -> real send
python send_bulk_mail.py --recipients recipients.csv --subject "Hello {{name}}" --dry-run
python send_bulk_mail.py --recipients recipients.csv --subject "Hello {{name}}" --test-email you@example.com
python send_bulk_mail.py --recipients recipients.csv --subject "Hello {{name}}"
python send_bulk_mail.py --recipients recipients.csv --subject "..." --resume --max-per-run 50
```

There is no test suite, linter, or build step. Verify changes with
`python3 -m py_compile send_bulk_mail.py webapp/app.py webapp/jobs.py` plus a
`--dry-run` CLI run and/or a dry-run through the web form (`--log` /
`RUNS_DIR` output goes to the scratchpad, not the repo, when experimenting).

## Architecture

**`send_bulk_mail.py` is the shared core.** `webapp/app.py` does
`sys.path.insert(ROOT)` and imports `build_message`, `dedupe_recipients`,
`render`, `sanitize_cid` from it. Changes to those functions affect both front
ends — check `webapp/` before editing them.

**MIME structure is deliberate** (`build_message`): `multipart/related` as the
*outer* container wrapping `multipart/alternative(text, html)` with inline
images as siblings. Outlook/Exchange renders images as attachments if nested
the other way. `sanitize_cid` exists for the same reason — Content-IDs must be
`[A-Za-z0-9_-]` or clients can't match `<img src="cid:...">`.

**Header injection is guarded:** merge fields come from an untrusted CSV, so
every header value passes through `sanitize_header` and HTML interpolation in
`compose_html` passes through `html.escape`. Keep that when touching either.

**Web UI send flow** (`app.py::_send` → `jobs.py`): a long list with a
per-message delay outlasts any browser timeout, so `/send` writes a run
directory, spawns a daemon thread, and redirects to `/run/<run_id>`, which
polls `/run/<run_id>/status` every second.

**`webapp/_runs/<run_id>/` is the source of truth**, not the in-memory `Job`:

```
campaign.json   cfg, subject, message, recipients, image cid->filename map
log.csv         timestamp,email,status,error — appended per recipient
images/         uploaded inline images (kept so a run can be resumed)
```

The SMTP password is **never** persisted — `_send` pops it before writing
`campaign.json`, and `/run/<id>/resume` re-prompts for it. Do not add it to the
campaign dict for convenience.

Because the log is on disk, `jobs.status()` reconstructs progress after a
server restart: no live `Job` + complete log = `done`, no live `Job` +
incomplete log = `interrupted` (resumable). Resume skips addresses already
logged as sent.

**Status vocabularies differ between front ends** — CLI writes `sent`/`failed`
to `sent_log.csv`; the web UI writes `enviado`/`falhou`/`dry-run` and
`jobs.sent_addresses` / `run.html` CSS classes key off those Portuguese
values. Don't unify one side without the other.

`jobs.start()` takes `send_one`, `connect`, and `friendly_error` as injected
callables so `jobs.py` stays free of message-building and Flask details.

## Conventions

- Merge fields are `{{field}}`, substituted by `render()` from any CSV column,
  plus injected `from_name` / `from_email`. Missing fields render as empty.
- The web form accepts either a real CSV with an `email` header or a bare list
  of addresses (`parse_recipients` sniffs the first cell); uploads are decoded
  through `decode_upload`'s encoding ladder because Excel-on-Windows exports
  are usually cp1252 with accented Portuguese names.
- `_run_dir()` resolves and confirms a run id stays inside `RUNS_DIR` — every
  `/run/<run_id>/*` route must go through it.
- SMTP errors reach the user via `friendly_smtp_error`, which special-cases
  `SendAsDenied` and auth failures with Portuguese explanations.
- `.env`, `recipients.csv`, `sent_log.csv`, and `webapp/_runs/` are
  git-ignored; `.env.example` and `recipients.example.csv` are the committed
  templates. Defaults target UFPR institutional Microsoft 365
  (`smtp.office365.com:587`, STARTTLS).
