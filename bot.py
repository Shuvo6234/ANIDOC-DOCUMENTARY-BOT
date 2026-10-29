import os
import sys
import json
from pathlib import Path

from google import genai
from google.oauth2 import service_account
from googleapiclient.discovery import build

PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

DRIVE_FOLDER_ID = "1tOKfM5wPPIA42gTUzVQUsgm295mzPqeJ"


def main():
    print("ANIDOC DOCUMENTARY BOT")

    # Gemini API configuration
    api_keys = os.getenv("GEMINI_API_KEYS", "").strip()

    if not api_keys:
        print("ERROR: GEMINI_API_KEYS secret is missing.")
        sys.exit(1)

    print("Gemini API secret detected.")

    # Google Drive authentication
    service_account_json = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON", ""
    ).strip()

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
            "drive", "v3", credentials=credentials
        )

        folder = drive_service.files().get(
            fileId=DRIVE_FOLDER_ID,
            fields="id,name,mimeType"
        ).execute()

        print("Google Drive connected successfully.")
        print("Output folder:", folder["name"])

    except Exception as error:
        print("Google Drive connection failed.")
        print("Error type:", type(error).__name__)
        sys.exit(1)

    print("Output directory:", OUTPUT_DIR)
    print("Image-to-video AI: DISABLED")
    print("Video editing engine: FFmpeg")
    print("Environment check completed.")
    print("Documentary generation is not implemented yet.")


if __name__ == "__main__":
    main()
