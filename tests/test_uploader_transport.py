"""Transport checks use synthetic bytes in this project's tests directory only."""

from __future__ import annotations

import builtins
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from google.auth.exceptions import TransportError
from requests.exceptions import ConnectionError

from data.uploader_queue import QueueEntry
from data.uploader_transport import CHUNK_SIZE, UPLOAD_URL, ResumableUploader, UploadError


PROJECT = Path(__file__).resolve().parents[1]
SESSION_URL = UPLOAD_URL + "?uploadType=resumable&upload_id=synthetic-session"
CHANNEL = "UCsynthetic_channel_only"
VIDEO_ID = "abcdefghijk"


class Response:
    def __init__(self, status, *, headers=None, data=None):
        self.status_code = status
        self.headers = headers or {}
        self.data = data
        self.closed = False

    def json(self):
        return self.data

    def close(self):
        self.closed = True


class Store:
    def __init__(self, record=None):
        self.record = record
        self.events = []

    def get(self, entry):
        return self.record

    def start(self, entry, size, mtime_ns):
        if self.record is not None:
            raise AssertionError("A second initialization must never happen")
        self.events.append(("start",))
        self.record = dict(status="initiating", size=size, mtime_ns=mtime_ns, offset=0)

    def save_session(self, entry, url):
        self.events.append(("save_session",))
        self.record.update(status="uploading", session_url=url)

    def progress(self, entry, offset):
        self.events.append(("progress", offset))
        self.record.update(offset=offset)

    def complete(self, entry, video_id):
        self.events.append(("complete", video_id))
        self.record.update(status="completed", video_id=video_id)

    def block(self, entry, reason):
        self.events.append(("block",))
        self.record.update(status="blocked", reason=reason)

    def record_remote_issue(self, entry, video_id, reason):
        self.events.append(("remote_issue", video_id))
        self.record.update(status="blocked", video_id=video_id, reason=reason)


class Session:
    def __init__(self, store, *responses):
        self.store = store
        self.responses = list(responses)
        self.calls = []

    def _request(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if method == "post":
            assert self.store.record["status"] == "initiating"
        if method == "put":
            assert self.store.record.get("session_url") == SESSION_URL
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def post(self, url, **kwargs):
        return self._request("post", url, kwargs)

    def put(self, url, **kwargs):
        return self._request("put", url, kwargs)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="synthetic-transfer-", dir=PROJECT / "tests")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.mp4"
        self.path.write_bytes(b"0123456789")
        self.entry = QueueEntry(1, str(self.path), "Synthetic 2020-01-01 01:23")
        info = self.path.stat()
        self.size = info.st_size
        self.mtime = info.st_mtime_ns
        self.delays = []

    def successful(self, **overrides):
        data = {
            "id": VIDEO_ID,
            "snippet": {"title": self.entry.title, "channelId": CHANNEL},
            "status": {"privacyStatus": "unlisted"},
        }
        data.update(overrides)
        return Response(200, data=data)

    def pending(self, offset=0):
        return dict(
            status="uploading", size=self.size, mtime_ns=self.mtime,
            session_url=SESSION_URL, offset=offset,
        )

    def uploader(self, store, session):
        return ResumableUploader(
            session, store, output=lambda _: None, sleep=self.delays.append,
            expected_channel_id=CHANNEL,
        )

    def test_upload_reads_original_only_rb_and_saves_session_before_video_bytes(self):
        store = Store()
        first = Response(200, headers={"Location": SESSION_URL})
        last = self.successful()
        session = Session(store, first, last)
        actual_open = builtins.open
        opened = []

        def only_source(path, mode, *args, **kwargs):
            self.assertEqual((path, mode), (str(self.path), "rb"))
            opened.append((path, mode))
            return actual_open(path, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=only_source):
            self.assertEqual(self.uploader(store, session).upload(self.entry), VIDEO_ID)
        self.assertEqual(len(opened), 1)
        self.assertEqual(store.record["status"], "completed")
        self.assertEqual(store.events[:2], [("start",), ("save_session",)])
        post, put = session.calls
        self.assertEqual(post[2]["params"]["notifySubscribers"], "false")
        self.assertEqual(post[2]["json"], {
            "snippet": {"title": self.entry.title, "categoryId": "20"},
            "status": {"privacyStatus": "unlisted"},
        })
        self.assertEqual(put[2]["data"], b"0123456789")
        self.assertEqual(put[2]["headers"]["Content-Range"], "bytes 0-9/10")
        self.assertTrue(first.closed and last.closed)
        self.assertTrue(all(call[2]["allow_redirects"] is False for call in session.calls))

    def test_lost_final_response_queries_same_session_and_retains_one_video_id(self):
        store = Store()
        session = Session(
            store, Response(200, headers={"Location": SESSION_URL}),
            ConnectionError("sensitive exception text must never be printed"), self.successful(),
        )
        self.assertEqual(self.uploader(store, session).upload(self.entry), VIDEO_ID)
        self.assertEqual([call[0] for call in session.calls], ["post", "put", "put"])
        query = session.calls[-1]
        self.assertEqual(query[1], SESSION_URL)
        self.assertEqual(query[2]["headers"]["Content-Range"], "bytes */10")
        self.assertEqual(query[2]["data"], b"")
        self.assertEqual(store.record["video_id"], VIDEO_ID)

    def test_resume_queries_308_then_seeks_to_server_offset(self):
        store = Store(self.pending(offset=2))
        session = Session(store, Response(308, headers={"Range": "bytes=0-3"}), self.successful())
        self.assertEqual(self.uploader(store, session).upload(self.entry), VIDEO_ID)
        self.assertEqual([call[0] for call in session.calls], ["put", "put"])
        self.assertEqual(session.calls[0][2]["headers"]["Content-Range"], "bytes */10")
        self.assertEqual(session.calls[1][2]["headers"]["Content-Range"], "bytes 4-9/10")
        self.assertEqual(session.calls[1][2]["data"], b"456789")

    def test_google_auth_transport_failure_also_checks_same_session(self):
        store = Store()
        session = Session(
            store, Response(200, headers={"Location": SESSION_URL}),
            TransportError("sensitive response text"), self.successful(),
        )
        self.assertEqual(self.uploader(store, session).upload(self.entry), VIDEO_ID)
        self.assertEqual(session.calls[-1][2]["headers"]["Content-Range"], "bytes */10")

    def test_resume_server_can_confirm_lower_offset_than_local_state(self):
        store = Store(self.pending(offset=8))
        session = Session(store, Response(308, headers={"Range": "bytes=0-1"}), self.successful())
        self.uploader(store, session).upload(self.entry)
        self.assertIn(("progress", 2), store.events)
        self.assertEqual(session.calls[1][2]["data"], b"23456789")

    def test_init_quota_refusal_blocks_and_cannot_start_another_session(self):
        store = Store()
        session = Session(store, Response(403, data={"error": "not printed"}))
        with self.assertRaisesRegex(UploadError, "403"):
            self.uploader(store, session).upload(self.entry)
        self.assertEqual(store.record["status"], "blocked")
        with patch("builtins.open", side_effect=AssertionError("No source access")):
            with self.assertRaisesRegex(UploadError, "blocked"):
                self.uploader(store, session).upload(self.entry)
        self.assertEqual(len(session.calls), 1)

    def test_ambiguous_initialization_network_failure_never_posts_again(self):
        store = Store()
        session = Session(store, ConnectionError("sensitive token text"))
        with self.assertRaisesRegex(UploadError, "unknown") as result:
            self.uploader(store, session).upload(self.entry)
        self.assertNotIn("sensitive", str(result.exception))
        self.assertEqual(store.record["status"], "blocked")
        self.assertEqual([call[0] for call in session.calls], ["post"])

    def test_expired_session_blocks_without_starting_replacement(self):
        store = Store(self.pending())
        session = Session(store, Response(404))
        with self.assertRaisesRegex(UploadError, "expired"):
            self.uploader(store, session).upload(self.entry)
        self.assertEqual(store.record["status"], "blocked")
        self.assertEqual([call[0] for call in session.calls], ["put"])

    def test_initiating_without_session_blocks_before_any_source_access(self):
        store = Store(dict(status="initiating", size=self.size, mtime_ns=self.mtime))
        session = Session(store)
        with patch("builtins.open", side_effect=AssertionError("No source access")):
            with self.assertRaisesRegex(UploadError, "no saved session"):
                self.uploader(store, session).upload(self.entry)
        self.assertFalse(session.calls)

    def test_completed_record_returns_without_source_or_network_access(self):
        store = Store(dict(status="completed", video_id=VIDEO_ID))
        session = Session(store)
        with patch("builtins.open", side_effect=AssertionError("No source access")):
            self.assertEqual(self.uploader(store, session).upload(self.entry), VIDEO_ID)
        self.assertFalse(session.calls)

    def test_changed_source_on_resume_stops_before_network(self):
        record = self.pending()
        record["mtime_ns"] -= 1
        store = Store(record)
        session = Session(store)
        with self.assertRaisesRegex(UploadError, "differs"):
            self.uploader(store, session).upload(self.entry)
        self.assertEqual(store.record["status"], "blocked")
        self.assertFalse(session.calls)

    def test_unexpected_metadata_retains_remote_video_id_and_blocks(self):
        store = Store(self.pending())
        session = Session(store, self.successful(status={"privacyStatus": "public"}))
        with self.assertRaisesRegex(UploadError, "completion record"):
            self.uploader(store, session).upload(self.entry)
        self.assertEqual(store.record["status"], "blocked")
        self.assertEqual(store.record["video_id"], VIDEO_ID)
        self.assertNotIn(("complete", VIDEO_ID), store.events)

    def test_source_changed_after_completion_keeps_id_and_blocks(self):
        store = Store(self.pending())
        session = Session(store, self.successful())
        uploader = self.uploader(store, session)
        with patch.object(uploader, "_snapshot", side_effect=[
            (self.size, self.mtime), (self.size, self.mtime + 1),
        ]):
            with self.assertRaisesRegex(UploadError, "source changed"):
                uploader.upload(self.entry)
        self.assertEqual(store.record["video_id"], VIDEO_ID)
        self.assertEqual(store.record["status"], "blocked")

    def test_source_becomes_empty_after_completion_keeps_id_and_blocks(self):
        store = Store(self.pending())
        session = Session(store, self.successful())
        uploader = self.uploader(store, session)
        with patch.object(uploader, "_snapshot", side_effect=[
            (self.size, self.mtime), UploadError("The current source is empty"),
        ]):
            with self.assertRaisesRegex(UploadError, "source changed"):
                uploader.upload(self.entry)
        self.assertEqual(store.record["video_id"], VIDEO_ID)
        self.assertEqual(store.record["status"], "blocked")

    def test_invalid_location_never_receives_video_bytes(self):
        store = Store()
        session = Session(store, Response(200, headers={"Location": "https://example.com/upload?upload_id=x"}))
        with self.assertRaisesRegex(UploadError, "usable upload session"):
            self.uploader(store, session).upload(self.entry)
        self.assertEqual([call[0] for call in session.calls], ["post"])
        self.assertEqual(store.record["status"], "blocked")

    def test_out_of_bounds_server_range_blocks(self):
        store = Store(self.pending())
        session = Session(store, Response(308, headers={"Range": "bytes=0-10"}))
        with self.assertRaisesRegex(UploadError, "impossible"):
            self.uploader(store, session).upload(self.entry)
        self.assertEqual(store.record["status"], "blocked")

    def test_retryable_put_checks_session_before_sending_remaining_bytes(self):
        store = Store(self.pending())
        session = Session(
            store, Response(308), Response(503, headers={"Retry-After": "2"}),
            Response(308, headers={"Range": "bytes=0-3"}), self.successful(),
        )
        self.uploader(store, session).upload(self.entry)
        ranges = [call[2]["headers"]["Content-Range"] for call in session.calls]
        self.assertEqual(ranges, ["bytes */10", "bytes 0-9/10", "bytes */10", "bytes 4-9/10"])
        self.assertIn(2, self.delays)

    def test_full_byte_count_without_video_id_never_completes(self):
        store = Store(self.pending())
        session = Session(store, *[Response(308, headers={"Range": "bytes=0-9"}) for _ in range(8)])
        with self.assertRaisesRegex(UploadError, "could not be confirmed"):
            self.uploader(store, session).upload(self.entry)
        self.assertNotEqual(store.record["status"], "completed")
        self.assertTrue(all(call[2]["data"] == b"" for call in session.calls))

    def test_large_video_uses_bounded_eight_mib_chunks_in_order(self):
        self.path.write_bytes(b"x" * CHUNK_SIZE + b"tail")
        store = Store()
        session = Session(
            store, Response(200, headers={"Location": SESSION_URL}),
            Response(308, headers={"Range": f"bytes=0-{CHUNK_SIZE - 1}"}), self.successful(),
        )
        self.uploader(store, session).upload(self.entry)
        puts = session.calls[1:]
        self.assertEqual([len(call[2]["data"]) for call in puts], [CHUNK_SIZE, 4])
        self.assertEqual(puts[1][2]["data"], b"tail")


if __name__ == "__main__":
    unittest.main()
