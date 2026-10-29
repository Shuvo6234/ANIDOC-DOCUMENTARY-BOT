
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR = PROJECT_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

def main():
    print("ANIDOC DOCUMENTARY BOT")
    print("Image-to-Video AI: DISABLED")
    print("Video editing: FFmpeg")
    print("Output directory:", OUTPUT_DIR)

    if not os.getenv("GEMINI_API_KEYS"):
        print("ERROR: GEMINI_API_KEYS secret is not configured.")
        sys.exit(1)

    print("Gemini API secret detected.")
    print("Environment check completed.")

if __name__ == "__main__":
    main()
