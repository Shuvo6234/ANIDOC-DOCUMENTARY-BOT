# Privacy Policy — ANIDOC Documentary Bot

Last updated: November 1, 2026

## 1. Overview

ANIDOC Documentary Bot is a personal, non-commercial automation project. It runs privately on GitHub Actions and is operated only by its owner. It generates documentary videos (scripts, voice-over, music and video editing) from images and clips prepared by the owner, and uploads the finished videos to the owner's own YouTube channel.

The project is not offered to the public. It has no website, no sign-in for other people, and no users other than the owner.

## 2. Google Services Used

The project connects to the owner's own Google account through OAuth and uses these Google services:

- **YouTube Data API v3** — to upload finished videos, with their title, description, tags and a first comment, to the owner's own YouTube channel.
- **Google Drive API** — to read input files and store generated files and finished videos in the owner's designated Drive folder.
- **Blogger API** — to keep a list of already-used video topics on a private page of the owner's own blog.
- **Gemini API (Google AI)** — to generate scripts, text and speech.
- **Google Trends** — public trending-topic data used to choose video topics.

## 3. YouTube API Services

This project uses YouTube API Services. By using this project you agree to be bound by the YouTube Terms of Service: https://www.youtube.com/t/terms

Google Privacy Policy: https://policies.google.com/privacy

The project only accesses the owner's own YouTube channel, and only to upload videos and post a comment on those videos. It does not read, collect, store or share any data about other YouTube users or viewers, and it does not collect comments, watch history or analytics of other people.

## 4. Data We Access and Store

The project handles only the owner's own content:

- Files in the owner's Google Drive folder (images, clips, audio, prompts, scripts and finished videos).
- Video metadata (title, description, tags, comment text) that the project itself generates.
- OAuth credentials, stored as encrypted GitHub Actions secrets and never published in the repository.

Temporary working files created during a run may be kept as GitHub Actions artifacts for up to 14 days and are then deleted automatically. The project does not collect personal information from any other person.

## 5. Use and Sharing of Data

Data is used only to produce and upload the owner's videos. It is not sold, rented, or shared with third parties, and it is not used for advertising.

Text and audio are sent to Google (Gemini API) only to produce content, and are processed under Google's privacy policy and terms.

The use of information received from Google APIs will adhere to the Google API Services User Data Policy, including the Limited Use requirements: https://developers.google.com/terms/api-services-user-data-policy

## 6. Data Security

OAuth tokens and API keys are stored only as GitHub Actions secrets. They are never written to the public repository or to logs.

## 7. Revoking Access and Data Deletion

The owner can revoke the project's access to the Google account at any time here: https://security.google.com/settings/security/permissions

Videos, files and generated data can be deleted at any time directly from YouTube Studio, Google Drive and Blogger. For YouTube-related data, see Google's guidance: https://support.google.com/youtube/answer/57407

## 8. Changes

This policy may be updated from time to time. The "Last updated" date shows the latest version.

## 9. Contact

For questions about this policy, contact the project owner at: YOUR_EMAIL_HERE
Repository: https://github.com/Shuvo6234/ANIDOC-DOCUMENTARY-BOT
