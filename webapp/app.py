#!/usr/bin/env python3
"""Simple local web UI for the bulk mail sender.

Run with:
    python3 webapp/app.py
Then open http://127.0.0.1:5000 in your browser.

Nothing here is exposed to the internet by default -- it only listens on
localhost. Your SMTP password is used in-memory for the request and is
never written to disk.
"""
import csv
import io
import shutil
import smtplib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from send_bulk_mail import build_message, render  # noqa: E402

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


def parse_recipients(file_storage, text_blob):
    content = None
    if file_storage and file_storage.filename:
        content = file_storage.read().decode("utf-8")
    elif text_blob and text_blob.strip():
        content = text_blob
    if not content:
        return []
    reader = csv.DictReader(io.StringIO(content))
    return [row for row in reader if row.get("email")]


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
    test_email = f.get("test_email", "").strip()

    recipients = parse_recipients(request.files.get("recipients_file"), f.get("recipients_text"))
    if test_email:
        recipients = [{"email": test_email, "name": "Teste"}]
    if not recipients:
        return render_template("result.html", error="Nenhum destinatario informado.", results=[])

    run_dir = UPLOAD_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    images = {}
    try:
        for file in request.files.getlist("images"):
            if file and file.filename:
                stem = Path(file.filename).stem
                dest = run_dir / file.filename
                file.save(dest)
                images[stem] = dest

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
                smtp.quit()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

    return render_template("result.html", results=results, error=None)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
