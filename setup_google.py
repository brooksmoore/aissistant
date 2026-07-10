"""One-time Google authorization for Calendar + Gmail (read-only).

Prereq: google_credentials.json in this folder (README.md step 6 explains how to get it).
Run:  python setup_google.py
A browser window opens — SHE logs in with her PERSONAL Gmail account and clicks Allow.
This saves google_token.json; the bot picks it up automatically on next restart."""
from google_auth_oauthlib.flow import InstalledAppFlow

from config import GOOGLE_CREDS, GOOGLE_TOKEN
from gcal import SCOPES


def main():
    if not GOOGLE_CREDS.exists():
        raise SystemExit(
            f"Missing {GOOGLE_CREDS.name}.\n"
            "Follow README.md step 6 to download it from Google Cloud Console, "
            "put it in this folder, then run this again."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CREDS), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    GOOGLE_TOKEN.write_text(creds.to_json())
    print("\n✅ Google connected! Calendar + inbox watching will be active next time the bot starts.")
    print("   (Restart it now: bash run.sh)")


if __name__ == "__main__":
    main()
