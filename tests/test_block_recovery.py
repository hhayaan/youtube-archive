"""Runtime recovery integration using only temporary synthetic upload state."""

from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from data import upload_videos as cli
from data.uploader_queue import MANIFEST_NAME, QueueEntry, QueueError, StateStore
from data.uploader_transport import UploadError


PROJECT = Path(__file__).resolve().parents[1]
SOURCE = r"D:\Synthetic Clip Library"
CHANNEL_ID = "UC" + "A" * 22
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=synthetic-only"
INITIALIZATION_REASON = (
    "Upload initialization was not confirmed. Resolve this row manually "
    "before retrying so a duplicate cannot be created."
)
INITIALIZATION_403 = (
    "YouTube refused upload initialization (HTTP 403). Check the API/channel "
    "upload limit and permissions. Resolve this row before retrying."
)


class BlockRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="block-recovery-", dir=PROJECT / "tests")
        self.project = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        start = datetime(2020, 1, 1)
        self.entries = [
            QueueEntry(
                number, SOURCE + f"\\Synthetic\\clip-{number}.mp4",
                f"Synthetic {(start + timedelta(minutes=number)).strftime('%Y-%m-%d %H:%M')}",
            )
            for number in range(1, 167)
        ]
        (self.project / MANIFEST_NAME).write_text(
            f"# Source root: {SOURCE}\n# Channel ID: {CHANNEL_ID}\n"
            + "\n".join(f"{entry.number:03d} | {entry.path} | {entry.title}" for entry in self.entries),
            encoding="utf-8",
        )
        self.current = self.entries[164]
        with StateStore(self.project, self.entries) as store:
            store.bind_channel(CHANNEL_ID)
            # Fast fixture creation in this temporary database only. The next
            # public operation validates the entire completed prefix normally.
            with store._transaction() as connection:
                connection.executemany(
                    "INSERT INTO uploads "
                    "(number,path,title,status,size,mtime_ns,session_url,offset,video_id,reason) "
                    "VALUES (?,?,?,'completed',100,123,NULL,100,?,NULL)",
                    [(e.number, e.path, e.title, f"s{e.number:010d}") for e in self.entries[:164]],
                )
            store.start(self.current, 100, 123)
            store.block(self.current, INITIALIZATION_REASON)
            self.database = store.path
        self.before_rows, self.before_metadata = self.snapshot()
        self.output = []

    def snapshot(self, path=None):
        path = path or self.database
        self.assertTrue(path.is_relative_to(self.project))
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute("SELECT * FROM uploads ORDER BY number")]
            metadata = tuple(tuple(row) for row in connection.execute("SELECT key,value FROM metadata ORDER BY key"))
        return rows, metadata

    @staticmethod
    def bytes_of(rows):
        return json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def assert_completed_unchanged(self):
        rows, metadata = self.snapshot()
        self.assertEqual(self.bytes_of(rows[:164]), self.bytes_of(self.before_rows[:164]))
        self.assertEqual(metadata, self.before_metadata)

    @contextmanager
    def no_external_activity(self):
        """Fail even incidental attempts to open/stat/walk an original path."""
        protected = SOURCE.replace("/", "\\").casefold()
        with ExitStack() as stack:
            for target in ("builtins.open", "io.open", "os.open", "os.stat", "os.lstat", "os.scandir"):
                module, attr = target.rsplit(".", 1)
                imported = __import__(module)
                original = getattr(imported, attr)

                def guarded(path, *args, _original=original, **kwargs):
                    if not isinstance(path, int):
                        value = os.fsdecode(os.fspath(path)).replace("/", "\\").casefold()
                        if value == protected or value.startswith(protected + "\\"):
                            raise AssertionError("Original source access is forbidden in this test")
                    return _original(path, *args, **kwargs)

                stack.enter_context(patch(target, side_effect=guarded))
            stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("No network")))
            stack.enter_context(patch("webbrowser.get", side_effect=AssertionError("No browser")))
            yield

    def invoke(self, *, args=None, retry=(True,), actions=("exit",), fail_upload=False):
        retry_reader = Mock(side_effect=list(retry))
        action_reader = Mock(side_effect=list(actions))
        calls = []

        @contextmanager
        def fake_auth(_config):
            yield object()

        def fake_transport(_session, store, **_kwargs):
            def upload(entry):
                calls.append(entry.number)
                self.assertEqual(store.pending(1)[0], entry)
                self.assertEqual(store.get(self.entries[entry.number - 2])["status"], "completed")
                store.start(entry, 100, 123)
                if fail_upload:
                    store.block(entry, INITIALIZATION_403)
                    raise UploadError(INITIALIZATION_403)
                store.save_session(entry, SESSION)
                video_id = f"s{entry.number:010d}"
                store.complete(entry, video_id)
                return video_id
            return SimpleNamespace(upload=upload)

        with (
            self.no_external_activity(),
            patch.object(cli, "load_client_config", return_value={}) as config,
            patch.object(cli, "authorized_session", side_effect=fake_auth) as auth,
            patch.object(cli, "read_channel", return_value=("Synthetic", CHANNEL_ID)) as channel,
            patch.object(cli, "ResumableUploader", side_effect=fake_transport) as transport,
        ):
            result = cli.main(
                args or ["--batch-size", "1"], project_dir=self.project,
                output=self.output.append, retry_fn=retry_reader, action_fn=action_reader,
            )
        return SimpleNamespace(
            result=result, retry=retry_reader, actions=action_reader, calls=calls,
            config=config, auth=auth, channel=channel, transport=transport,
        )

    def test_confirm_clear_then_separate_menu_escape_keeps_164_completions(self):
        run = self.invoke()
        self.assertEqual(run.result, 0)
        run.retry.assert_called_once()
        self.assertEqual(run.retry.call_args.args[0], self.current)
        run.actions.assert_called_once()
        run.auth.assert_not_called()
        run.config.assert_not_called()
        run.transport.assert_not_called()
        self.assert_completed_unchanged()
        with StateStore(self.project, self.entries) as store:
            self.assertEqual(len(store.rows()), 164)
            self.assertIsNone(store.get(self.current))
            self.assertEqual(store.pending(1)[0].number, 165)
            backups = list(store.state_dir.glob("retry-backup-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.snapshot(backups[0]), (self.before_rows, self.before_metadata))

    def test_recovery_escape_preserves_every_row_and_never_authenticates(self):
        run = self.invoke(retry=(False,), actions=())
        self.assertEqual(run.result, 0)
        self.assertEqual(self.snapshot(), (self.before_rows, self.before_metadata))
        run.retry.assert_called_once()
        run.actions.assert_not_called()
        run.auth.assert_not_called()
        run.config.assert_not_called()
        run.transport.assert_not_called()

    def test_truthy_text_is_not_explicit_confirmation(self):
        run = self.invoke(retry=("yes",), actions=())
        self.assertEqual(run.result, 0)
        self.assertEqual(self.snapshot(), (self.before_rows, self.before_metadata))
        run.actions.assert_not_called()
        run.auth.assert_not_called()

    def test_fresh_start_uploads_165_first_and_never_replays_164_or_starts_166(self):
        run = self.invoke(actions=("start", "exit"))
        self.assertEqual(run.result, 0)
        self.assertEqual(run.calls, [165])
        self.assertEqual([call.kwargs["signed_in"] for call in run.actions.call_args_list], [False, True])
        run.retry.assert_called_once()
        run.auth.assert_called_once()
        run.channel.assert_called_once()
        self.assert_completed_unchanged()
        with StateStore(self.project, self.entries) as store:
            self.assertEqual(store.get(self.current)["status"], "completed")
            self.assertIsNone(store.get(self.entries[165]))
            self.assertEqual(store.pending(1)[0].number, 166)

    def test_preview_and_status_do_not_prompt_or_change_the_block(self):
        for args in (["--preview"], ["--status"]):
            with self.subTest(args=args):
                run = self.invoke(args=args, retry=(), actions=())
                self.assertEqual(run.result, 0)
                run.retry.assert_not_called()
                run.actions.assert_not_called()
                run.auth.assert_not_called()
                run.config.assert_not_called()
                run.transport.assert_not_called()
                self.assertEqual(self.snapshot(), (self.before_rows, self.before_metadata))

    def test_exact_initialization_403_can_be_reviewed_and_cleared(self):
        with StateStore(self.project, self.entries) as store:
            store.block(self.current, INITIALIZATION_403)
        run = self.invoke()
        self.assertEqual(run.result, 0)
        run.retry.assert_called_once()
        run.actions.assert_called_once()
        run.auth.assert_not_called()
        self.assert_completed_unchanged()
        with StateStore(self.project, self.entries) as store:
            self.assertIsNone(store.get(self.current))

    def test_quota_failure_offers_recovery_in_same_window_without_auto_retry(self):
        run = self.invoke(retry=(True, False), actions=("start",), fail_upload=True)
        self.assertEqual(run.result, 0)
        self.assertEqual(run.calls, [165])
        self.assertEqual(run.retry.call_count, 2)
        run.actions.assert_called_once()
        run.auth.assert_called_once()
        self.assert_completed_unchanged()
        with StateStore(self.project, self.entries) as store:
            self.assertEqual(store.get(self.current)["status"], "blocked")
            self.assertEqual(store.pending(1)[0].number, 165)
            self.assertIsNone(store.get(self.entries[165]))

    def test_unsafe_rows_never_offer_reset_or_change_progress(self):
        variants = {
            "unknown initialization outcome": dict(reason="Upload initialization was interrupted and its outcome is unknown."),
            "existing resumable session": dict(session_url=SESSION),
            "known remote video": dict(video_id="remote165id", offset=100),
            "previously transferred bytes": dict(offset=1),
            "unconfirmed initiating row": dict(status="initiating", reason=None),
        }
        for name, changes in variants.items():
            with self.subTest(case=name):
                original = self.before_rows[164]
                values = {**original, **changes}
                with StateStore(self.project, self.entries) as store:
                    with store._transaction() as connection:
                        connection.execute(
                            "UPDATE uploads SET status=?,session_url=?,offset=?,video_id=?,reason=? WHERE number=165",
                            tuple(values[key] for key in ("status", "session_url", "offset", "video_id", "reason")),
                        )
                    before = self.snapshot()
                    self.assertFalse(store.can_retry_blocked(self.current))
                    with self.assertRaises(QueueError):
                        store.reset_blocked_for_retry(self.current, confirmed_not_uploaded=True)
                run = self.invoke(retry=(), actions=())
                self.assertEqual(run.result, 1)
                run.retry.assert_not_called()
                run.auth.assert_not_called()
                run.transport.assert_not_called()
                self.assertEqual(self.snapshot(), before)

    def test_missing_confirmation_or_noncurrent_row_cannot_be_reset(self):
        before = self.snapshot()
        with self.no_external_activity(), StateStore(self.project, self.entries) as store:
            for entry, confirmed in ((self.current, False), (self.entries[163], True), (self.entries[165], True)):
                with self.subTest(number=entry.number, confirmed=confirmed):
                    with self.assertRaises(QueueError):
                        store.reset_blocked_for_retry(entry, confirmed_not_uploaded=confirmed)
                    self.assertEqual(self.snapshot(), before)


class RecoveryPromptKeysTests(unittest.TestCase):
    entry = QueueEntry(165, SOURCE + r"\Synthetic\clip-165.mp4", "Synthetic 2020-01-01 02:45")

    def prompt(self, keys, queued=(False,)):
        keyboard = SimpleNamespace(
            getwch=Mock(side_effect=list(keys)), kbhit=Mock(side_effect=list(queued)),
        )
        output = []
        with patch.object(cli, "_console_keys", return_value=keyboard):
            result = cli.confirm_block_retry(self.entry, output=output.append)
        return result, keyboard, output

    def test_fresh_enter_confirms_and_escape_declines(self):
        self.assertIs(self.prompt(["\r"])[0], True)
        self.assertIs(self.prompt(["\x1b"])[0], False)

    def test_buffered_enter_is_discarded_before_recovery_prompt(self):
        result, keyboard, output = self.prompt(["\r", "\x1b"], queued=(True, False))
        self.assertIs(result, False)
        self.assertEqual(keyboard.getwch.call_count, 2)
        self.assertTrue(any("165" in line for line in output))
        self.assertTrue(any("did NOT upload" in line for line in output))

    def test_extended_key_is_consumed_before_fresh_enter(self):
        result, keyboard, _ = self.prompt(["\xe0", "K", "\r"])
        self.assertIs(result, True)
        self.assertEqual(keyboard.getwch.call_count, 3)

    def test_ctrl_c_cancels_without_confirmation(self):
        with self.assertRaises(KeyboardInterrupt):
            self.prompt(["\x03"])

    def test_noninteractive_console_declines(self):
        with patch.object(cli, "_console_keys", return_value=None):
            self.assertIs(cli.confirm_block_retry(self.entry, output=lambda _: None), False)


if __name__ == "__main__":
    unittest.main()
