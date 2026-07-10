#!/usr/bin/env python3
"""Send a text+image HTML email to a list of recipients via SMTP.

Usage:
    python send_bulk_mail.py --recipients recipients.csv --subject "Hello {{name}}" --dry-run
    python send_bulk_mail.py --recipients recipients.csv --subject "Hello {{name}}" --test-email you@yourcompany.com
    python send_bulk_mail.py --recipients recipients.csv --subject "Hello {{name}}"

See README.md for setup instructions.
"""
import argparse
import csv
import mimetypes
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(text, fields):
    return PLACEHOLDER_RE.sub(lambda m: str(fields.get(m.group(1), "")), text)


def load_config():
    load_dotenv(ROOT / ".env")
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        sys.exit(
            f"Missing required settings in .env: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in your credentials first."
        )
    return {
        "host": os.environ["SMTP_HOST"],
        "port": int(os.environ["SMTP_PORT"]),
        "use_ssl": os.getenv("SMTP_USE_SSL", "false").lower() == "true",
        "user": os.environ["SMTP_USER"],
        "password": os.environ["SMTP_PASSWORD"],
        "from_name": os.getenv("FROM_NAME", ""),
        "from_email": os.getenv("FROM_EMAIL", os.environ["SMTP_USER"]),
        "reply_to": os.getenv("REPLY_TO", ""),
        "delay": float(os.getenv("SEND_DELAY_SECONDS", "2")),
        "max_per_run": int(os.getenv("MAX_PER_RUN", "0")),
    }


def load_recipients(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"No recipients found in {path}")
    for i, row in enumerate(rows, start=2):
        if not row.get("email"):
            sys.exit(f"Row {i} in {path} is missing an 'email' value")
    return rows


def load_images(images_dir):
    """Return {cid_name: Path} for every image file in images_dir."""
    images = {}
    if not images_dir.is_dir():
        return images
    for p in sorted(images_dir.iterdir()):
        if p.is_file() and (mimetypes.guess_type(p.name)[0] or "").startswith("image/"):
            images[p.stem] = p
    return images


def already_sent(log_path):
    sent = set()
    if log_path.exists():
        with open(log_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "sent":
                    sent.add(row["email"].lower())
    return sent


def build_message(cfg, subject, text_body, html_body, images, to_email):
    msg = EmailMessage()
    from_header = f"{cfg['from_name']} <{cfg['from_email']}>" if cfg["from_name"] else cfg["from_email"]
    msg["From"] = from_header
    msg["To"] = to_email
    msg["Subject"] = subject
    if cfg["reply_to"]:
        msg["Reply-To"] = cfg["reply_to"]

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    # Attach images as inline (CID) parts on the HTML part so <img src="cid:name"> resolves.
    html_part = msg.get_payload()[1]
    for cid, path in images.items():
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            html_part.add_related(f.read(), maintype=maintype, subtype=subtype, cid=f"<{cid}>")

    return msg


def send_all(args):
    cfg = load_config()
    recipients = load_recipients(args.recipients)
    images = load_images(Path(args.images_dir))
    html_template = Path(args.html_template).read_text(encoding="utf-8")
    text_template = Path(args.text_template).read_text(encoding="utf-8")

    log_path = Path(args.log)
    skip = already_sent(log_path) if args.resume else set()
    log_is_new = not log_path.exists()

    if args.test_email:
        recipients = [{"email": args.test_email, "name": "Test", **recipients[0]}] if recipients else [
            {"email": args.test_email, "name": "Test"}
        ]

    if args.max_per_run is None:
        args.max_per_run = cfg["max_per_run"]
    if args.max_per_run and len(recipients) > args.max_per_run:
        recipients = recipients[: args.max_per_run]

    print(f"Loaded {len(recipients)} recipient(s), {len(images)} inline image(s): "
          f"{', '.join(images) or '(none)'}")

    smtp = None
    if not args.dry_run:
        if cfg["use_ssl"]:
            smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30)
        else:
            smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
            smtp.starttls()
        smtp.login(cfg["user"], cfg["password"])

    sent_count = fail_count = skip_count = 0
    try:
        with open(log_path, "a", newline="", encoding="utf-8") as logf:
            logwriter = csv.writer(logf)
            if log_is_new:
                logwriter.writerow(["timestamp", "email", "status", "error"])

            for row in recipients:
                email = row["email"].strip()
                if email.lower() in skip:
                    skip_count += 1
                    continue

                fields = {**row, "from_name": cfg["from_name"], "from_email": cfg["from_email"]}
                subject = render(args.subject, fields)
                text_body = render(text_template, fields)
                html_body = render(html_template, fields)

                ts = datetime.now(timezone.utc).isoformat()
                try:
                    if args.dry_run:
                        print(f"[DRY RUN] Would send to {email} | subject: {subject}")
                    else:
                        msg = build_message(cfg, subject, text_body, html_body, images, email)
                        smtp.send_message(msg)
                        print(f"Sent to {email}")
                    logwriter.writerow([ts, email, "sent", ""])
                    logf.flush()
                    sent_count += 1
                except Exception as exc:
                    print(f"FAILED to send to {email}: {exc}", file=sys.stderr)
                    logwriter.writerow([ts, email, "failed", str(exc)])
                    logf.flush()
                    fail_count += 1

                if not args.dry_run and cfg["delay"] > 0 and row is not recipients[-1]:
                    time.sleep(cfg["delay"])
    finally:
        if smtp is not None:
            smtp.quit()

    print(f"\nDone. sent={sent_count} failed={fail_count} skipped(already sent)={skip_count}")
    print(f"Log written to {log_path}")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recipients", default="recipients.csv", help="CSV file with an 'email' column (default: recipients.csv)")
    p.add_argument("--subject", required=True, help="Subject line, supports {{merge_fields}}")
    p.add_argument("--html-template", default="templates/email.html")
    p.add_argument("--text-template", default="templates/email.txt")
    p.add_argument("--images-dir", default="images")
    p.add_argument("--log", default="sent_log.csv")
    p.add_argument("--resume", action="store_true", help="Skip recipients already marked 'sent' in the log")
    p.add_argument("--dry-run", action="store_true", help="Render and print without sending anything")
    p.add_argument("--test-email", help="Send a single test message to this address instead of the full list")
    p.add_argument("--max-per-run", type=int, default=None, help="Cap number of emails sent this run (overrides MAX_PER_RUN in .env)")
    return p.parse_args()


if __name__ == "__main__":
    send_all(parse_args())
