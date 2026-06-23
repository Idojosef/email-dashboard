#!/usr/bin/env python3
"""One-time setup: Authorize Gmail API access and get a refresh token.

Usage:
  1. Go to https://console.cloud.google.com
  2. Create a project (or select existing)
  3. Enable the Gmail API
  4. Create OAuth 2.0 credentials (Desktop app type)
  5. Download the credentials JSON file
  6. Run: python3 setup_gmail_oauth.py path/to/credentials.json
  7. A browser window opens — authorize with your Gmail account
  8. Copy the output values into your GitHub repo secrets
"""

import sys
import json

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Missing dependency. Run: pip3 install google-auth-oauthlib")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 setup_gmail_oauth.py <path/to/credentials.json>")
        print()
        print("Get credentials.json from Google Cloud Console:")
        print("  1. Go to https://console.cloud.google.com/apis/credentials")
        print("  2. Create OAuth 2.0 Client ID (Desktop app type)")
        print("  3. Download the JSON file")
        sys.exit(1)

    creds_file = sys.argv[1]

    # Read the credentials to show client_id
    with open(creds_file) as f:
        creds_data = json.load(f)

    key = "installed" if "installed" in creds_data else "web"
    client_id = creds_data[key]["client_id"]
    client_secret = creds_data[key]["client_secret"]

    print(f"Using client: {client_id[:30]}...")
    print("Opening browser for authorization...")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
    creds = flow.run_local_server(port=8080, prompt="consent")

    print()
    print("=" * 60)
    print("SUCCESS! Save these as GitHub Secrets:")
    print("=" * 60)
    print()
    print(f"  GMAIL_CLIENT_ID = {client_id}")
    print()
    print(f"  GMAIL_CLIENT_SECRET = {client_secret}")
    print()
    print(f"  GMAIL_REFRESH_TOKEN = {creds.refresh_token}")
    print()
    print("=" * 60)
    print()
    print("Go to: https://github.com/Idojosef/email-dashboard/settings/secrets/actions")
    print("Add each of the 3 secrets above, plus:")
    print("  ANTHROPIC_API_KEY = your Anthropic API key")
    print()
    print("That's it! Your dashboard will now refresh automatically.")


if __name__ == "__main__":
    main()
