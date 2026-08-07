"""Microsoft 365 / Office 365 OAuth2 (XOAUTH2) SMTP login.

Shared by send_bulk_mail.py and webapp/app.py the same way build_message()
etc. already are. Splitting this out keeps webapp/jobs.py free of MSAL/SMTP-
auth details -- it only ever calls the injected connect(cfg, password)
callable, unaware of whether that callable used a password or a token.

Token acquisition here is always silent (acquire_token_silent): a background
send job must never block waiting on interactive login. The one-time
interactive/device-code consent lives in oauth_setup.py instead.
"""
import base64
import json
import smtplib
from pathlib import Path

import msal

ROOT = Path(__file__).resolve().parent
TOKEN_CACHE_PATH = ROOT / ".oauth_cache.json"
SCOPES = ["https://outlook.office365.com/SMTP.Send"]


class OAuthTokenError(Exception):
    """No usable cached token -- oauth_setup.py needs to be (re)run."""


def load_token_cache():
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
    return cache


def save_token_cache(cache):
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.write_text(cache.serialize(), encoding="utf-8")


def build_msal_app(client_id, tenant_id, cache=None):
    return msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )


def get_access_token(client_id, tenant_id, user):
    """Silently acquire an access token for `user` from the local cache.

    Matches ONLY the exact account `user` was authorized for -- this mailer
    sends as an institutional identity to real recipient lists, so silently
    falling back to some other cached account (e.g. a second account cached
    during testing, or a stale entry) would send mail as the wrong sender
    with no visible error. Raises OAuthTokenError if there is no cached
    account for `user` or the cached refresh token no longer works -- the
    caller should tell the operator to run oauth_setup.py, not attempt an
    interactive login itself.
    """
    cache = load_token_cache()
    app = build_msal_app(client_id, tenant_id, cache=cache)

    account = next((a for a in app.get_accounts(username=user)), None)
    if account is None:
        cached = ", ".join(a.get("username", "?") for a in app.get_accounts()) or "nenhuma"
        raise OAuthTokenError(
            f"Nenhuma conta OAuth2 em cache para {user} (contas em cache: {cached}). "
            f"Rode: python3 oauth_setup.py"
        )

    result = app.acquire_token_silent(SCOPES, account=account)
    save_token_cache(cache)

    if not result or "access_token" not in result:
        error = (result or {}).get("error_description", "token expirado ou revogado")
        raise OAuthTokenError(
            f"Nao foi possivel renovar o login OAuth2 automaticamente ({error}). "
            f"Rode: python3 oauth_setup.py"
        )
    return result["access_token"]


def smtp_xoauth2_login(smtp, user, access_token):
    """AUTH XOAUTH2 handshake -- smtplib has no native support for it."""
    sasl = f"user={user}\x01auth=Bearer {access_token}\x01\x01"
    b64 = base64.b64encode(sasl.encode("utf-8")).decode("ascii")
    code, resp = smtp.docmd("AUTH", "XOAUTH2 " + b64)
    if code == 334:
        # Server sent a base64 JSON error blob and is waiting for us to
        # close out the failed attempt with an empty response before the
        # connection can be reused or quit cleanly.
        code, resp = smtp.docmd("", "")
    if code != 235:
        raise smtplib.SMTPAuthenticationError(code, resp)
