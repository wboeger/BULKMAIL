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
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from dotenv import load_dotenv

import msoauth
import suppression

ROOT = Path(__file__).resolve().parent
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(text, fields):
    return PLACEHOLDER_RE.sub(lambda m: str(fields.get(m.group(1), "")), text)


def sanitize_header(value):
    """Collapse CR/LF out of a value destined for an email header.

    Merge fields come from a CSV the sender may not have written, and a cell
    containing a newline followed by e.g. "Bcc: someone@else" would otherwise
    be injected as an extra header.
    """
    return " ".join(str(value).replace("\r", "\n").split("\n")).strip()


def normalize_password(value):
    """Strip every whitespace character out of an SMTP password.

    Gmail displays App Passwords as "abcd efgh ijkl mnop" and the copy button
    hands over U+00A0 (non-breaking space), not a plain space. smtplib encodes
    credentials as ASCII, so a pasted password blows up with
    "'ascii' codec can't encode character '\\xa0'" long before authentication.
    Providers ignore the separators anyway, so drop all whitespace.
    """
    return "".join(str(value or "").split())


EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def valid_email(value):
    return bool(EMAIL_RE.match((value or "").strip()))


def dedupe_recipients(rows):
    """Drop rows with an invalid or repeated address (first occurrence wins).

    Returns (kept_rows, invalid_addresses, duplicate_addresses).
    """
    kept, invalid, duplicates = [], [], []
    seen = set()
    for row in rows:
        email = (row.get("email") or "").strip()
        if not valid_email(email):
            invalid.append(email)
            continue
        key = email.lower()
        if key in seen:
            duplicates.append(email)
            continue
        seen.add(key)
        kept.append({**row, "email": email})
    return kept, invalid, duplicates


def filter_suppressed(rows):
    """Drop rows whose address is on the persistent do-not-mail list.

    Returns (kept_rows, suppressed_addresses). Suppression (hard bounces,
    unsubscribes) is recorded once and honored by every future campaign on
    either front end -- see suppression.py.
    """
    blocked = suppression.load()
    if not blocked:
        return rows, []
    kept, dropped = [], []
    for row in rows:
        email = (row.get("email") or "").strip()
        if email.lower() in blocked:
            dropped.append(email)
        else:
            kept.append(row)
    return kept, dropped


def classify_smtp_error(exc):
    """Sort an SMTP failure into 'throttled' / 'hard_bounce' / 'transient' / 'other'.

    Getting this right matters more than any fixed per-message delay.
    Repeatedly hammering a mailbox after a throttle signal -- SMTP 4xx, or
    Microsoft's 550 5.7.708 "user is sending too much mail in a similar way
    to a known spam sender" -- is what turns a temporary slowdown into the
    account being flagged as compromised and restricted from sending
    entirely (see README "Getting unblocked"). Hard bounces (550 5.1.1 and
    friends) must stop being retried on every future campaign, since bounce
    rate is itself a reputation signal providers use to decide whether to
    throttle or restrict a sender.
    """
    code = getattr(exc, "smtp_code", None)
    raw = getattr(exc, "smtp_error", b"")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        code, raw = next(iter(exc.recipients.values()), (None, b""))
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    combined = f"{code} {text}".lower()
    if "5.7.708" in combined or "too much mail" in combined or "sending limits" in combined:
        return "throttled"
    if code is not None and 400 <= code < 500:
        return "transient"
    if code is not None and 500 <= code < 600:
        return "hard_bounce"
    return "other"


def smtp_connect(cfg):
    if cfg["use_ssl"]:
        smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30)
    else:
        smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        smtp.starttls()
    if cfg["auth_method"] == "oauth2":
        token = msoauth.get_access_token(cfg["oauth_client_id"], cfg["oauth_tenant_id"], cfg["user"])
        msoauth.smtp_xoauth2_login(smtp, cfg["user"], token)
    else:
        smtp.login(cfg["user"], cfg["password"])
    return smtp


def load_config():
    load_dotenv(ROOT / ".env")
    auth_method = os.getenv("SMTP_AUTH_METHOD", "password").lower()

    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER"]
    if auth_method == "oauth2":
        required.append("MS_OAUTH_CLIENT_ID")
    else:
        required.append("SMTP_PASSWORD")
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
        "password": normalize_password(os.getenv("SMTP_PASSWORD", "")),
        "auth_method": auth_method,
        "oauth_client_id": os.getenv("MS_OAUTH_CLIENT_ID", ""),
        "oauth_tenant_id": os.getenv("MS_OAUTH_TENANT_ID", "organizations"),
        "from_name": os.getenv("FROM_NAME", ""),
        "from_email": os.getenv("FROM_EMAIL", os.environ["SMTP_USER"]),
        "reply_to": os.getenv("REPLY_TO", ""),
        "delay": float(os.getenv("SEND_DELAY_SECONDS", "2")),
        "max_per_run": int(os.getenv("MAX_PER_RUN", "0")),
        "throttle_backoff_seconds": float(os.getenv("THROTTLE_BACKOFF_SECONDS", "300")),
        "max_throttle_retries": int(os.getenv("MAX_THROTTLE_RETRIES", "2")),
    }


def load_recipients(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"No recipients found in {path}")
    for i, row in enumerate(rows, start=2):
        if not row.get("email"):
            sys.exit(f"Row {i} in {path} is missing an 'email' value")

    rows, invalid, duplicates = dedupe_recipients(rows)
    if invalid:
        print(f"Skipping {len(invalid)} invalid address(es): {', '.join(invalid[:10])}"
              + (" ..." if len(invalid) > 10 else ""), file=sys.stderr)
    if duplicates:
        print(f"Skipping {len(duplicates)} duplicate address(es): {', '.join(duplicates[:10])}"
              + (" ..." if len(duplicates) > 10 else ""), file=sys.stderr)

    rows, suppressed = filter_suppressed(rows)
    if suppressed:
        print(f"Skipping {len(suppressed)} suppressed address(es) (prior hard bounce/unsubscribe): "
              f"{', '.join(suppressed[:10])}" + (" ..." if len(suppressed) > 10 else ""), file=sys.stderr)
    if not rows:
        sys.exit(f"No usable recipients left in {path}")
    return rows


def sanitize_cid(name, used):
    """Turn an arbitrary filename stem into a safe, unique Content-ID token.

    Content-ID values must not contain spaces or most punctuation (mail
    filenames like "Screenshot 2024-07-10 at 10.23.45.png" would otherwise
    produce an invalid cid that clients like Outlook can't match against
    the <img src="cid:..."> reference, causing the image to show up as a
    plain attachment instead of rendering inline).
    """
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "img"
    cid = slug
    n = 2
    while cid in used:
        cid = f"{slug}_{n}"
        n += 1
    used.add(cid)
    return cid


def load_images(images_dir):
    """Return {cid_name: Path} for every image file in images_dir."""
    images = {}
    used_cids = set()
    if not images_dir.is_dir():
        return images
    for p in sorted(images_dir.iterdir()):
        if p.is_file() and (mimetypes.guess_type(p.name)[0] or "").startswith("image/"):
            images[sanitize_cid(p.stem, used_cids)] = p
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
    # multipart/related > multipart/alternative(text, html) > inline images (siblings)
    # This ordering (related as the outermost container) is the structure Outlook /
    # Exchange render most reliably; images nested the other way around often show
    # up as attachments instead of inline in the body on those clients.
    # Every header value is sanitized: subjects carry rendered merge fields and
    # the recipient list is untrusted input, so a stray newline must not be able
    # to introduce a header of its own.
    from_name = sanitize_header(cfg["from_name"])
    from_email = sanitize_header(cfg["from_email"])

    root = MIMEMultipart("related")
    root["From"] = formataddr((from_name, from_email)) if from_name else from_email
    root["To"] = sanitize_header(to_email)
    root["Subject"] = sanitize_header(subject)
    if cfg["reply_to"]:
        root["Reply-To"] = sanitize_header(cfg["reply_to"])

    # Date and Message-ID are mandatory per RFC 5322. Relays usually patch them
    # in, but a message that arrives without them (or with one the relay had to
    # invent) scores worse with spam filters, so set them here. The Message-ID
    # domain is taken from the From address so it stays aligned with SPF/DKIM.
    root["Date"] = formatdate(localtime=True)
    root["Message-ID"] = make_msgid(domain=from_email.rpartition("@")[2] or None)

    # Gmail/Yahoo require a working unsubscribe path from bulk senders (their
    # 2024 sender guidelines) and weight its absence as a spam signal. A
    # mailto: is all a purely local mailer can offer -- true one-click
    # (RFC 8058) needs a public HTTPS endpoint, which this app doesn't have.
    unsub_addr = cfg["reply_to"] or from_email
    root["List-Unsubscribe"] = f"<mailto:{sanitize_header(unsub_addr)}?subject=unsubscribe>"

    alt = MIMEMultipart("alternative")
    root.attach(alt)
    alt.attach(MIMEText(text_body, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))

    for cid, path in images.items():
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            img = MIMEImage(f.read(), _subtype=subtype)
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=path.name)
        root.attach(img)

    return root


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
        smtp = smtp_connect(cfg)

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

                throttle_attempt = 0
                while True:
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
                        break
                    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
                        print(f"SMTP connection dropped ({exc}); reconnecting...", file=sys.stderr)
                        smtp = smtp_connect(cfg)
                        continue
                    except Exception as exc:
                        kind = classify_smtp_error(exc)
                        if kind == "throttled" and throttle_attempt < cfg["max_throttle_retries"]:
                            throttle_attempt += 1
                            wait = cfg["throttle_backoff_seconds"] * throttle_attempt
                            print(f"Provider signaled throttling ({exc}); backing off {wait:.0f}s "
                                  f"before retry {throttle_attempt}/{cfg['max_throttle_retries']}...",
                                  file=sys.stderr)
                            time.sleep(wait)
                            continue
                        if kind == "throttled":
                            logwriter.writerow([ts, email, "failed", str(exc)])
                            logf.flush()
                            fail_count += 1
                            print(f"\nStill throttled after {throttle_attempt} retries -- stopping "
                                  f"this run before it makes the block worse. Re-run with --resume "
                                  f"once the mailbox is sending normally again (see README "
                                  f"'Getting unblocked').", file=sys.stderr)
                            print(f"\nDone. sent={sent_count} failed={fail_count} "
                                  f"skipped(already sent)={skip_count}")
                            print(f"Log written to {log_path}")
                            sys.exit(1)
                        if kind == "hard_bounce":
                            suppression.add(email, str(exc))
                            print(f"Hard bounce for {email} (added to suppressed.csv): {exc}",
                                  file=sys.stderr)
                        else:
                            print(f"FAILED to send to {email}: {exc}", file=sys.stderr)
                        logwriter.writerow([ts, email, "failed", str(exc)])
                        logf.flush()
                        fail_count += 1
                        break

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
