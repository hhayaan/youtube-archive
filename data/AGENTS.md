# Application and source protection

- The source folder is declared in the project-root `upload-order.txt`. Treat it and every item beneath it as read-only, exclusively for uploads authorized by the user.
- Never modify, rename, move, delete, copy, stage, transcode, change permissions or timestamps, or write metadata to source items. Never create files, caches, thumbnails, logs, dependencies, or temporary files there.
- Open only the current video's original bytes, in binary read-only mode, when its authorized upload starts. Stream directly without a local copy.
- Do not access, stat, resolve, or enumerate source paths during development, setup, previews, status checks, or tests. Resolve relative paths lexically only. Use synthetic files and fake services for tests.
- Keep application and source folders disjoint: neither may contain the other. All application writes belong in the uploader project.
- Building, testing, or previewing does not authorize sign-in or uploads. Never perform a live upload without an explicit request to start it.

# Configuration and compatibility

- `upload-order.txt` is authoritative UTF-8 configuration and upload order. Required headers are `# Source root:` and `# Channel ID:`. Accept legacy `# Original source root:`. Verify and display the channel ID; there is no separate channel-name setting.
- Records contain `Number | Video path | YouTube title`, with consecutive numbers starting at 1. Blank lines and other comments are ignored. Paths are unquoted literals; preserve spaces and Unicode. Never interpolate them into shell commands.
- Paths may be relative to the configured source root or absolute inside it. Preserve existing absolute strings exactly. Reject traversal, drive-relative paths, reserved names, alternate streams, and root escapes without filesystem inspection.
- Use stored titles exactly. Upload in numbered order; do not infer dates, generate titles, or impose game-specific naming rules.
- Derive channel and source settings from the manifest. Never introduce personal paths, channel IDs, account names, or credential values into code or public examples.
- Verify the configured channel ID after sign-in and against existing progress. All uploads are unlisted.
- Preserve existing queue fingerprints and state compatibility: metadata comments do not affect the entries-only fingerprint. Never silently replace saved channel bindings.
- `start.cmd` is the sole launcher. It handles first-time local dependency setup. Keep setup and offline preview/status available through this launcher; do not add extra CMD files without need.
- Application modules form the `data` package. Use package-relative imports and run `python -m data.upload_videos` from the project root. Keep `data/__init__.py` dependency-free for first-run preflight. Credentials, `upload-order.txt`, `.venv`, and `.uploader-state` remain beside `start.cmd`; `data/requirements.txt` is the dependency list.

# Sequencing, batches, and recovery

- Upload exactly one video at a time. Never pre-start a later upload session.
- Require the final successful server response, expected video ID/title/channel/unlisted status, and durable local completion before advancing. Uploaded byte count alone is insufficient.
- Do not wait for processing completion. Report later processing failures separately without automatically uploading again.
- Hold a project-local single-instance lock. Persist progress and resumable sessions under `.uploader-state` and check the current session on restart; never skip failed or uncertain rows.
- Default batches contain at most 10 confirmed completions. Show a preview immediately: fresh ENTER starts sign-in/upload, B edits the batch size then returns to preview, ESC exits with progress saved. Repeat the menu after each batch and retain sign-in only in memory. Discard buffered keys before fresh confirmations. Never continue or schedule uploads automatically.
- Keep `.uploader-state` intact. Do not delete progress to force retries. Review the specific row and remote outcome before changing a block.
- For an exactly recognized initialization rejection with no session, video ID, or confirmed bytes, offer ENTER to confirm the video did not upload and the rejection cause is resolved, or ESC to keep the block. Verify a local backup, atomically remove only that failed attempt, and preserve all completed rows and metadata exactly. Return to a fresh menu with that same video first.
- Never clear blocks during tests, preview, or status. Unknown outcomes, active sessions, known remote IDs, confirmed bytes, and completed records remain protected.

# Privacy and verification

- Never request, print, or persist private account email addresses. Users choose an account with access to their configured channel.
- Read exactly one project-local Desktop OAuth client JSON matching `client_secret*.json`. Never print its contents or values.
- Keep access and refresh tokens in memory only. Request only YouTube read-only and upload scopes. Recheck the destination before each manually started batch.
- The user requests a copyable authorization link in the uploader console after ENTER. Do not launch a browser automatically or save that link to disk, chat, logs, or tool output. Never print callback URLs/codes, tokens, resumable session URLs, raw state, or unsanitized auth/network exceptions.
- Keep credentials, filled-in private queues, progress, virtual environments, test scratch files, and caches out of public distributions. The checked-in `upload-order.txt` must remain a generic template for sharing. Keep the test source files and this generic contributor guidance tracked; ignore generated artifacts only.
- Before changing progress-related behavior, back up live state and compare completed rows, IDs, metadata, and current queue afterward. Test using synthetic state, never source videos or real OAuth.
- Do not commit or push without the user's request.
