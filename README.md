# Bulk Mail

Send a personalized text + image HTML email to a list of recipients using
your own professional mailbox over SMTP. No external services involved —
your credentials only ever go to your mail provider's SMTP server.

## Web UI (simplest way to use it)

A local browser form is included so you don't need the command line at all:

```bash
pip install -r requirements.txt
python3 webapp/app.py
```

Then open **http://127.0.0.1:5001**. (Default port is 5001, not 5000,
because macOS usually has AirPlay Receiver bound to port 5000 already —
override with `PORT=8000 python3 webapp/app.py` if 5001 is also busy.)
Fill in your SMTP login, subject,
message text, upload images, and paste/upload your recipients CSV. It only
listens on localhost (not exposed to the internet), and your password is
used only for that one request — it's never written to disk. Always leave
"Modo teste (dry-run)" checked for your first try, then use "Enviar apenas
um teste para" to send yourself one real message before sending to everyone.

The rest of this README covers the command-line version, which offers more
control (editable HTML template, resume, per-run caps, etc.).

## Setup (command line)

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Configure your mailbox:
   ```
   cp .env.example .env
   ```
   Edit `.env` and set `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, etc.
   - **UFPR institutional email (Microsoft 365)**: host `smtp.office365.com`,
     port `587`, STARTTLS (`SMTP_USE_SSL=false`), username = your full
     `@ufpr.br` address, password = your normal mailbox password. This is
     the default already set in `.env.example`.
   - **Gmail / Google Workspace**: host `smtp.gmail.com`, port `587`. If
     2‑Step Verification is on (recommended), generate an
     [App Password](https://myaccount.google.com/apppasswords) — your normal
     login password won't work.
   - **Other Microsoft 365 / Outlook accounts**: same as UFPR above, host
     `smtp.office365.com`, port `587`.
   - Other providers: check their SMTP docs for host/port.

   `.env` is git-ignored and never committed.

3. Add your recipients:
   ```
   cp recipients.example.csv recipients.csv
   ```
   Edit `recipients.csv`. Required column: `email`. Any other column
   (`name`, `company`, …) becomes a merge field you can use in the
   subject/body as `{{name}}`, `{{company}}`, etc.

4. Add your images to the `images/` folder (e.g. `logo.png`, `banner.jpg`).
   Reference them in `templates/email.html` as `<img src="cid:logo">` —
   the `cid` name is just the filename without its extension. Images are
   embedded directly in the email, not linked, so they show up even for
   recipients who block remote images.

5. Edit `templates/email.html` (rich version) and `templates/email.txt`
   (plain-text fallback) with your actual message. Merge fields like
   `{{name}}` work in both.

## Sending

Always test before a real run:

```bash
# 1. Dry run - renders everything, sends nothing
python send_bulk_mail.py --recipients recipients.csv --subject "Hello {{name}}" --dry-run

# 2. Send one real test message to yourself
python send_bulk_mail.py --recipients recipients.csv --subject "Hello {{name}}" --test-email you@yourcompany.com

# 3. Send to everyone
python send_bulk_mail.py --recipients recipients.csv --subject "Hello {{name}}"
```

Useful flags:
- `--max-per-run N` — cap how many emails go out in one run (also settable via `MAX_PER_RUN` in `.env`).
- `--resume` — skip addresses already marked `sent` in `sent_log.csv`, so an interrupted run can be safely re-run.
- `SEND_DELAY_SECONDS` in `.env` — pause between sends to stay under your provider's rate limits and reduce spam-flagging risk.

Every run appends to `sent_log.csv` (email, status, timestamp, error if any) so you can audit what went out.

## Notes on deliverability

- Most providers (Gmail included) rate-limit or flag accounts that send
  large volumes too quickly. Keep `SEND_DELAY_SECONDS` reasonable and use
  `MAX_PER_RUN` to send in batches for large lists.
- This sends one individual message per recipient (not one message with
  everyone in `To:`/`Bcc:`), so each recipient only sees their own address.
- For lists beyond a few hundred recipients, consider a dedicated
  transactional email service (SES, SendGrid, Postmark, etc.) instead of
  your personal SMTP mailbox, since providers like Gmail impose daily
  sending caps (e.g. ~500/day for regular Gmail accounts).
