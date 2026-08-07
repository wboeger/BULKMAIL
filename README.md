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
used only for that one send — it's never written to disk. Always leave
"Modo teste (dry-run)" checked for your first try, then use "Enviar apenas
um teste para" to send yourself one real message before sending to everyone.

Sending runs in the background, so submitting the form takes you to a live
progress page instead of a browser that hangs for ten minutes on a long
list. From that page you can watch each address as it goes out, **cancel**
mid-run, **download the log as CSV**, and **resume** a run that was
cancelled or interrupted — resume replays only the addresses that were never
sent, and asks for your SMTP password again since it was never stored.

Each run is kept under `webapp/_runs/<run_id>/` (recipient list, log, and
uploaded images — no credentials) so its progress page survives a server
restart. Runs are deleted automatically after 7 days; change that with
`RUN_RETENTION_DAYS=30 python3 webapp/app.py`.

Invalid and duplicate addresses are dropped before sending, and reported on
the run page.

The rest of this README covers the command-line version, which offers more
control (editable HTML template, per-run caps, etc.).

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

## Login OAuth2 para Microsoft 365 / Office 365 (alternativa a senha)

Muitos tenants Microsoft 365 (UFPR incluido) estao desativando o login SMTP
com usuario+senha ("Basic Auth") e exigindo login moderno, ou exigem MFA sem
permitir senha de app. Se `SMTP_PASSWORD` parar de funcionar mesmo com a
senha correta, use OAuth2 no lugar:

1. Registre um app publico no [Entra ID / Azure AD](https://entra.microsoft.com)
   (Azure Active Directory → App registrations → New registration):
   - Tipo de conta: conforme seu tenant (normalmente "Accounts in this
     organizational directory only").
   - Nao crie um client secret — este app e um "public client".
   - Em **Authentication**, adicione uma plataforma "Mobile and desktop
     applications" e marque **"Allow public client flows" = Yes**.
   - Em **API permissions**, adicione a permissao delegada
     **Office 365 Exchange Online → SMTP.Send** e peca consentimento do
     administrador do tenant (necessario na maioria das organizacoes).
   - Copie o **Application (client) ID** da pagina Overview.

2. No seu `.env`, defina:
   ```
   SMTP_AUTH_METHOD=oauth2
   MS_OAUTH_CLIENT_ID=<o client ID copiado acima>
   MS_OAUTH_TENANT_ID=organizations
   ```
   `SMTP_PASSWORD` pode ficar vazio. `MS_OAUTH_TENANT_ID=organizations` funciona
   pra login com qualquer conta de trabalho; use o Tenant ID especifico do seu
   tenant so se `organizations` for recusado.

3. Rode o login interativo uma unica vez (abre o navegador para voce entrar
   com sua conta):
   ```
   python3 oauth_setup.py
   ```
   Sem navegador disponivel (ex: servidor remoto), use
   `python3 oauth_setup.py --device-code`, que imprime um codigo e um link
   pra completar o login em outro dispositivo.

   Isso grava um refresh token em `.oauth_cache.json` (git-ignored, tao
   sensivel quanto uma senha — nunca compartilhe esse arquivo). Depois disso,
   tanto o CLI quanto a interface web usam esse cache automaticamente, sem
   pedir senha nem abrir navegador de novo. Se o token expirar ou for
   revogado (troca de senha, politica do tenant, etc.), rode
   `python3 oauth_setup.py` novamente.

   Na interface web, escolha "OAuth2 (Microsoft 365)" como metodo de login
   no lugar de digitar a senha.

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

- Most providers (Gmail, Microsoft 365) rate-limit or flag accounts that
  send large volumes too quickly. Keep `SEND_DELAY_SECONDS` reasonable and
  use `MAX_PER_RUN` (CLI) or "Lote"/"Pausa entre lotes" (web UI) to send in
  batches for large lists.
- This sends one individual message per recipient (not one message with
  everyone in `To:`/`Bcc:`), so each recipient only sees their own address.
- **Hard bounces and unsubscribes are remembered permanently** in
  `suppressed.csv` (git-ignored, at the repo root) and are skipped
  automatically on every future run from either front end. This matters
  because repeat bounces are themselves a reputation signal providers use
  to decide whether to throttle or restrict a sending account — don't
  delete rows from it without a real reason to believe they're wrong.
- If the server responds with a throttling signal mid-run (Microsoft's
  `550 5.7.708`, or a generic 4xx), the sender backs off automatically
  (`THROTTLE_BACKOFF_SECONDS` in `.env`, default 300s, doubling for
  `MAX_THROTTLE_RETRIES` attempts, default 2) instead of hammering the
  server. If it's still throttled after that, the run stops itself rather
  than risk turning a temporary slowdown into a full account restriction —
  re-run with `--resume` (CLI) or "Continuar de onde parou" (web UI) once
  sending is normal again.
- For lists beyond a few hundred recipients, consider a dedicated
  transactional email service (SES, SendGrid, Postmark, etc.) instead of
  your personal SMTP mailbox, since providers like Gmail impose daily
  sending caps (e.g. ~500/day for regular Gmail accounts).

## Getting unblocked (Microsoft 365 / UFPR)

If M365 has actually blocked or restricted the mailbox from sending
(rather than just delaying individual messages), that's an account-level
state on Microsoft's side — no client-side change fixes it by itself:

1. **Check for a "Restricted user" state.** Sending accounts that trip
   Microsoft's outbound-spam protection get automatically restricted from
   sending until released. A tenant admin (UFPR's NTI) needs to check
   **Microsoft 365 Defender → Email & collaboration → Review → Restricted
   entities** and release the account, or run
   `Get-BlockedSenderAddress` / the "unblock" action in the Exchange admin
   center.
2. **Ask NTI about the mailbox's outbound throttling policy.** Exchange
   Online's default outbound spam filter policy caps external recipients
   per hour/day per mailbox; a legitimate high-volume research mailing list
   is exactly the case admins can raise that limit for via a custom policy
   scoped to this one account, instead of the sender fighting the default
   limit indefinitely.
3. **Verify SPF/DKIM/DMARC on the sending domain.** Deliverability and
   throttling decisions are made mostly on domain authentication, not on
   anything this app can do at the message level — worth confirming with
   NTI that `ufpr.br` DKIM signing and an enforced DMARC policy are in
   place.
4. **Once released**, re-run with `--resume` / "Continuar de onde parou" —
   already-sent addresses are skipped, and `suppressed.csv` keeps any hard
   bounces from being retried.
