#!/usr/bin/env python3
"""
Shared Garmin Connect client + tiny helpers used by all the pull scripts.

Auth model (garminconnect token caching):
  - First run ever: set GARMIN_EMAIL / GARMIN_PASSWORD env vars and run any
    pull script; it logs in, prompts for your MFA code, and caches a token to
    ~/.garmin_tokens.
  - Every run after: the cached token is reused. No login, no MFA, no creds
    needed in the environment.

Nothing here stores or logs your credentials; they're read from the env only
on the first login and handed straight to the library.
"""
import os
import time
from garminconnect import Garmin

TOKEN_DIR = os.path.expanduser("~/.garmin_tokens")


def _ask_mfa():
    return input("Enter the MFA code Garmin just sent you: ").strip()


def get_client():
    """Return a logged-in Garmin client, reusing the cached token if present."""
    try:
        g = Garmin()
        g.login(tokenstore=TOKEN_DIR)
        print("Reused cached token — no login needed.")
        return g
    except Exception as e:
        email = os.environ.get("GARMIN_EMAIL")
        pw = os.environ.get("GARMIN_PASSWORD")
        if not (email and pw):
            raise SystemExit(
                "No cached token and GARMIN_EMAIL / GARMIN_PASSWORD not set.\n"
                "Set them and run once to log in (you'll be asked for your MFA code):\n"
                '  set GARMIN_EMAIL=you@example.com  (PowerShell: $env:GARMIN_EMAIL="...")\n'
                '  set GARMIN_PASSWORD=...\n'
                f"(underlying error: {e})"
            )
        print("No cached token; logging in fresh...")
        g = Garmin(email, pw, prompt_mfa=_ask_mfa)
        g.login(tokenstore=TOKEN_DIR)
        print(f"Logged in; token cached to {TOKEN_DIR}")
        return g


def safe(fn, *args, **kwargs):
    """Call an API method; return an {_error} marker instead of raising.

    Many endpoints legitimately have no data for a given day/activity (feature
    not supported by the device, no wear that day, etc.). We record the error
    so the raw file is still written and the day isn't retried forever.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        return {"_error": str(e)}


def polite_sleep(seconds=0.7):
    """Throttle between calls so we don't trip Garmin's 429 rate limits."""
    time.sleep(seconds)
