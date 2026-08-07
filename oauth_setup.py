#!/usr/bin/env python3
"""One-time interactive login for Microsoft 365 OAuth2 SMTP.

Run this once (and again whenever the cached token stops working) to
authorize this tool against your mailbox. It opens a browser for you to
sign in, then caches a refresh token in .oauth_cache.json (gitignored).

After this, send_bulk_mail.py and webapp/app.py read that cache silently --
they never prompt for interactive login themselves.

Usage:
    python3 oauth_setup.py
    python3 oauth_setup.py --device-code   # no local browser available
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

import msoauth

ROOT = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device-code", action="store_true", help="Use device-code flow instead of opening a local browser")
    args = p.parse_args()

    load_dotenv(ROOT / ".env")
    client_id = os.getenv("MS_OAUTH_CLIENT_ID")
    tenant_id = os.getenv("MS_OAUTH_TENANT_ID", "organizations")
    user = os.getenv("SMTP_USER")

    if not client_id:
        sys.exit(
            "MS_OAUTH_CLIENT_ID nao definido no .env.\n"
            "Registre um app publico no Entra ID/Azure AD (veja README.md) e cole o Application (client) ID."
        )
    if not user:
        sys.exit("SMTP_USER nao definido no .env (e a caixa de correio que sera autorizada).")

    cache = msoauth.load_token_cache()
    app = msoauth.build_msal_app(client_id, tenant_id, cache=cache)

    if args.device_code:
        flow = app.initiate_device_flow(scopes=msoauth.SCOPES)
        if "user_code" not in flow:
            sys.exit(f"Falha ao iniciar device-code flow: {flow.get('error_description', flow)}")
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    else:
        print("Abrindo navegador para login... (use --device-code se nao houver navegador disponivel)")
        result = app.acquire_token_interactive(scopes=msoauth.SCOPES, login_hint=user)

    if not result or "access_token" not in result:
        error = (result or {}).get("error_description", result)
        sys.exit(f"Login OAuth2 falhou: {error}")

    msoauth.save_token_cache(cache)
    account = result.get("id_token_claims", {}).get("preferred_username", user)
    print(f"Login OAuth2 concluido para {account}. Cache salvo em {msoauth.TOKEN_CACHE_PATH}.")
    print("Mantenha esse arquivo local -- nao o commite (ja esta no .gitignore).")


if __name__ == "__main__":
    main()
