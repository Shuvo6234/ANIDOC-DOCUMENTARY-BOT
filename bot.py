import os
import sys
import json
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "output"
DRIVE_FOLDER_ID = "1tOKfM5wPPIA42gTUzVQUsgm295mzPqeJ"


def main():
    print("ANIDOC DOCUMENTARY BOT")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    api_keys = os.getenv("GEMINI_API_KEYS", "").strip()
    if not api_keys:
        print("ERROR: GEMINI_API_KEYS secret is missing.")
        sys.exit(1)

    print("Gemini API secret detected.")

    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not service_account_json:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON secret is missing.")
        sys.exit(1)

    try:
        credentials_info = json.loads(service_account_json)

        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/drive"]
        )

        drive_service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False
        )

        folder = drive_service.files().get(
            fileId=DRIVE_FOLDER_ID,
            fields="id,name,mimeType"
        ).execute()

        if folder.get("mimeType") != "application/vnd.google-apps.folder":
            print("ERROR: The configured Drive ID is not a folder.")
            sys.exit(1)

        print("Google Drive connected successfully.")
        print("Output folder:", folder.get("name", "Unknown"))

    except HttpError as error:
        print("Google Drive HTTP request failed.")
        print("HTTP status:", getattr(error.resp, "status", "Unknown"))
        print(
            "Google error details:",
            error.content.decode("utf-8", errors="replace")
        )
        sys.exit(1)

    except json.JSONDecodeError:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.")
        sys.exit(1)

    except Exception as error:
        print("Google Drive connection failed.")
        print("Error type:", type(error).__name__)
        print("Error message:", str(error))
        sys.exit(1)

    print("Output directory:", OUTPUT_DIR)
    print("Image-to-video AI: DISABLED")
    print("Video editing engine: FFmpeg")
    print("Configuration check completed.")
    print("Documentary generation is not implemented yet.")


if __name__ == "__main__":
    main()
