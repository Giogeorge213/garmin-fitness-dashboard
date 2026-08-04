"""
Garmin auth with token caching + MFA (garminconnect 0.3.6).
First run: logs in (prompts for MFA code), saves token to ~/.garmin_tokens.
Later runs: loads saved token — no login, no MFA, no 429.
"""
import os
from garminconnect import Garmin

TOKEN_DIR = os.path.expanduser("~/.garmin_tokens")

def _ask_mfa():
    return input("Enter the MFA code Garmin just sent you: ").strip()

def get_client():
    try:
        garmin = Garmin()
        garmin.login(tokenstore=TOKEN_DIR)
        print("Reused saved token — no login needed.")
        return garmin
    except Exception as e:
        print(f"No saved token ({e}); logging in fresh...")
        garmin = Garmin(
            os.environ["GARMIN_EMAIL"],
            os.environ["GARMIN_PASSWORD"],
            prompt_mfa=_ask_mfa,      # library calls this when MFA is needed
        )
        garmin.login(tokenstore=TOKEN_DIR)
        print(f"Logged in and saved token to {TOKEN_DIR}")
        return garmin

if __name__ == "__main__":
    g = get_client()
    print("OK —", g.get_full_name())
