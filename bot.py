import os
import sys
import json
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

PROJECT_DIR = Path(file).parent
OUTPUT_DIR = PROJECT_DIR / "output"

DRIVE_FOLDER_ID = "1tOKfM5wPPIA42gTUzVQUsgm295mzPqeJ"

DRIVE_SCOPES = [
"https://www.googleapis.com/auth/drive"
]

def check_gemini_secret():
api_keys = os.getenv("GEMINI_API_KEYS", "").strip()

if not api_keys:
    raise RuntimeError(
        "GEMINI_API_KEYS secret is missing."
    )

print("Gemini API secret detected.")

def connect_google_drive():
service_account_json = os.getenv(
"GOOGLE_SERVICE_ACCOUNT_JSON", ""
).strip()

if not service_account_json:
    raise RuntimeError(
        "GOOGLE_SERVICE_ACCOUNT_JSON secret is missing."
    )

try:
    credentials_info = json.loads(service_account_json)

    credentials = (
        service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=DRIVE_SCOPES
        )
    )

    drive_service = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False
    )

    folder = (
        drive_service.files()
        .get(
            fileId=DRIVE_FOLDER_ID,
            fields="id,name,mimeType"
        )
        .execute()
    )

    if folder.get("mimeType") != "application/vnd.google-apps.folder":
        raise RuntimeError(
            "The configured Drive ID is not a folder."
        )

    print("Google Drive connected successfully.")
    print("Output folder:", folder.get("name", "Unknown"))

    return drive_service

except HttpError as error:
    print("Google Drive HTTP request failed.")
    print("HTTP status:", getattr(error.resp, "status", "Unknown"))

    try:
        print(
            "Google error details:",
            error.content.decode("utf-8", errors="replace")
        )
    except Exception:
        print("Google did not provide readable error details.")

    raise

except json.JSONDecodeError:
    raise RuntimeError(
        "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON."
    ) from None

except Exception as error:
    print("Google Drive connection failed.")
    print("Error type:", type(error).__name__)
    print("Error message:", str(error))
    raise

def main():
print("ANIDOC DOCUMENTARY BOT")
print("Checking configuration...")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

try:
    check_gemini_secret()
    connect_google_drive()

except Exception:
    print("Configuration check failed.")
    sys.exit(1)

print("Local output directory:", OUTPUT_DIR)
print("Image-to-video AI: DISABLED")
print("Video editing engine: FFmpeg")
print("Google Drive connection test completed.")
print("Documentary generation is not implemented yet.")

if name == "main":
main()
