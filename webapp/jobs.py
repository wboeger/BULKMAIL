"""Background send jobs for the web UI.

A send of a few hundred recipients with a delay between each one takes far
longer than a browser is willing to wait for a response, so /send starts a
worker thread and redirects to a progress page instead of blocking.

Each run gets a directory under webapp/_runs/<run_id>/ holding:

    campaign.json  what to send, to whom (never the SMTP password)
    log.csv        one row per recipient, appended as it happens
    images/        inline images, kept so a run can be resumed

The log on disk -- not the in-memory job -- is the source of truth, so the
progress page still works after the server restarts, and resuming a partial
run means replaying the recipients that have no "enviado" row yet.
"""
import csv
import json
import smtplib
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG_FIELDS = ["timestamp", "email", "status", "error"]

_jobs = {}
_jobs_lock = threading.Lock()


class Job:
    """In-memory handle on a running worker. Absent once the process restarts."""

    def __init__(self, run_id):
        self.run_id = run_id
        self.state = "running"  # running | done | cancelled | error
        self.error = ""
        self.cancel = threading.Event()


def _now():
    return datetime.now(timezone.utc)


def create_run(runs_dir, campaign):
    run_id = _now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = runs_dir / run_id
    (run_dir / "images").mkdir(parents=True, exist_ok=True)
    write_campaign(run_dir, campaign)
    return run_id, run_dir


def write_campaign(run_dir, campaign):
    (run_dir / "campaign.json").write_text(
        json.dumps(campaign, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_campaign(run_dir):
    path = run_dir / "campaign.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def read_log(run_dir):
    path = run_dir / "log.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _append_log(run_dir, email, status, error):
    path = run_dir / "log.csv"
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(LOG_FIELDS)
        writer.writerow([_now().isoformat(), email, status, error])


def sent_addresses(run_dir):
    return {
        row["email"].strip().lower()
        for row in read_log(run_dir)
        if row.get("status") in ("enviado", "dry-run")
    }


def get_job(run_id):
    with _jobs_lock:
        return _jobs.get(run_id)


def cancel(run_id):
    job = get_job(run_id)
    if job and job.state == "running":
        job.cancel.set()
        return True
    return False


def status(run_dir, run_id):
    """Progress for a run, reconstructed from disk plus any live job."""
    campaign = read_campaign(run_dir)
    if campaign is None:
        return None
    rows = read_log(run_dir)
    counts = {"enviado": 0, "falhou": 0, "dry-run": 0}
    for row in rows:
        if row.get("status") in counts:
            counts[row["status"]] += 1

    job = get_job(run_id)
    if job is not None:
        state, error = job.state, job.error
    elif len(rows) >= len(campaign["recipients"]):
        state, error = "done", ""
    else:
        # No worker and an unfinished log: the server restarted mid-run.
        state, error = "interrupted", ""

    total = len(campaign["recipients"])
    return {
        "run_id": run_id,
        "state": state,
        "error": error,
        "total": total,
        "done": len(rows),
        "counts": counts,
        "subject": campaign.get("subject", ""),
        "dry_run": campaign.get("dry_run", False),
        "auth_method": campaign["cfg"].get("auth_method", "password"),
        "delay": campaign.get("delay") or 0,
        "chunk_size": campaign.get("chunk_size") or 0,
        "chunk_pause": campaign.get("chunk_pause") or 0,
        "notices": campaign.get("notices", []),
        "remaining": max(total - len(rows), 0),
        "rows": rows,
    }


def start(run_dir, run_id, password, send_one, connect, friendly_error, classify_error, record_bounce, resume=False):
    """Spawn the worker thread for a run and register its Job.

    send_one(campaign, row, images, smtp) does the actual per-recipient work;
    connect(cfg, password) returns a logged-in smtplib connection;
    classify_error(exc) sorts a failure into 'throttled' / 'hard_bounce' /
    'transient' / 'other'; record_bounce(email, reason) persists a hard
    bounce to the shared do-not-mail list. All four are injected so this
    module stays free of message-building and SMTP-classification details.
    """
    campaign = read_campaign(run_dir)
    job = Job(run_id)
    with _jobs_lock:
        _jobs[run_id] = job

    thread = threading.Thread(
        target=_run,
        args=(job, run_dir, campaign, password, send_one, connect, friendly_error,
              classify_error, record_bounce, resume),
        daemon=True,
    )
    thread.start()
    return job


def _run(job, run_dir, campaign, password, send_one, connect, friendly_error,
          classify_error, record_bounce, resume):
    skip = sent_addresses(run_dir) if resume else set()
    recipients = [r for r in campaign["recipients"] if r["email"].lower() not in skip]
    images = {
        cid: run_dir / "images" / fname
        for cid, fname in campaign.get("images", {}).items()
    }
    delay = float(campaign.get("delay") or 0)
    chunk_size = int(campaign.get("chunk_size") or 0)
    chunk_pause = float(campaign.get("chunk_pause") or 0)
    throttle_backoff = float(campaign.get("throttle_backoff") or 300)
    max_throttle_retries = int(campaign.get("max_throttle_retries") or 2)
    dry_run = campaign.get("dry_run", False)

    smtp = None
    try:
        if not dry_run:
            try:
                smtp = connect(campaign["cfg"], password)
            except Exception as exc:
                job.state = "error"
                job.error = f"Falha ao conectar/autenticar no SMTP: {friendly_error(exc)}"
                return

        for i, row in enumerate(recipients):
            if job.cancel.is_set():
                job.state = "cancelled"
                return
            email = row["email"].strip()
            throttle_attempt = 0
            while True:
                try:
                    if dry_run:
                        _append_log(run_dir, email, "dry-run", "")
                    else:
                        send_one(campaign, row, images, smtp)
                        _append_log(run_dir, email, "enviado", "")
                    break
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
                    # The connection dropped mid-list; reconnect once so the rest of
                    # the list does not fail one by one against a dead socket.
                    _append_log(run_dir, email, "falhou", friendly_error(exc))
                    try:
                        smtp = connect(campaign["cfg"], password)
                    except Exception as reconnect_exc:
                        job.state = "error"
                        job.error = f"Conexao SMTP perdida e reconexao falhou: {friendly_error(reconnect_exc)}"
                        return
                    break
                except Exception as exc:
                    kind = classify_error(exc)
                    if kind == "throttled" and throttle_attempt < max_throttle_retries:
                        throttle_attempt += 1
                        wait = throttle_backoff * throttle_attempt
                        if job.cancel.wait(wait):
                            job.state = "cancelled"
                            return
                        continue
                    if kind == "throttled":
                        _append_log(run_dir, email, "falhou", friendly_error(exc))
                        job.state = "error"
                        job.error = (
                            "O provedor continua bloqueando o envio apos espera. "
                            "Campanha interrompida para nao piorar o bloqueio -- "
                            "veja README 'Getting unblocked'. " + friendly_error(exc)
                        )
                        return
                    if kind == "hard_bounce":
                        record_bounce(email, friendly_error(exc))
                    _append_log(run_dir, email, "falhou", friendly_error(exc))
                    break

            if not dry_run and i < len(recipients) - 1:
                # End of a chunk: pause chunk_pause instead of the regular
                # per-message delay, then start the next chunk fresh.
                at_chunk_boundary = chunk_size > 0 and (i + 1) % chunk_size == 0
                pause = chunk_pause if at_chunk_boundary else delay
                if pause > 0 and job.cancel.wait(pause):
                    job.state = "cancelled"
                    return
        job.state = "done"
    except Exception as exc:
        job.state = "error"
        job.error = friendly_error(exc)
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass  # server may have closed it already; the log is what matters


def cleanup_old_runs(runs_dir, days=7):
    """Delete run directories older than `days` (they hold recipient lists)."""
    import shutil

    cutoff = _now() - timedelta(days=days)
    if not runs_dir.is_dir():
        return
    for path in runs_dir.iterdir():
        if not path.is_dir():
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)
