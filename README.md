# YouTube batch uploader

Upload an ordered list of videos to a YouTube channel, **unlisted**, one at a time. The program pauses between batches so you can review the results. It reads source videos directly without modifying or copying them.

## First-time setup

1. Install **Python 3.11 or newer** on Windows, including the Python launcher or adding Python to PATH.
2. Keep the uploader and your video source in separate folders. Neither folder may contain the other.
3. Create a Google Cloud project, enable **YouTube Data API v3**, and create a **Desktop app** OAuth client. Configure its consent settings for your account and these scopes:
   - `https://www.googleapis.com/auth/youtube.readonly`
   - `https://www.googleapis.com/auth/youtube.upload`
4. Download the client JSON and put it **beside `Upload Videos.cmd`**. Its name must begin with `client_secret` and end in `.json`; keep exactly one matching file there. For example, `client_secret.json` works. Use your own credentials; do not share the file.
5. Create `YouTube Upload Order.txt` using the format below. `YouTube Upload Order.example.txt` is a template you can copy and edit.
6. Double-click **Upload Videos.cmd**. On a fresh installation it creates a local `.venv` and installs `requirements.txt` automatically. Setup does not sign in or upload videos.

Google's [YouTube API setup guide](https://developers.google.com/youtube/data/getting-started) and [Desktop OAuth guide](https://developers.google.com/identity/protocols/oauth2/native-app) explain the Cloud project and client setup. If your consent configuration uses test users, include the Google account you will sign in with.

## Upload-order file

The UTF-8 text file contains the **source folder, destination channel, and ordered video records**. Configure everything here; no Python edits are needed.

```text
# Source root: D:\Clips
# Channel ID: YOUR_YOUTUBE_CHANNEL_ID
# Destination channel: My archive

001 | Game One\first clip.mp4 | First clip
002 | Game One\second clip.mkv | Second clip
003 | D:\Clips\Game Two\last clip.mp4 | Last clip
```

Replace the source root and channel ID with your own values. A channel ID is the public 24-character identifier beginning with `UC`, not an email, handle, or channel URL. The destination channel name is optional and is only a display label; the **ID** is checked after sign-in and against existing progress.

Each record has exactly three fields separated by **` | `**:

| Field | Required content |
| --- | --- |
| Number | Consecutive integers starting at 1; leading zeroes are optional. |
| Path | A path relative to `Source root`, or a full absolute Windows path inside that root. |
| Title | The exact YouTube title to use: nonempty, at most 100 characters, without `<`, `>` or control characters. |

The numbered order determines upload order. Dates in titles or filenames are not parsed or reformatted; put records in the order you want yourself. Titles can contain a game and date/time, or any other valid title.

Keep paths **unquoted**, including paths with spaces. Original spelling and Unicode characters are preserved. Do not use `..` traversal or paths outside the source root. Blank lines and ordinary `#` comments are ignored; the two configuration headers are required. The older `# Original source root:` spelling is also accepted.

Accepted video extensions are `.mp4`, `.mkv`, `.mov`, `.webm`, `.avi`, `.m4v`, `.wmv`, `.mts`, and `.m2ts`. Paths are checked without inspecting the source folder; each file is opened only when its authorized upload starts.

## Use the uploader

The main launcher is **Upload Videos.cmd**. It immediately previews the next batch, with a default size of **10**:

- **Enter:** start sign-in and upload the displayed batch.
- **B:** change the batch size, then return to the preview.
- **Esc:** exit with progress saved, including while editing the size.

Copy the sign-in link from the console into your chosen browser. Select an account with access to the configured channel and approve the requested permissions. Keep the console open during sign-in; **Ctrl+C** cancels the wait. No browser opens automatically.

After each batch, review the completed video links in the console or `.uploader-state\last-batch.txt`. The same Enter/B/Esc menu returns. Sign-in stays in memory while the window remains open. Reopening later continues at the next unfinished record.

For an offline preview or status check, use the same launcher from a terminal:

```powershell
& '.\Upload Videos.cmd' --preview --batch-size 5
& '.\Upload Videos.cmd' --status
```

These modes do not sign in, contact YouTube, or access source videos. On a fresh installation the launcher's dependency setup can still contact the Python package repository.

## Progress and recovery

Every video is uploaded **unlisted**. The next video starts only after YouTube confirms upload completion, returns the expected video ID/title/channel/visibility, and that result is saved locally. Processing can continue in the background. Only one uploader instance and one upload session run at a time.

**Keep `.uploader-state` intact and private.** It contains completed video IDs, resumable upload state, backups, and the latest batch report. Deleting it would lose the record used to avoid repeated uploads. Active session addresses are sensitive; OAuth access and refresh tokens stay in memory only.

**Ctrl+C** stops a transfer. On restart, a retained session is checked before resending bytes. Errors stop at the current record rather than skipping it. After a recognized initialization rejection, the program can offer Enter to clear only that failed attempt: confirm that the video did not upload and the upload limit or other rejection cause has been resolved. It verifies a progress backup first, preserves completed records, and keeps that same video first in the next batch. Esc leaves the block untouched. Unknown outcomes, saved sessions/IDs, or confirmed bytes require manual review.

Once uploads have started, the list of resolved paths and titles and the destination channel are protected by saved progress. Do not replace the queue or change it to force a retry. A fresh unrelated archive or another user's channel needs a **separate uploader folder and its own progress state**.

Source videos are opened read-only and streamed directly. The uploader does not rename, move, delete, stage, transcode, change metadata, or create files in the source folder. Application files and state stay beside the launcher.

## Reuse or share

Share the code, `Upload Videos.cmd`, `requirements.txt`, this README, and the example queue. Each user supplies their own actual `YouTube Upload Order.txt` and client JSON. Exclude **client JSON files, `.uploader-state`, `.venv`, personal upload lists, and test/cache folders** from a public copy.

On another computer, recreate `.venv` through the main launcher instead of copying it. For a new archive, edit the example's source root and channel ID, add your ordered rows, and save it as `YouTube Upload Order.txt`. Existing absolute lists work too; relative paths make changing a source folder easier before starting an archive.

## Offline tests

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

Tests use synthetic files and fake services. They do not sign in, contact YouTube, or access real source videos.
