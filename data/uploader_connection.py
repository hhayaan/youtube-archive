"""Credential validation and read-only destination-channel lookup."""

from __future__ import annotations

import json
from pathlib import Path
import re


PROJECT_DIR = Path(__file__).resolve().parents[1]
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
AUTH_URLS = {
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
}
TOKEN_URL = "https://oauth2.googleapis.com/token"
_CHANNEL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class CheckError(Exception):
    """A safe message that contains no credentials or private account details."""


def load_client_config(project_dir: Path = PROJECT_DIR) -> dict:
    """Read exactly one Desktop OAuth client JSON directly beside the uploader."""
    project_dir = Path(project_dir)
    try:
        candidates = list(project_dir.glob("client_secret*.json"))
        if len(candidates) != 1:
            raise CheckError(
                "Keep exactly one downloaded client_secret*.json file beside start.cmd."
            )
        candidate = candidates[0]
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.resolve().parent != project_dir.resolve()
        ):
            raise CheckError("The client JSON must be a regular file beside start.cmd.")
        config = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise CheckError("The client JSON could not be read. Download it again.") from None

    installed = config.get("installed") if isinstance(config, dict) else None
    if not isinstance(installed, dict) or "web" in config:
        raise CheckError("Create a Desktop app OAuth client and download its JSON.")
    if not all(
        isinstance(installed.get(key), str) and installed[key].strip()
        for key in ("client_id", "client_secret")
    ):
        raise CheckError("The Desktop client JSON is incomplete. Download it again.")
    auth_uri = installed.get("auth_uri")
    if not isinstance(auth_uri, str) or auth_uri not in AUTH_URLS or installed.get("token_uri") != TOKEN_URL:
        raise CheckError("The client JSON does not use the standard Google endpoints.")
    return config


def read_channel(session) -> tuple[str, str]:
    """Retrieve only the authorized channel's public title and ID before upload."""
    try:
        response = session.get(
            CHANNELS_URL,
            params={
                "part": "id,snippet",
                "mine": "true",
                "fields": "items(id,snippet/title)",
                "maxResults": 50,
            },
            timeout=30,
            allow_redirects=False,
        )
    except Exception:
        raise CheckError(
            "The destination-channel request could not finish. Check your connection "
            "and run start.cmd again."
        ) from None
    try:
        if response.status_code != 200:
            if response.status_code == 403:
                message = (
                    "YouTube denied the channel request (HTTP 403). Check that "
                    "YouTube Data API v3 is enabled and read-only permission is granted."
                )
            elif response.status_code == 401:
                message = "YouTube did not accept the sign-in (HTTP 401). Run start.cmd again."
            else:
                message = "YouTube could not verify the destination channel. Try again later."
            raise CheckError(message)
        try:
            data = response.json()
        except ValueError:
            raise CheckError("YouTube returned an unreadable channel response.") from None
    finally:
        response.close()

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        raise CheckError(
            "Google did not identify exactly one YouTube channel. Run start.cmd "
            "again and select the intended channel. If it is missing, verify the "
            "selected account's channel access."
        )
    channel = items[0]
    if not isinstance(channel, dict):
        raise CheckError("YouTube returned an incomplete channel response.")
    snippet = channel.get("snippet")
    title = snippet.get("title") if isinstance(snippet, dict) else None
    channel_id = channel.get("id")
    if not isinstance(title, str) or not isinstance(channel_id, str) or not _CHANNEL_ID.fullmatch(channel_id):
        raise CheckError("YouTube returned an incomplete channel response.")
    # Public channel titles can contain unusual characters; keep console output on one line.
    title = "".join(character if character.isprintable() else " " for character in title)
    return title, channel_id
