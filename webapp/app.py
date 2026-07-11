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
import io
import os
import shutil
import smtplib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from send_bulk_mail import build_message, render, sanitize_cid  # noqa: E402

UPLOAD_DIR = ROOT / "webapp" / "_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def friendly_smtp_error(exc):
    text = str(exc)
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

    lines = [line for line in content.splitlines() if line.strip()]
    if not lines:
        return []
    first_cell = lines[0].split(",")[0].strip().strip('"').lower()

    if first_cell == "email":
        reader = csv.DictReader(io.StringIO(content))
        return [row for row in reader if row.get("email", "").strip()]

    # No "email" header: treat every line as one recipient, "email" or
    # "email,name,..." -- handles a plain list of addresses typed by hand.
    rows = []
    for parts in csv.reader(io.StringIO(content)):
        parts = [p.strip() for p in parts if p.strip()]
        if not parts or "@" not in parts[0]:
            continue
        row = {"email": parts[0]}
        if len(parts) > 1:
            row["name"] = parts[1]
        rows.append(row)
    return rows


def compose_html(message_text, image_names, from_name, from_email):
    paragraphs = "".join(
        f'<p style="color:#444444;font-size:15px;line-height:1.6;margin:0 0 16px 0;">{p}</p>'
        for p in message_text.strip().split("\n\n")
        if p.strip()
    )
    images_html = "".join(
        f'<img src="cid:{name}" alt="" width="536" '
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
    return render_template("index.html")


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
            "password": f["smtp_password"],
            "from_name": f.get("from_name", "").strip(),
            "from_email": (f.get("from_email", "").strip() or f["smtp_user"].strip()),
            "reply_to": f.get("reply_to", "").strip(),
            "delay": float(f.get("delay") or 2),
        }
    except (KeyError, ValueError) as exc:
        return render_template("result.html", error=f"Configuracao invalida: {exc}", results=[])

    dry_run = f.get("dry_run") == "on"
    if not dry_run:
        missing = [
            label for label, val in [
                ("Servidor SMTP", cfg["host"]),
                ("Seu e-mail (usuario)", cfg["user"]),
                ("Sua senha", cfg["password"]),
            ] if not val
        ]
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

    run_dir = UPLOAD_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    images = {}
    used_cids = set()
    try:
        for file in request.files.getlist("images"):
            if file and file.filename:
                dest = run_dir / file.filename
                file.save(dest)
                images[sanitize_cid(Path(file.filename).stem, used_cids)] = dest

        subject_template = f["subject"]
        message_text = f["message"]
        results = []
        smtp = None

        if not dry_run:
            try:
                if cfg["use_ssl"]:
                    smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30)
                else:
                    smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
                    smtp.starttls()
                smtp.login(cfg["user"], cfg["password"])
            except Exception as exc:
                return render_template("result.html", error=f"Falha ao conectar/autenticar no SMTP: {friendly_smtp_error(exc)}", results=[])

        try:
            for i, row in enumerate(recipients):
                email = row["email"].strip()
                fields = {**row, "from_name": cfg["from_name"], "from_email": cfg["from_email"]}
                subject = render(subject_template, fields)
                text_body = render(message_text, fields)
                html_body = compose_html(text_body, list(images.keys()), cfg["from_name"], cfg["from_email"])
                try:
                    if dry_run:
                        results.append({"email": email, "status": "dry-run", "error": ""})
                    else:
                        msg = build_message(cfg, subject, text_body, html_body, images, email)
                        smtp.send_message(msg)
                        results.append({"email": email, "status": "enviado", "error": ""})
                        if cfg["delay"] > 0 and i < len(recipients) - 1:
                            time.sleep(cfg["delay"])
                except Exception as exc:
                    results.append({"email": email, "status": "falhou", "error": friendly_smtp_error(exc)})
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    pass  # connection may already be closed by the server; results are what matter
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

    return render_template("result.html", results=results, error=None)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="127.0.0.1", port=port, debug=False)
