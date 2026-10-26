"""Sequential resumable transfer; opening media requires upload() explicitly."""

from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

from google.auth.exceptions import TransportError
from requests.exceptions import RequestException

from .uploader_queue import QueueEntry, StateStore


UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK_SIZE = 8 * 1024 * 1024
RETRYABLE = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
MAX_DELAY = 60
REQUEST_TIMEOUT = (30, 120)
NETWORK_ERRORS = (RequestException, TransportError)
_RANGE = re.compile(r"bytes=0-(\d+)\Z")
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


class UploadError(Exception):
    """A safe user-facing failure with no response body, URL, or auth data."""


@dataclass(frozen=True)
class _Result:
    offset: int | None = None
    video_id: str | None = None


def _session_url(value: object) -> str:
    """Reject redirects and credential-bearing/unexpected upload locations."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise UploadError("The saved upload session address is invalid. Resolve this row manually.")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "www.googleapis.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/upload/youtube/v3/videos"
            and not parsed.fragment
            and bool(parse_qs(parsed.query).get("upload_id"))
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise UploadError("The saved upload session address is invalid. Resolve this row manually.")
    return value


class ResumableUploader:
    """Transfer one queue entry; callers must enforce queue order and one process."""

    def __init__(
        self,
        session,
        store: StateStore,
        output=print,
        sleep=time.sleep,
        *,
        expected_channel_id: str,
    ):
        if not isinstance(expected_channel_id, str) or not expected_channel_id:
            raise UploadError("A confirmed destination channel ID is required before uploading.")
        self.session = session
        self.store = store
        self.output = output
        self.sleep = sleep
        self.expected_channel_id = expected_channel_id
        self._last_percent = -1

    def _block(self, entry: QueueEntry, reason: str) -> None:
        self.store.block(entry, reason)
        raise UploadError(reason)

    @staticmethod
    def _snapshot(source) -> tuple[int, int]:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise UploadError("The current source must be a nonempty regular video file.")
        return info.st_size, info.st_mtime_ns

    def _unchanged(self, entry: QueueEntry, source, size: int, mtime_ns: int) -> None:
        if self._snapshot(source) != (size, mtime_ns):
            self._block(
                entry,
                "The source size or modification time changed. Stop and resolve this row manually.",
            )

    def _pause(self, attempt: int, retry_after: str | None = None) -> None:
        if attempt >= MAX_RETRIES:
            raise UploadError(
                "The current upload could not be confirmed after repeated retries. "
                "It remains paused on this row; rerun to check the same session."
            )
        delay = min(2**attempt, MAX_DELAY)
        if retry_after:
            try:
                requested = float(retry_after)
            except (ValueError, TypeError):
                try:
                    target = parsedate_to_datetime(retry_after)
                    if target.tzinfo is None:
                        target = target.replace(tzinfo=timezone.utc)
                    requested = (target - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    requested = 0
            if requested > MAX_DELAY:
                raise UploadError(
                    "YouTube requested a longer pause. Resume this row later; "
                    "the existing upload session has been retained."
                )
            if 0 <= requested <= MAX_DELAY:
                delay = max(delay, requested)
        self.sleep(delay)

    def _progress(self, entry: QueueEntry, offset: int, size: int) -> _Result:
        self.store.progress(entry, offset)
        percent = offset * 100 // size
        if percent != self._last_percent:
            self.output(f"Transfer confirmed: {percent}% ({offset:,} / {size:,} bytes).")
            self._last_percent = percent
        return _Result(offset=offset)

    def _completed(
        self, entry: QueueEntry, response, source, size: int, mtime_ns: int
    ) -> _Result:
        try:
            data = response.json()
        except (ValueError, TypeError):
            self._block(
                entry,
                "YouTube returned completion without a readable video record. "
                "Resolve this row manually before uploading anything else.",
            )
        snippet = data.get("snippet") if isinstance(data, dict) else None
        status = data.get("status") if isinstance(data, dict) else None
        video_id = data.get("id") if isinstance(data, dict) else None
        known_id = isinstance(video_id, str) and _VIDEO_ID.fullmatch(video_id)
        if not (
            known_id
            and isinstance(snippet, dict)
            and snippet.get("title") == entry.title
            and snippet.get("channelId") == self.expected_channel_id
            and isinstance(status, dict)
            and status.get("privacyStatus") == "unlisted"
        ):
            reason = (
                "YouTube's completion record did not confirm the expected video, title, "
                "unlisted visibility, and destination channel. Resolve this row manually."
            )
            if known_id:
                self.store.record_remote_issue(entry, video_id, reason)
                raise UploadError(reason)
            self._block(entry, reason)
        try:
            unchanged = self._snapshot(source) == (size, mtime_ns)
        except (OSError, UploadError):
            unchanged = False
        if not unchanged:
            reason = (
                "YouTube returned a video ID, but the source changed or could not be "
                "verified. The ID is retained; resolve this row manually."
            )
            self.store.record_remote_issue(entry, video_id, reason)
            raise UploadError(reason)
        # Persist the successful ID before the caller can advance to another row.
        self.store.complete(entry, video_id)
        return _Result(video_id=video_id)

    def _response(
        self,
        entry: QueueEntry,
        response,
        source,
        size: int,
        mtime_ns: int,
        *,
        maximum_offset: int | None = None,
    ) -> _Result | None:
        """Inspect a response while its caller owns closing it; None requests recovery."""
        code = response.status_code
        if code in (200, 201):
            return self._completed(entry, response, source, size, mtime_ns)
        if code == 308:
            value = response.headers.get("Range")
            if value is None:
                offset = 0
            else:
                match = _RANGE.fullmatch(value) if isinstance(value, str) else None
                if match is None:
                    self._block(entry, "YouTube returned an invalid received-byte range. Resolve this row manually.")
                offset = int(match.group(1)) + 1
            if not 0 <= offset <= size or (
                maximum_offset is not None and offset > maximum_offset
            ):
                self._block(entry, "YouTube returned an impossible received-byte range. Resolve this row manually.")
            return self._progress(entry, offset, size)
        if code in (404, 410):
            self._block(
                entry,
                "The existing upload session expired or could not be found. "
                "Check YouTube and resolve this row manually; no replacement upload was started.",
            )
        if code in RETRYABLE:
            return None
        if code == 403:
            raise UploadError(
                "YouTube refused the current upload (HTTP 403). Check the API/channel "
                "upload limit and permissions, then resume this same row."
            )
        if code == 401:
            raise UploadError("YouTube rejected authorization (HTTP 401). Sign in again and resume this row.")
        raise UploadError(
            "YouTube rejected the current transfer. The queue remains on this row "
            "and the existing session has been retained."
        )

    def _query(
        self, entry: QueueEntry, url: str, source, size: int, mtime_ns: int
    ) -> _Result:
        """Always establish the server's position before retrying uncertain bytes."""
        attempt = 0
        while True:
            response = None
            retry_after = None
            try:
                response = self.session.put(
                    url,
                    data=b"",
                    headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"},
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                )
                result = self._response(entry, response, source, size, mtime_ns)
                if result is not None:
                    return result
                retry_after = response.headers.get("Retry-After")
            except NETWORK_ERRORS:
                pass
            finally:
                if response is not None:
                    response.close()
            self._pause(attempt, retry_after)
            attempt += 1

    def _initiate(
        self, entry: QueueEntry, size: int, mtime_ns: int
    ) -> str:
        # A crash after this state write can never silently trigger another POST.
        self.store.start(entry, size, mtime_ns)
        response = None
        try:
            response = self.session.post(
                UPLOAD_URL,
                params={
                    "uploadType": "resumable",
                    "part": "snippet,status",
                    "notifySubscribers": "false",
                },
                json={
                    "snippet": {"title": entry.title, "categoryId": "20"},
                    "status": {"privacyStatus": "unlisted"},
                },
                headers={
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Length": str(size),
                    "X-Upload-Content-Type": "video/*",
                },
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
            if response.status_code not in (200, 201):
                reason = (
                    "YouTube refused upload initialization (HTTP 403). Check the API/channel "
                    "upload limit and permissions. Resolve this row before retrying."
                    if response.status_code == 403
                    else "Upload initialization was not confirmed. Resolve this row manually "
                    "before retrying so a duplicate cannot be created."
                )
                self._block(entry, reason)
            try:
                url = _session_url(response.headers.get("Location"))
            except UploadError:
                self._block(
                    entry,
                    "YouTube did not return a usable upload session. Resolve this row "
                    "manually before retrying so a duplicate cannot be created.",
                )
            # The session must be durable before sending even the first video byte.
            self.store.save_session(entry, url)
            return url
        except NETWORK_ERRORS:
            self._block(
                entry,
                "Upload initialization was interrupted and its outcome is unknown. "
                "Resolve this row manually; a second upload was not started.",
            )
        finally:
            if response is not None:
                response.close()

    def upload(self, entry: QueueEntry) -> str:
        """Open this one original read-only only when an authorized caller invokes us."""
        record = self.store.get(entry)
        if record is not None:
            if record.get("status") == "completed":
                video_id = record.get("video_id")
                if isinstance(video_id, str) and _VIDEO_ID.fullmatch(video_id):
                    return video_id
                raise UploadError("The recorded completed upload is invalid. Resolve this row manually.")
            if record.get("status") == "blocked":
                raise UploadError("This row is blocked. Resolve its recorded issue before continuing.")
            if not record.get("session_url"):
                self._block(
                    entry,
                    "An earlier upload initialization has no saved session address. "
                    "Resolve this row manually; another upload will not be started.",
                )
            try:
                url = _session_url(record["session_url"])
            except UploadError:
                self._block(entry, "The saved upload session is invalid. Resolve this row manually.")

        self._last_percent = -1
        try:
            # Deliberately no preliminary stat(), directory walk, staging, or media processing.
            source = open(entry.path, "rb")
        except OSError:
            raise UploadError("The current source video could not be opened read-only. Check its queue path.") from None
        try:
            with source:
                size, mtime_ns = self._snapshot(source)
                if record is None:
                    url = self._initiate(entry, size, mtime_ns)
                    result = _Result(offset=0)
                else:
                    if (record.get("size"), record.get("mtime_ns")) != (size, mtime_ns):
                        self._block(
                            entry,
                            "The source size or modification time differs from the saved upload. "
                            "Resolve this row manually before continuing.",
                        )
                    result = self._query(entry, url, source, size, mtime_ns)

                no_progress = 0
                while result.video_id is None:
                    offset = result.offset
                    if offset is None or not 0 <= offset <= size:
                        self._block(entry, "The current upload position is invalid. Resolve this row manually.")
                    self._unchanged(entry, source, size, mtime_ns)
                    if offset == size:
                        # All bytes alone are insufficient: only a confirmed video ID completes a row.
                        self._pause(no_progress)
                        no_progress += 1
                        result = self._query(entry, url, source, size, mtime_ns)
                        continue
                    source.seek(offset)
                    chunk = source.read(min(CHUNK_SIZE, size - offset))
                    if len(chunk) != min(CHUNK_SIZE, size - offset):
                        self._block(entry, "The source could not be read completely. Resolve this row manually.")
                    end = offset + len(chunk) - 1
                    response = None
                    retry_after = None
                    try:
                        response = self.session.put(
                            url,
                            data=chunk,
                            headers={
                                "Content-Length": str(len(chunk)),
                                "Content-Type": "video/*",
                                "Content-Range": f"bytes {offset}-{end}/{size}",
                            },
                            timeout=REQUEST_TIMEOUT,
                            allow_redirects=False,
                        )
                        next_result = self._response(
                            entry, response, source, size, mtime_ns, maximum_offset=end + 1
                        )
                        if next_result is None:
                            retry_after = response.headers.get("Retry-After")
                    except NETWORK_ERRORS:
                        next_result = None
                    finally:
                        if response is not None:
                            response.close()
                    if next_result is None:
                        self._pause(no_progress, retry_after)
                        next_result = self._query(entry, url, source, size, mtime_ns)
                    if next_result.video_id is not None:
                        result = next_result
                        continue
                    if next_result.offset is not None and next_result.offset > offset:
                        no_progress = 0
                    else:
                        # A syntactically successful 308 without progress must also be bounded.
                        self._pause(no_progress)
                        no_progress += 1
                    result = next_result
                return result.video_id
        except OSError:
            raise UploadError(
                "The source read or local upload-state write failed. Stop on this row "
                "and resume only after resolving the local problem."
            ) from None
