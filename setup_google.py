"""One-time Google authorization for Calendar + Gmail (read-only).

Prereq: instances/<name>/google_credentials.json (SETUP.md step 6 explains how to get it).
Run:  AISSISTANT_INSTANCE=<name> ./venv/bin/python setup_google.py
A browser window opens — the OWNER of that instance signs in with their own personal
Google account and clicks Allow. Nobody else's login will work: the token is minted for
whoever authorizes here, and it is what the bot reads that account's calendar and mail with.
This saves google_token.json into the instance directory; the bot picks it up on restart.

Re-run this any time the token dies (revoked, expired, or password changed) — the symptom
is `invalid_grant` in the log and an UNREADABLE calendar in the bot's replies."""
from google_auth_oauthlib.flow import InstalledAppFlow

from config import GOOGLE_CREDS, GOOGLE_TOKEN
from gcal import SCOPES


def main():
    if not GOOGLE_CREDS.exists():
        raise SystemExit(
            f"Missing {GOOGLE_CREDS}.\n"
            "Follow SETUP.md step 6 to download it from Google Cloud Console, "
            "put it at that path, then run this again."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CREDS), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    GOOGLE_TOKEN.write_text(creds.to_json())
    print("\n✅ Google connected! Calendar + inbox watching will be active next time the bot starts.")
    print("   (Restart it now: bash install_autostart.sh <instance>)")


if __name__ == "__main__":
    main()
