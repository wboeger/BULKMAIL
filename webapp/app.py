#!/usr/bin/env python3
"""Simple local web UI for the bulk mail sender.

Run with:
    python3 webapp/app.py
Then open http://127.0.0.1:5001 in your browser.

Nothing here is exposed to the internet by default -- it only listens on
localhost. Your SMTP password is used in-memory for the request and is
never written to disk.

Note: default port is 5001, not 5000, because on macOS port 5000 is
usually taken by the AirPlay Receiver system service. Override with the
PORT environment variable if 5001 is also busy, e.g. PORT=8000 python3 webapp/app.py
"""
import csv
import html
import io
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from send_bulk_mail import (  # noqa: E402
    build_message,
    classify_smtp_error,
    dedupe_recipients,
    filter_suppressed,
    render,
    sanitize_cid,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobs  # noqa: E402  (sits next to this file)
import msoauth  # noqa: E402  (imported via ROOT, same as send_bulk_mail)
import suppression  # noqa: E402  (imported via ROOT, same as send_bulk_mail)

# override=True: .env is the source of truth for this app. Without it a stale
# SMTP_* left in the parent shell's environment silently wins over the file.
load_dotenv(ROOT / ".env", override=True)

RUNS_DIR = ROOT / "webapp" / "_runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
jobs.cleanup_old_runs(RUNS_DIR, days=int(os.environ.get("RUN_RETENTION_DAYS", 7)))

EMAIL_RE = re.compile(r"^[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+$")


def connect_smtp(cfg):
    if cfg["use_ssl"]:
        smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30)
    else:
        smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        smtp.starttls()
    smtp.login(cfg["user"], cfg["password"])
    return smtp


def friendly_smtp_error(exc):
    if isinstance(exc, msoauth.OAuthTokenError):
        return str(exc)
    text = str(exc)
    if classify_smtp_error(exc) == "throttled":
        return (
            "O Microsoft 365 sinalizou que esta caixa esta enviando mensagens demais, "
            "demais rapido, ou parecido com spam (\"550 5.7.708\"). Isso pode levar a "
            "conta a ser restringida de enviar e-mails ate um administrador liberar. "
            "Aumente o intervalo entre mensagens / use pausas entre lotes, e veja "
            "README.md secao \"Getting unblocked\" antes de tentar de novo. " + text
        )
    if "SendAsDenied" in text:
        return (
            "O servidor recusou o envio porque o e-mail do remetente e diferente "
            "do e-mail usado para login. No campo \"E-mail do remetente\", deixe em "
            "branco (usa automaticamente seu usuario SMTP) ou use o mesmo endereco "
            "com que voce fez login."
        )
    if "Authentication" in text or "authentication" in text or "535" in text:
        return (
            "Falha de autenticacao no servidor SMTP. Confira usuario e senha, e "
            "se sua conta exige senha de aplicativo (app password) em vez da senha normal."
        )
    return text if len(text) < 300 else text[:300] + "..."

app = Flask(__name__)


def decode_upload(raw_bytes):
    # Excel's "Unicode Text" export is UTF-16 with a BOM. utf-8-sig would
    # "succeed" on it anyway (a NUL byte plus an ASCII byte are each valid
    # UTF-8 on their own), silently wedging a NUL between every character,
    # so UTF-16 BOMs must be checked before the utf-8 fallbacks run.
    if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw_bytes.decode("utf-16")

    # CSVs exported from Excel on Windows are commonly Latin-1/cp1252, not UTF-8
    # (very common with accented Portuguese names), so try a few encodings.
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def parse_recipients(file_storage, text_blob):
    content = None
    if file_storage and file_storage.filename:
        content = decode_upload(file_storage.read())
    elif text_blob and text_blob.strip():
        content = text_blob
    if not content:
        return []

    content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line for line in content.splitlines() if line.strip()]
    if not lines:
        return []

    # pt-BR locale Excel exports CSV with ";" (comma is the decimal separator
    # there), so sniff the delimiter instead of assuming ",".
    delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","
    first_cell = lines[0].split(delimiter)[0].strip().strip('"').lower()

    if first_cell == "email":
        rows = []
        for row in csv.DictReader(io.StringIO(content), delimiter=delimiter):
            email = row.get("email", "").strip().strip(";").strip()
            if not email:
                continue
            row["email"] = email
            rows.append(row)
        return rows

    # No "email" header: treat every line as one or more recipients. Handles
    # a plain list typed/pasted by hand, including Outlook-style lists where
    # multiple addresses on one line are separated by ";" (e.g. copy-pasted
    # from an Outlook "To:" field), with an optional ",Name" per address.
    rows = []
    for line in lines:
        for chunk in line.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = [p.strip() for p in next(csv.reader([chunk])) if p.strip()]
            if not parts or "@" not in parts[0]:
                continue
            row = {"email": parts[0]}
            if len(parts) > 1:
                row["name"] = parts[1]
            rows.append(row)
    return rows


def compose_html(message_text, image_names, from_name, from_email):
    # Everything interpolated here is user/CSV text, so it is escaped: a name
    # containing "&" or "<" would otherwise mangle the message body.
    paragraphs = "".join(
        f'<p style="color:#444444;font-size:15px;line-height:1.6;margin:0 0 16px 0;">'
        f'{html.escape(p).replace(chr(10), "<br />")}</p>'
        for p in message_text.strip().split("\n\n")
        if p.strip()
    )
    from_name = html.escape(from_name)
    from_email = html.escape(from_email)
    images_html = "".join(
        f'<img src="cid:{html.escape(name, quote=True)}" alt="" width="536" '
        f'style="display:block;margin:0 auto 16px auto;max-width:100%;border-radius:6px;" />'
        for name in image_names
    )
    return f"""<!DOCTYPE html>
<html>
  <body style="margin:0;padding:0;background-color:#f4f4f4;font-family:Arial,Helvetica,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f4;padding:24px 0;">
      <tr><td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:8px;overflow:hidden;">
          <tr><td style="padding:32px;">{paragraphs}{images_html}</td></tr>
          <tr><td style="padding:16px 32px;background-color:#fafafa;text-align:center;">
            <p style="color:#999999;font-size:12px;margin:0;">Enviado por {from_name} &middot; {from_email}</p>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


@app.route("/", methods=["GET"])
def index():
    # Pre-fill the connection/sender fields from .env so the operator only
    # types the message. The password is deliberately NOT sent to the browser;
    # _send() falls back to SMTP_PASSWORD when the field is left blank.
    return render_template(
        "index.html",
        defaults={
            "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
            "port": os.getenv("SMTP_PORT", "587"),
            "use_ssl": os.getenv("SMTP_USE_SSL", "false").strip().lower() in ("1", "true", "yes"),
            "user": os.getenv("SMTP_USER", ""),
            "auth_method": os.getenv("SMTP_AUTH_METHOD", "password"),
            "has_env_password": bool(os.getenv("SMTP_PASSWORD")),
            "from_name": os.getenv("FROM_NAME", ""),
            "from_email": os.getenv("FROM_EMAIL", ""),
            "reply_to": os.getenv("REPLY_TO", ""),
            "delay": os.getenv("SEND_DELAY_SECONDS", "2"),
        },
    )


@app.route("/send", methods=["POST"])
def send():
    try:
        return _send()
    except Exception as exc:
        return render_template("result.html", error=f"Erro inesperado: {friendly_smtp_error(exc)}", results=[])


def _send():
    f = request.form
    try:
        cfg = {
            "host": f["smtp_host"].strip(),
            "port": int(f["smtp_port"]),
            "use_ssl": f.get("smtp_use_ssl") == "on",
            "user": f["smtp_user"].strip(),
            "password": f.get("smtp_password", "") or os.getenv("SMTP_PASSWORD", ""),
            "auth_method": f.get("smtp_auth_method", "password"),
            "oauth_client_id": os.getenv("MS_OAUTH_CLIENT_ID", ""),
            "oauth_tenant_id": os.getenv("MS_OAUTH_TENANT_ID", "organizations"),
            "from_name": f.get("from_name", "").strip(),
            "from_email": (f.get("from_email", "").strip() or f["smtp_user"].strip()),
            "reply_to": f.get("reply_to", "").strip(),
            "delay": float(f.get("delay") or 2),
            "chunk_size": int(f.get("chunk_size") or 0),
            "chunk_pause": float(f.get("chunk_pause") or 0),
        }
    except (KeyError, ValueError) as exc:
        return render_template("result.html", error=f"Configuracao invalida: {exc}", results=[])

    dry_run = f.get("dry_run") == "on"
    if not dry_run:
        required_fields = [
            ("Servidor SMTP", cfg["host"]),
            ("Seu e-mail (usuario)", cfg["user"]),
        ]
        if cfg["auth_method"] == "oauth2":
            required_fields.append(("Client ID OAuth2 (MS_OAUTH_CLIENT_ID no .env do servidor)", cfg["oauth_client_id"]))
        else:
            required_fields.append(("Sua senha", cfg["password"]))
        missing = [label for label, val in required_fields if not val]
        if missing:
            return render_template(
                "result.html",
                error=f"Preencha os campos obrigatorios: {', '.join(missing)}.",
                results=[],
            )

    test_email = f.get("test_email", "").strip()

    recipients = parse_recipients(request.files.get("recipients_file"), f.get("recipients_text"))
    if test_email:
        recipients = [{"email": test_email, "name": "Teste"}]
    if not recipients:
        return render_template("result.html", error="Nenhum destinatario informado.", results=[])

    recipients, invalid, duplicates = dedupe_recipients(recipients)
    notices = []
    if invalid:
        notices.append(f"{len(invalid)} endereco(s) invalido(s) ignorado(s): {', '.join(invalid[:10])}")
    if duplicates:
        notices.append(f"{len(duplicates)} endereco(s) repetido(s) ignorado(s): {', '.join(duplicates[:10])}")

    recipients, suppressed = filter_suppressed(recipients)
    if suppressed:
        notices.append(
            f"{len(suppressed)} endereco(s) suprimido(s) (bounce definitivo/descadastro anterior): "
            f"{', '.join(suppressed[:10])}"
        )

    if not recipients:
        return render_template(
            "result.html",
            error="Nenhum destinatario valido. " + " ".join(notices),
            results=[],
        )

    # The password stays in memory for this worker only: campaign.json is
    # written to disk, so it must never carry credentials.
    password = cfg.pop("password")
    delay = cfg.pop("delay")
    chunk_size = cfg.pop("chunk_size")
    chunk_pause = cfg.pop("chunk_pause")
    campaign = {
        "cfg": cfg,
        "subject": f["subject"],
        "message": f["message"],
        "delay": delay,
        "chunk_size": chunk_size,
        "chunk_pause": chunk_pause,
        "throttle_backoff": float(os.environ.get("THROTTLE_BACKOFF_SECONDS", 300)),
        "max_throttle_retries": int(os.environ.get("MAX_THROTTLE_RETRIES", 2)),
        "dry_run": dry_run,
        "recipients": recipients,
        "images": {},
        "notices": notices,
        "created": datetime.now(timezone.utc).isoformat(),
    }

    run_id, run_dir = jobs.create_run(RUNS_DIR, campaign)
    used_cids = set()
    for file in request.files.getlist("images"):
        if file and file.filename:
            name = secure_filename(file.filename) or "image"
            file.save(run_dir / "images" / name)
            campaign["images"][sanitize_cid(Path(name).stem, used_cids)] = name
    jobs.write_campaign(run_dir, campaign)

    jobs.start(run_dir, run_id, password, send_one, smtp_connect, friendly_smtp_error,
               classify_smtp_error, suppression.add)
    return redirect(url_for("run_page", run_id=run_id))


def smtp_connect(cfg, password):
    if cfg["use_ssl"]:
        smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30)
    else:
        smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        smtp.starttls()
    if cfg.get("auth_method") == "oauth2":
        token = msoauth.get_access_token(cfg["oauth_client_id"], cfg["oauth_tenant_id"], cfg["user"])
        msoauth.smtp_xoauth2_login(smtp, cfg["user"], token)
    else:
        smtp.login(cfg["user"], password)
    return smtp


def send_one(campaign, row, images, smtp):
    cfg = campaign["cfg"]
    email = row["email"].strip()
    # Reject malformed addresses locally: a stray ';' or space left by an Excel
    # export otherwise reaches the server, which answers with a protocol error
    # and, on Exchange, may drop the whole connection.
    if not EMAIL_RE.match(email):
        raise ValueError("Endereco invalido (confira se nao sobrou ';' ou espaco no fim).")
    fields = {**row, "from_name": cfg["from_name"], "from_email": cfg["from_email"]}
    subject = render(campaign["subject"], fields)
    text_body = render(campaign["message"], fields)
    html_body = compose_html(text_body, list(images.keys()), cfg["from_name"], cfg["from_email"])
    msg = build_message(cfg, subject, text_body, html_body, images, email)
    smtp.send_message(msg)


def _run_dir(run_id):
    """Resolve a run id to its directory, refusing anything outside RUNS_DIR."""
    run_dir = (RUNS_DIR / run_id).resolve()
    if run_dir.parent != RUNS_DIR.resolve() or not run_dir.is_dir():
        return None
    return run_dir


@app.route("/run/<run_id>")
def run_page(run_id):
    run_dir = _run_dir(run_id)
    if run_dir is None:
        return render_template("result.html", error="Envio nao encontrado.", results=[]), 404
    return render_template("run.html", status=jobs.status(run_dir, run_id))


@app.route("/run/<run_id>/status")
def run_status(run_id):
    run_dir = _run_dir(run_id)
    if run_dir is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(jobs.status(run_dir, run_id))


@app.route("/run/<run_id>/log.csv")
def run_log(run_id):
    run_dir = _run_dir(run_id)
    if run_dir is None or not (run_dir / "log.csv").exists():
        return render_template("result.html", error="Log nao encontrado.", results=[]), 404
    return send_file(
        run_dir / "log.csv",
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"envio-{run_id}.csv",
    )


@app.route("/run/<run_id>/cancel", methods=["POST"])
def run_cancel(run_id):
    if _run_dir(run_id) is None:
        return render_template("result.html", error="Envio nao encontrado.", results=[]), 404
    jobs.cancel(run_id)
    return redirect(url_for("run_page", run_id=run_id))


@app.route("/run/<run_id>/resume", methods=["POST"])
def run_resume(run_id):
    """Restart a stopped run, skipping recipients already logged as sent.

    Only the SMTP password is re-submitted: it is deliberately absent from the
    saved campaign, so it has to come from the operator again.
    """
    run_dir = _run_dir(run_id)
    if run_dir is None:
        return render_template("result.html", error="Envio nao encontrado.", results=[]), 404

    current = jobs.status(run_dir, run_id)
    if current["state"] == "running":
        return redirect(url_for("run_page", run_id=run_id))

    password = request.form.get("smtp_password", "")
    campaign = jobs.read_campaign(run_dir)
    needs_password = campaign["cfg"].get("auth_method") != "oauth2"
    if not campaign.get("dry_run") and needs_password and not password:
        return render_template(
            "result.html", error="Informe a senha SMTP para continuar o envio.", results=[]
        )

    jobs.start(run_dir, run_id, password, send_one, smtp_connect, friendly_smtp_error,
               classify_smtp_error, suppression.add, resume=True)
    return redirect(url_for("run_page", run_id=run_id))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="127.0.0.1", port=port, debug=False)
