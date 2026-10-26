"""Batch/CLI checks are offline; queue source paths are never accessed."""

from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta
import builtins
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from data import upload_videos as cli
from data.uploader_queue import MANIFEST_NAME, QueueEntry, StateStore
from data.uploader_transport import UploadError


PROJECT = Path(__file__).resolve().parents[1]
SOURCE = r"D:\Synthetic Clip Library"
CHANNEL_ID = "UC" + "A" * 22
SESSION_URL = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=synthetic-secret"


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="batch-test-", dir=PROJECT / "tests")
        self.project = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        base = datetime(2020, 1, 1)
        self.entries = [
            QueueEntry(i, SOURCE + f"\\Game\\synthetic-{i}.mp4",
                       f"Game {(base + timedelta(minutes=i)).strftime('%Y-%m-%d %H:%M')}")
            for i in range(1, 23)
        ]
        (self.project / MANIFEST_NAME).write_text(
            f"# Source root: {SOURCE}\n# Channel ID: {CHANNEL_ID}\n"
            + "\n".join(f"{e.number:03d} | {e.path} | {e.title}" for e in self.entries), encoding="utf-8"
        )
        self.output = []

    def store(self):
        store = StateStore(self.project, self.entries)
        self.addCleanup(store.close)
        store.bind_channel(CHANNEL_ID)
        return store

    def uploader(self, store, *, fail=None, persist=True):
        calls = []

        def upload(entry):
            calls.append(entry.number)
            # A later row may start only after the preceding completion is durable.
            if entry.number > 1:
                self.assertEqual(store.get(self.entries[entry.number - 2])["status"], "completed")
            if fail == entry.number:
                raise UploadError("Synthetic interrupted upload.")
            video_id = f"v{entry.number:010d}"
            if persist:
                store.start(entry, 100, 123)
                store.save_session(entry, SESSION_URL)
                store.complete(entry, video_id)
            return video_id

        return SimpleNamespace(upload=upload, calls=calls)

    def test_batch_ten_stops_then_restart_uses_next_ten(self):
        store = self.store()
        first = self.uploader(store)
        self.assertEqual(cli.run_batch(store, first, 10, self.output.append), 10)
        self.assertEqual(first.calls, list(range(1, 11)))
        self.assertIsNone(store.get(self.entries[10]))
        text = (store.state_dir / "last-batch.txt").read_text(encoding="utf-8")
        self.assertEqual(text.count("Uploaded | https://"), 10)
        self.assertNotIn("synthetic-secret", text)
        store.close()
        second_store = StateStore(self.project, self.entries)
        self.addCleanup(second_store.close)
        second = self.uploader(second_store)
        self.assertEqual(cli.run_batch(second_store, second, 10, self.output.append), 10)
        self.assertEqual(second.calls, list(range(11, 21)))
        final = self.uploader(second_store)
        self.assertEqual(cli.run_batch(second_store, final, 10, self.output.append), 2)
        self.assertEqual(final.calls, [21, 22])
        self.assertEqual(second_store.summary()["remaining"], 0)

    def test_failure_stops_before_later_clip_and_next_run_starts_failed_row(self):
        store = self.store()
        uploader = self.uploader(store, fail=4)
        with self.assertRaises(UploadError):
            cli.run_batch(store, uploader, 10, self.output.append)
        self.assertEqual(uploader.calls, [1, 2, 3, 4])
        self.assertEqual(store.pending(1)[0].number, 4)
        self.assertEqual(store.summary()["completed"], 3)
        self.assertIn("Not confirmed complete | not started", (store.state_dir / "last-batch.txt").read_text())

    def test_video_id_without_durable_completion_cannot_advance(self):
        store = self.store()
        uploader = self.uploader(store, persist=False)
        with self.assertRaises(UploadError):
            cli.run_batch(store, uploader, 10, self.output.append)
        self.assertEqual(uploader.calls, [1])
        self.assertEqual(store.summary()["completed"], 0)

    def test_blocked_or_uncertain_initialization_cannot_start_batch(self):
        store = self.store()
        store.start(self.entries[0], 100, 123)
        uploader = self.uploader(store)
        for block in (False, True):
            if block:
                store.block(self.entries[0], "Synthetic ambiguous result.")
            with self.assertRaises(cli.QueueError):
                cli.run_batch(store, uploader, 10, self.output.append)
        self.assertEqual(uploader.calls, [])

    @contextmanager
    def no_source_access(self):
        def guarded(original):
            def call(path, *args, **kwargs):
                if str(path).casefold().startswith(SOURCE.casefold()):
                    raise AssertionError("An offline mode accessed the original source")
                return original(path, *args, **kwargs)
            return call
        with (
            patch("builtins.open", side_effect=guarded(builtins.open)),
            patch("io.open", side_effect=guarded(io.open)),
            patch("os.stat", side_effect=guarded(os.stat)),
            patch("os.lstat", side_effect=guarded(os.lstat)),
            patch("os.scandir", side_effect=guarded(os.scandir)),
        ):
            yield

    def test_preview_status_and_cancel_never_authenticate_or_access_source(self):
        with (
            self.no_source_access(),
            patch.object(cli, "authorized_session", side_effect=AssertionError("Unexpected OAuth")) as auth,
            patch.object(cli, "ResumableUploader", side_effect=AssertionError("Unexpected transport")) as transport,
            patch.object(cli, "load_client_config", return_value={}) as config,
        ):
            for args in (["--preview", "--batch-size", "10"], ["--status"]):
                self.assertEqual(cli.main(args, project_dir=self.project, output=self.output.append), 0)
            config.assert_not_called()
            self.assertEqual(cli.main(["--batch-size", "10"], project_dir=self.project,
                                      action_fn=lambda *a, **k: "exit", output=self.output.append), 0)
            config.assert_not_called()
            auth.assert_not_called()
            transport.assert_not_called()

    def test_wrong_channel_cannot_create_transport_or_open_source(self):
        @contextmanager
        def fake_auth(config):
            yield Mock()
        with (
            self.no_source_access(),
            patch.object(cli, "authorized_session", fake_auth),
            patch.object(cli, "load_client_config", return_value={}),
            patch.object(cli, "read_channel", return_value=("Other", "UCwrong")),
            patch.object(cli, "ResumableUploader") as transport,
        ):
            result = cli.main(["--batch-size", "10"], project_dir=self.project,
                              action_fn=lambda *a, **k: "start", output=self.output.append)
        self.assertEqual(result, 1)
        transport.assert_not_called()
        self.assertTrue(any("does not match Channel ID" in line for line in self.output))
        with StateStore(self.project, self.entries) as store:
            self.assertEqual(store.summary()["completed"], 0)
            self.assertEqual(store.rows(), [])

    def test_change_size_then_escape_never_authenticates(self):
        actions = Mock(side_effect=["size", "exit"])
        size = Mock(return_value=3)
        with (
            self.no_source_access(),
            patch.object(cli, "load_client_config") as config,
            patch.object(cli, "authorized_session") as auth,
        ):
            self.assertEqual(cli.main([], project_dir=self.project,
                                      action_fn=actions, size_fn=size, output=self.output.append), 0)
        auth.assert_not_called()
        config.assert_not_called()
        self.assertEqual([call.args[0] for call in actions.call_args_list], [10, 3])
        size.assert_called_once()
        self.assertTrue(any("Selected batch: 3" in line for line in self.output))

    def run_interactive(self, decisions, *, count=10, fail=None, sizes=None):
        @contextmanager
        def fake_auth(config):
            yield Mock()

        def make_uploader(session, store, **kwargs):
            self.active_uploader = self.uploader(store, fail=fail)
            return self.active_uploader

        continuation = Mock(side_effect=decisions)
        size_reader = Mock(side_effect=sizes or [])
        with (
            self.no_source_access(),
            patch.object(cli, "authorized_session", side_effect=fake_auth) as auth,
            patch.object(cli, "load_client_config", return_value={}),
            patch.object(cli, "read_channel", return_value=("Synthetic", CHANNEL_ID)) as channel,
            patch.object(cli, "ResumableUploader", side_effect=make_uploader),
        ):
            result = cli.main(
                ["--batch-size", str(count)], project_dir=self.project,
                output=self.output.append, action_fn=continuation, size_fn=size_reader,
            )
        return result, auth, channel, continuation

    def test_enter_starts_next_batch_in_same_session_then_escape_stops(self):
        result, auth, channel, continuation = self.run_interactive(["start", "start", "exit"])
        self.assertEqual(result, 0)
        self.assertEqual(self.active_uploader.calls, list(range(1, 21)))
        self.assertEqual([call.args[0] for call in continuation.call_args_list], [10, 10, 2])
        self.assertEqual([call.kwargs["signed_in"] for call in continuation.call_args_list], [False, True, True])
        auth.assert_called_once()
        self.assertEqual(channel.call_count, 2)
        with StateStore(self.project, self.entries) as store:
            self.assertEqual(store.summary()["completed"], 20)
            self.assertEqual(store.pending(1)[0].number, 21)
        self.assertTrue(any("Progress is saved" in line for line in self.output))

    def test_reopening_after_first_ten_starts_at_eleven_and_escape_preserves_next(self):
        store = self.store()
        first = self.uploader(store)
        for entry in self.entries[:10]:
            first.upload(entry)
        store.close()
        result, _, _, continuation = self.run_interactive(["start", "exit"])
        self.assertEqual(result, 0)
        self.assertEqual(self.active_uploader.calls, list(range(11, 21)))
        self.assertEqual(continuation.call_count, 2)
        with StateStore(self.project, self.entries) as store:
            self.assertEqual(store.pending(1)[0].number, 21)

    def test_finished_queue_does_not_offer_another_batch(self):
        result, _, channel, continuation = self.run_interactive(["start"], count=30)
        self.assertEqual(result, 0)
        self.assertEqual(self.active_uploader.calls, list(range(1, 23)))
        continuation.assert_called_once()
        channel.assert_called_once()

    def test_failed_batch_never_offers_continuation(self):
        result, _, _, continuation = self.run_interactive(["start"], fail=4)
        self.assertEqual(result, 1)
        self.assertEqual(self.active_uploader.calls, [1, 2, 3, 4])
        continuation.assert_called_once()

    def test_size_changes_before_and_between_batches_wait_for_enter(self):
        result, auth, channel, actions = self.run_interactive(
            ["size", "start", "size", "start", "exit"], sizes=[5, 2]
        )
        self.assertEqual(result, 0)
        self.assertEqual(self.active_uploader.calls, list(range(1, 8)))
        auth.assert_called_once()
        self.assertEqual(channel.call_count, 2)
        self.assertEqual([call.args[0] for call in actions.call_args_list], [10, 5, 5, 2, 2])
        with StateStore(self.project, self.entries) as store:
            self.assertEqual(store.pending(1)[0].number, 8)

    def test_escape_in_size_editor_exits_without_auth(self):
        with patch.object(cli, "authorized_session") as auth:
            result = cli.main([], project_dir=self.project, output=self.output.append,
                              action_fn=lambda *a, **k: "size", size_fn=lambda *a, **k: None)
        self.assertEqual(result, 0)
        auth.assert_not_called()

    def test_different_root_channel_and_relative_paths_preview_without_auth(self):
        configured_id = "UC" + "B" * 22
        (self.project / MANIFEST_NAME).write_text(
            "# Source root: E:\\Another archive\n"
            f"# Channel ID: {configured_id}\n"
            "001 | A game\\clip\u200b.mkv | Any exact title\n",
            encoding="utf-8",
        )
        with (
            patch.object(cli, "load_client_config") as config,
            patch.object(cli, "authorized_session") as auth,
            patch.object(cli, "ResumableUploader") as transport,
        ):
            result = cli.main(["--preview"], project_dir=self.project, output=self.output.append)
        self.assertEqual(result, 0)
        config.assert_not_called()
        auth.assert_not_called()
        transport.assert_not_called()
        self.assertTrue(any(f"Destination: {configured_id}" in line for line in self.output))
        self.assertTrue(any("Any exact title" in line for line in self.output))

    def test_changed_channel_header_cannot_reassign_existing_progress(self):
        store = self.store()
        self.uploader(store).upload(self.entries[0])
        before = store._snapshot(store._connection)
        store.close()
        manifest = self.project / MANIFEST_NAME
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(CHANNEL_ID, "UC" + "B" * 22), encoding="utf-8")
        with patch.object(cli, "authorized_session") as auth:
            result = cli.main(["--preview"], project_dir=self.project, output=self.output.append)
        self.assertEqual(result, 1)
        auth.assert_not_called()
        with StateStore(self.project, self.entries) as reopened:
            self.assertEqual(reopened._snapshot(reopened._connection), before)

    def test_unexpected_private_error_text_is_not_printed(self):
        with patch.object(cli, "load_upload_plan", side_effect=RuntimeError("synthetic-private@example.invalid secret")):
            self.assertEqual(cli.main(["--status"], project_dir=self.project, output=self.output.append), 1)
        text = "\n".join(self.output)
        self.assertNotIn("example.invalid", text)
        self.assertNotIn("secret", text)


class BatchMenuKeysTests(unittest.TestCase):
    def keys(self, values, queued=None):
        return SimpleNamespace(
            getwch=Mock(side_effect=values), kbhit=Mock(side_effect=queued or [False])
        )

    def wait(self, keys):
        with (
            patch.object(cli.os, "name", "nt"),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.dict(sys.modules, {"msvcrt": keys}),
        ):
            return cli.wait_for_batch_action(10, signed_in=False, output=lambda _: None)

    def test_enter_continues_and_other_keys_do_not(self):
        keys = self.keys(["a", "\x00", "K", "\r"])
        self.assertEqual(self.wait(keys), "start")
        self.assertEqual(keys.getwch.call_count, 4)

    def test_escape_exits_immediately(self):
        keys = self.keys(["\x1b"])
        self.assertEqual(self.wait(keys), "exit")
        keys.getwch.assert_called_once()

    def test_buffered_enter_is_discarded_before_prompt(self):
        keys = self.keys(["\r", "\x1b"], queued=[True, False])
        self.assertEqual(self.wait(keys), "exit")
        self.assertEqual(keys.getwch.call_count, 2)

    def test_ctrl_c_stops(self):
        with self.assertRaises(KeyboardInterrupt):
            self.wait(self.keys(["\x03"]))

    def test_redirected_input_cannot_continue_automatically(self):
        with patch.object(cli.sys.stdin, "isatty", return_value=False):
            self.assertEqual(cli.wait_for_batch_action(10, signed_in=False, output=lambda _: None), "exit")

    def test_b_opens_size_editor(self):
        for key in ("b", "B"):
            with self.subTest(key=key):
                self.assertEqual(self.wait(self.keys([key])), "size")

    def confirm_retry(self, keys):
        entry = QueueEntry(165, SOURCE + "\\Synthetic\\not-opened.mp4", "Synthetic 2020-01-01 00:00")
        with patch.object(cli, "_console_keys", return_value=keys):
            return cli.confirm_block_retry(entry, output=lambda _: None)

    def test_block_recovery_requires_fresh_enter(self):
        self.assertTrue(self.confirm_retry(self.keys(["\r"])))

    def test_escape_keeps_block_and_buffered_enter_cannot_clear_it(self):
        keys = self.keys(["\r", "\x1b"], queued=[True, False])
        self.assertFalse(self.confirm_retry(keys))
        self.assertEqual(keys.getwch.call_count, 2)

    def test_block_recovery_ignores_other_keys_and_extended_keys(self):
        keys = self.keys(["b", "\xe0", "K", "\x1b"])
        self.assertFalse(self.confirm_retry(keys))
        self.assertEqual(keys.getwch.call_count, 4)

    def test_block_recovery_cannot_confirm_with_redirected_input(self):
        self.assertFalse(self.confirm_retry(None))

    def edit(self, values):
        keys = self.keys(values)
        output = []
        with (
            patch.object(cli.os, "name", "nt"),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.dict(sys.modules, {"msvcrt": keys}),
            redirect_stdout(io.StringIO()),
        ):
            return cli.read_batch_size(10, output=output.append), output

    def test_size_editor_accepts_digits_and_backspace(self):
        size, _ = self.edit(["1", "2", "\b", "5", "\r"])
        self.assertEqual(size, 15)

    def test_size_editor_rejects_zero_and_ignores_letters(self):
        size, output = self.edit(["0", "\r", "x", "5", "\r"])
        self.assertEqual(size, 5)
        self.assertTrue(any("positive whole" in line for line in output))

    def test_size_editor_escape_exits_immediately(self):
        size, _ = self.edit(["9", "\x1b"])
        self.assertIsNone(size)

    def test_size_editor_empty_enter_keeps_current_count(self):
        size, _ = self.edit(["\r"])
        self.assertEqual(size, 10)


if __name__ == "__main__":
    unittest.main()
