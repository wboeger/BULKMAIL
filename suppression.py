"""Persistent do-not-mail list shared by the CLI and the web UI.

Both front ends send from the same mailbox, so reputation is shared too: a
hard bounce (permanently invalid address) or an unsubscribe request from a
CLI campaign must stay suppressed on every future web-UI campaign, and vice
versa. Repeatedly hitting addresses a provider already told us are bad is
itself a signal providers use to decide whether to throttle or restrict a
sending account -- this file is the single biggest lever on that after
authentication (SPF/DKIM/DMARC) and staying under the provider's own rate
limits.

Entries never expire and are never re-tried automatically; removing a false
positive means editing/deleting its row by hand.
"""
import csv
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUPPRESSION_PATH = ROOT / "suppressed.csv"
FIELDS = ["email", "reason", "added"]


def load(path=None):
    """Return {lowercased email: reason} for every suppressed address."""
    path = Path(path) if path is not None else SUPPRESSION_PATH
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {
            row["email"].strip().lower(): row.get("reason", "")
            for row in csv.DictReader(f)
            if row.get("email")
        }


def add(email, reason, path=None):
    """Append email to the suppression list, once, if not already present."""
    path = Path(path) if path is not None else SUPPRESSION_PATH
    email = (email or "").strip().lower()
    if not email or email in load(path):
        return
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(FIELDS)
        writer.writerow([email, reason, datetime.now(timezone.utc).isoformat()])
