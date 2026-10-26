"""Offline tests: original source paths are inert strings, never opened."""

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from data.uploader_queue import (
    MANIFEST_NAME, RETRYABLE_INITIALIZATION_BLOCK_REASON, ProjectLock,
    QueueEntry, QueueError, StateStore, UploadPlan, _safe_project, load_queue, load_upload_plan,
)


ROOT = r"D:\Synthetic Clip Library"
CHANNEL_ID = "UC" + "A" * 22
TEST_DIRECTORY = Path(__file__).absolute().parent.parent / ".test-tmp"


def entries():
    return [
        QueueEntry(1, ROOT + "\\Game\\first\u200b.mp4", "Game 2020-01-01 00:00"),
        QueueEntry(2, ROOT + "\\Game\\second.mp4", "Game 2020-01-01 00:01"),
        QueueEntry(3, ROOT + "\\Other Game\\third.mp4", "Other Game 2020-01-02 00:00"),
    ]


class QueueTests(unittest.TestCase):
    def setUp(self):
        TEST_DIRECTORY.mkdir(exist_ok=True)
        _safe_project(TEST_DIRECTORY)
        self.temp = tempfile.TemporaryDirectory(prefix="youtube-queue-test-", dir=TEST_DIRECTORY)
        self.project = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write_manifest(self, queue=None):
        queue = entries() if queue is None else queue
        text = f"# Source root: {ROOT}\n# Channel ID: {CHANNEL_ID}\n\n" + "\n".join(f"{e.number:03d} | {e.path} | {e.title}" for e in queue)
        (self.project / MANIFEST_NAME).write_text(text, encoding="utf-8")

    def write_plan_text(self, text):
        (self.project / MANIFEST_NAME).write_text(text, encoding="utf-8")

    def store(self):
        store = StateStore(self.project, entries())
        self.addCleanup(store.close)
        store.bind_channel("UCtest_channel")
        return store

    def begin(self, store):
        entry = entries()[0]
        store.start(entry, 100, 42)
        store.save_session(entry, "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=secret")
        return entry

    def test_manifest_preserves_unicode_and_never_checks_source(self):
        self.assertEqual(MANIFEST_NAME, "upload-order.txt")
        self.write_manifest()
        real_lstat = os.lstat
        def guarded(path, *args, **kwargs):
            self.assertFalse(str(path).casefold().startswith(ROOT.casefold()))
            return real_lstat(path, *args, **kwargs)
        with patch("data.uploader_queue.os.lstat", side_effect=guarded):
            self.assertEqual(load_queue(self.project), entries())

    def test_rejects_unsafe_or_ambiguous_paths(self):
        paths = [ROOT + "Extra\\a.mp4", ROOT + "\\Game\\..\\a.mp4",
                 ROOT + "\\Game\\a.mp4:stream", ROOT + "\\Game\\a.png", ROOT + "\\Game\\a.\\b.mp4",
                 ROOT + "\\Game\\CON.mp4"]
        for path in paths:
            with self.subTest(path=path):
                self.write_manifest([replace(entries()[0], path=path)])
                with self.assertRaises(QueueError):
                    load_queue(self.project)

    def test_rejects_bad_numbering_invalid_titles_and_duplicate_paths(self):
        for queue in ([replace(entries()[0], number=2)],
                      [replace(entries()[0], title="")],
                      [replace(entries()[0], title=" ")],
                      [replace(entries()[0], title="x" * 101)],
                      [replace(entries()[0], title="Invalid <title>")],
                      [entries()[0], replace(entries()[1], path=entries()[0].path.upper())],
                      [replace(entries()[0], title="Invalid\ntitle")]):
            with self.subTest(queue=queue):
                self.write_manifest(queue)
                with self.assertRaises(QueueError):
                    load_queue(self.project)

    def test_completed_prefix_and_current_session_survive_restart(self):
        store = self.store()
        first = self.begin(store)
        store.progress(first, 80)
        store.progress(first, 40)  # Server status can move the confirmed offset back.
        store.block(first, "network_interrupted")
        self.assertEqual(store.pending(2), entries()[:2])
        store.close()
        store = StateStore(self.project, entries())
        self.addCleanup(store.close)
        self.assertEqual(store.get(first)["offset"], 40)
        store.progress(first, 100)
        self.assertEqual(store.pending(1), [first])  # 100% is not completion.
        store.complete(first, "video_1")
        self.assertEqual(store.pending(2), entries()[1:])
        self.assertEqual(store.get(first)["status"], "completed")
        self.assertIsNone(store.get(first)["session_url"])
        self.assertEqual(store.summary(), {"total": 3, "completed": 1, "remaining": 2, "blocked": 0, "current": 2})

    def test_uncertain_initialization_cannot_be_started_again(self):
        store = self.store()
        first = entries()[0]
        store.start(first, 100, 42)
        store.close()
        store = StateStore(self.project, entries())
        self.addCleanup(store.close)
        self.assertEqual(store.get(first)["status"], "initiating")
        with self.assertRaises(QueueError):
            store.start(first, 100, 42)
        with self.assertRaises(QueueError):
            store.start(entries()[1], 100, 42)

    def test_channel_and_entire_started_queue_are_pinned(self):
        store = self.store()
        with self.assertRaises(QueueError):
            store.bind_channel("UCdifferent")
        self.begin(store)
        store.close()
        changed = entries()
        changed[2] = replace(changed[2], title="Other Game 2020-01-03 00:00")
        with self.assertRaises(QueueError):
            StateStore(self.project, changed)

    def test_queue_can_change_before_any_upload_start(self):
        store = self.store()
        store.close()
        changed = entries()
        changed[2] = replace(changed[2], title="Other Game 2020-01-03 00:00")
        store = StateStore(self.project, changed)
        self.addCleanup(store.close)
        self.assertEqual(store.pending(3), changed)
        with self.assertRaises(QueueError):
            store.bind_channel("UCdifferent")

    def test_stale_store_cannot_start_after_an_unstarted_queue_edit(self):
        original = self.store()
        changed = entries()
        changed[2] = replace(changed[2], title="Other Game 2020-01-03 00:00")
        updated = StateStore(self.project, changed)
        self.addCleanup(updated.close)
        with self.assertRaises(QueueError):
            original.start(entries()[0], 100, 42)

    def test_source_root_is_rejected_before_any_filesystem_check(self):
        if os.name != "nt":
            self.skipTest("Windows source path interpretation requires Windows")
        with patch("data.uploader_queue.os.lstat", side_effect=AssertionError("Filesystem access")):
            with self.assertRaises(QueueError):
                StateStore(Path(ROOT), entries())

    def test_wrong_remote_metadata_is_blocked_with_id_without_reupload(self):
        store = self.store()
        first = self.begin(store)
        store.record_remote_issue(first, "remote_video", "metadata_mismatch")
        self.assertEqual(store.get(first)["video_id"], "remote_video")
        self.assertEqual(store.get(first)["status"], "blocked")
        self.assertEqual(store.pending(2), entries()[:2])
        for operation in (lambda: store.start(first, 100, 42), lambda: store.progress(first, 0),
                          lambda: store.complete(first, "other_video"), lambda: store.start(entries()[1], 100, 42)):
            with self.assertRaises(QueueError):
                operation()

    def test_invalid_state_fails_closed(self):
        store = self.store()
        self.begin(store)
        database = store.path
        store.close()
        connection = sqlite3.connect(database)
        try:
            connection.execute("UPDATE uploads SET offset=101")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(QueueError):
            StateStore(self.project, entries())

    def test_remote_video_id_cannot_complete_two_queue_entries(self):
        store = self.store()
        first = self.begin(store)
        store.complete(first, "video_unique")
        second = entries()[1]
        store.start(second, 100, 43)
        store.save_session(second, "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=second")
        with self.assertRaises(QueueError):
            store.complete(second, "video_unique")
        self.assertEqual(store.get(second)["status"], "uploading")

    def test_atomic_write_failure_leaves_original_state(self):
        store = self.store()
        first = self.begin(store)
        calls = 0
        original_check = store._check_storage
        def fail_before_commit():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise QueueError("Synthetic unsafe state directory")
            original_check()
        with patch.object(store, "_check_storage", side_effect=fail_before_commit):
            with self.assertRaises(QueueError):
                store.progress(first, 90)
        self.assertEqual(store.get(first)["offset"], 0)

    def test_project_lock_excludes_second_holder_and_releases(self):
        with ProjectLock(self.project):
            with self.assertRaises(QueueError):
                with ProjectLock(self.project):
                    self.fail("Second lock succeeded")
        with ProjectLock(self.project):
            pass

    def test_state_symlink_is_rejected_without_target_writes(self):
        with tempfile.TemporaryDirectory(prefix="youtube-queue-external-", dir=TEST_DIRECTORY) as external:
            target = Path(external)
            try:
                (self.project / ".uploader-state").symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Creating a directory symlink is unavailable")
            with self.assertRaises(QueueError):
                StateStore(self.project, entries())
            self.assertEqual(list(target.iterdir()), [])

    def test_database_hard_link_is_rejected(self):
        store = self.store()
        database = store.path
        store.close()
        try:
            os.link(database, self.project / "state-copy.sqlite3")
        except (OSError, NotImplementedError):
            self.skipTest("Creating a hard link is unavailable")
        with self.assertRaises(QueueError):
            StateStore(self.project, entries())

    def test_confirmed_retry_preserves_164_completed_rows_and_full_backup(self):
        start = datetime(2020, 1, 1)
        queue = [
            QueueEntry(number, ROOT + f"\\Synthetic\\clip{number:03d}.mp4",
                       "Synthetic " + (start + timedelta(minutes=number)).strftime("%Y-%m-%d %H:%M"))
            for number in range(1, 200)
        ]
        with StateStore(self.project, queue) as store:
            store.bind_channel("UCtest_channel")
            # Seed synthetic historical completions; no source files or services exist.
            with store._transaction() as connection:
                connection.executemany(
                    "INSERT INTO uploads (number,path,title,status,size,mtime_ns,offset,video_id) "
                    "VALUES (?,?,?,'completed',100,42,100,?)",
                    [(entry.number, entry.path, entry.title, f"synthetic_{entry.number}") for entry in queue[:164]],
                )
            current = queue[164]
            store.start(current, 200, 43)
            store.block(current, RETRYABLE_INITIALIZATION_BLOCK_REASON)
            before_rows, before_metadata = store._snapshot(store._connection)
            self.assertTrue(store.can_retry_blocked(current))
            backup = store.reset_blocked_for_retry(current, confirmed_not_uploaded=True)
            self.assertEqual(backup.parent, store.state_dir)
            self.assertEqual(store._verified_backup_snapshot(backup), (before_rows, before_metadata))
            self.assertEqual(store._snapshot(store._connection), (before_rows[:-1], before_metadata))
            self.assertEqual(store.summary()["completed"], 164)
            self.assertEqual(store.summary()["current"], 165)
            self.assertIsNone(store.get(current))
            self.assertEqual(store.pending(10), queue[164:174])
            self.assertFalse(store.can_retry_blocked(current))
            self.assertFalse(store.can_retry_blocked(queue[163]))

    def test_retry_requires_literal_true_confirmation_and_never_clears_on_inspection(self):
        store = self.store()
        current = entries()[0]
        store.start(current, 100, 42)
        store.block(current, RETRYABLE_INITIALIZATION_BLOCK_REASON)
        before = store._snapshot(store._connection)
        for confirmation in (False, None, 1, "yes"):
            with self.subTest(confirmation=confirmation):
                with self.assertRaises(QueueError):
                    store.reset_blocked_for_retry(current, confirmed_not_uploaded=confirmation)
                self.assertTrue(store.can_retry_blocked(current))
                self.assertEqual(store._snapshot(store._connection), before)
        self.assertEqual(list(store.state_dir.glob("retry-backup-*")), [])

    def test_retry_rejects_uncertain_sessions_remote_ids_bytes_and_other_rows(self):
        cases = ("untouched", "initiating", "unknown_reason", "session", "bytes", "orphaned_bytes", "remote_id", "completed")
        for case in cases:
            with self.subTest(case=case):
                project = self.project / case
                project.mkdir()
                with StateStore(project, entries()) as store:
                    store.bind_channel("UCtest_channel")
                    current = entries()[0]
                    if case != "untouched":
                        store.start(current, 100, 42)
                    if case in {"session", "bytes", "remote_id", "completed"}:
                        store.save_session(current, "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=synthetic")
                    if case == "bytes":
                        store.progress(current, 1)
                    if case == "remote_id":
                        store.record_remote_issue(current, "synthetic_remote", RETRYABLE_INITIALIZATION_BLOCK_REASON)
                    elif case == "completed":
                        store.complete(current, "synthetic_completed")
                    elif case not in {"untouched", "initiating"}:
                        reason = "Interrupted initiation; remote outcome unknown." if case == "unknown_reason" else RETRYABLE_INITIALIZATION_BLOCK_REASON
                        store.block(current, reason)
                    if case == "orphaned_bytes":
                        store._connection.execute("UPDATE uploads SET offset=1 WHERE number=1")
                    before = store._snapshot(store._connection)
                    self.assertFalse(store.can_retry_blocked(current))
                    self.assertFalse(store.can_retry_blocked(entries()[2]))
                    with self.assertRaises(QueueError):
                        store.reset_blocked_for_retry(current, confirmed_not_uploaded=True)
                    with self.assertRaises(QueueError):
                        store.reset_blocked_for_retry(entries()[2], confirmed_not_uploaded=True)
                    self.assertEqual(store._snapshot(store._connection), before)
                    self.assertEqual(list(store.state_dir.glob("retry-backup-*")), [])

    def test_exact_http_403_initialization_refusal_can_be_confirmed_for_retry(self):
        store = self.store()
        current = entries()[0]
        store.start(current, 100, 42)
        store.block(current, "YouTube refused upload initialization (HTTP 403). Check the API/channel "
                             "upload limit and permissions. Resolve this row before retrying.")
        self.assertTrue(store.can_retry_blocked(current))
        store.reset_blocked_for_retry(current, confirmed_not_uploaded=True)
        self.assertIsNone(store.get(current))

    def test_retry_backup_failure_keeps_block_and_sanitizes_error(self):
        store = self.store()
        current = entries()[0]
        store.start(current, 100, 42)
        store.block(current, RETRYABLE_INITIALIZATION_BLOCK_REASON)
        before = store._snapshot(store._connection)
        with patch("data.uploader_queue.os.fsync", side_effect=OSError("https://secret.invalid/token")):
            with self.assertRaises(QueueError) as caught:
                store.reset_blocked_for_retry(current, confirmed_not_uploaded=True)
        self.assertNotIn("https://", str(caught.exception))
        self.assertEqual(store._snapshot(store._connection), before)
        self.assertTrue(store.can_retry_blocked(current))

    def test_retry_rolls_back_deletion_if_final_storage_check_fails(self):
        store = self.store()
        current = entries()[0]
        store.start(current, 100, 42)
        store.block(current, RETRYABLE_INITIALIZATION_BLOCK_REASON)
        before = store._snapshot(store._connection)
        original_check = store._check_storage
        def refuse_commit_after_deletion():
            if store._connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0:
                raise QueueError("Synthetic storage validation failure")
            original_check()
        with patch.object(store, "_check_storage", side_effect=refuse_commit_after_deletion):
            with self.assertRaises(QueueError):
                store.reset_blocked_for_retry(current, confirmed_not_uploaded=True)
        self.assertEqual(store._snapshot(store._connection), before)
        self.assertTrue(store.can_retry_blocked(current))

    def test_retry_refuses_state_changes_after_backup(self):
        store = self.store()
        current = entries()[0]
        store.start(current, 100, 42)
        store.block(current, RETRYABLE_INITIALIZATION_BLOCK_REASON)
        before_rows = store.rows()
        original_backup = store.backup_state
        def backup_then_external_change():
            backup = original_backup()
            store._connection.execute("INSERT INTO metadata VALUES ('synthetic_concurrent_change', '1')")
            return backup
        with patch.object(store, "backup_state", side_effect=backup_then_external_change):
            with self.assertRaises(QueueError):
                store.reset_blocked_for_retry(current, confirmed_not_uploaded=True)
        self.assertEqual(store.rows(), before_rows)
        self.assertTrue(store.can_retry_blocked(current))

    def test_state_backups_are_unique_verified_and_do_not_overwrite_existing_files(self):
        store = self.store()
        current = self.begin(store)
        before = store._snapshot(store._connection)
        first = store.backup_state()
        second = store.backup_state()
        self.assertNotEqual(first, second)
        self.assertEqual(store._verified_backup_snapshot(first), before)
        self.assertEqual(store._verified_backup_snapshot(second), before)
        fixed = store.checked_state_file("retry-backup-" + "a" * 32 + ".sqlite3")
        fixed.write_bytes(b"existing backup must remain untouched")
        with patch("data.uploader_queue.uuid.uuid4") as generate:
            generate.return_value.hex = "a" * 32
            with self.assertRaises(QueueError):
                store.backup_state()
        self.assertEqual(fixed.read_bytes(), b"existing backup must remain untouched")
        self.assertEqual(store.get(current)["status"], "uploading")

    def test_portable_plan_uses_declared_root_and_channel_without_rewriting_absolute_paths(self):
        absolute = "e:/Portable Clips/Game/clip\u200b.mov"
        channel = "UC" + "B" * 22
        self.write_plan_text(
            f"# Source root: E:\\Portable Clips\n# Channel ID: {channel}\n"
            f"# Destination channel: Example Channel\n001 | {absolute} | A custom title\n"
        )
        plan = load_upload_plan(self.project)
        self.assertIsInstance(plan, UploadPlan)
        self.assertEqual(plan.source_root, PureWindowsPath(r"E:\Portable Clips"))
        self.assertEqual(plan.channel_id, channel)
        self.assertFalse(hasattr(plan, "channel_name"))
        self.assertEqual(plan.entries, [QueueEntry(1, absolute, "A custom title")])

    def test_relative_unicode_paths_are_joined_lexically_and_source_is_never_inspected(self):
        relative = "Sub Dir\\clip\u200b 漢字.mkv"
        self.write_plan_text(f"# Source root: {ROOT}\n# Channel ID: {CHANNEL_ID}\n001 | {relative} | Unicode clip\n")
        original_lstat = os.lstat
        original_stat = os.stat
        def guard(function):
            def checked(path, *args, **kwargs):
                self.assertFalse(str(path).casefold().startswith(ROOT.casefold()))
                return function(path, *args, **kwargs)
            return checked
        with patch("data.uploader_queue.os.lstat", side_effect=guard(original_lstat)), \
                patch("data.uploader_queue.os.stat", side_effect=guard(original_stat)), \
                patch.object(Path, "resolve", side_effect=AssertionError("No path resolution is allowed")):
            plan = load_upload_plan(self.project)
        self.assertEqual(plan.entries[0].path, ROOT + "\\" + relative)
        self.assertFalse(hasattr(plan, "channel_name"))

    def test_legacy_source_root_header_and_headerless_absolute_queue_compatibility(self):
        self.write_plan_text(
            f"# Original source root: {ROOT}\n# Channel ID: {CHANNEL_ID}\n"
            f"001 | {entries()[0].path} | Historical title\n"
        )
        self.assertEqual(load_upload_plan(self.project).source_root, PureWindowsPath(ROOT))
        self.write_plan_text(f"001 | {entries()[0].path} | Historical title\n")
        self.assertEqual(load_queue(self.project), [replace(entries()[0], title="Historical title")])
        with self.assertRaises(QueueError):
            load_upload_plan(self.project)

    def test_missing_malformed_or_conflicting_plan_headers_fail(self):
        row = f"001 | {entries()[0].path} | Example\n"
        good_root = f"# Source root: {ROOT}\n"
        good_channel = f"# Channel ID: {CHANNEL_ID}\n"
        configurations = [
            good_root, good_channel, "", good_root + "# Channel ID: invalid\n",
            "# Source root: relative\\clips\n" + good_channel,
            "# Source root\n" + good_channel,
            good_root + good_channel + "# Original source root: E:\\Other Clips\n",
            good_root + good_channel + "# Channel ID: UC" + "B" * 22 + "\n",
        ]
        for headers in configurations:
            with self.subTest(headers=headers):
                self.write_plan_text(headers + row)
                with self.assertRaises(QueueError):
                    load_upload_plan(self.project)

    def test_legacy_destination_display_comments_are_ignored_without_configuration(self):
        row = f"001 | {entries()[0].path} | Example\n"
        headers = f"# Source root: {ROOT}\n# Channel ID: {CHANNEL_ID}\n"
        self.write_plan_text(headers + row)
        id_only = load_upload_plan(self.project)
        self.write_plan_text(headers + "# Destination channel: One\n# Destination channel: Two\n"
                             "# Destination channel:\n" + row)
        with_legacy_comments = load_upload_plan(self.project)
        self.assertEqual(with_legacy_comments, id_only)
        self.assertFalse(hasattr(with_legacy_comments, "channel_name"))

    def test_relative_path_escapes_and_other_roots_fail(self):
        paths = [r"..\outside.mp4", r"Game\..\outside.mp4", r"\outside.mp4",
                 r"D:outside.mp4", r"E:\Other\outside.mp4", ROOT + "Extra\\outside.mp4",
                 r'"Game\quoted.mp4"', r"Game\clip.mp4:alternate", r"Game\.\clip.mp4"]
        for path in paths:
            with self.subTest(path=path):
                self.write_plan_text(f"# Source root: {ROOT}\n# Channel ID: {CHANNEL_ID}\n001 | {path} | Example\n")
                with self.assertRaises(QueueError):
                    load_upload_plan(self.project)

    def test_common_video_formats_and_plain_titles_preserve_explicit_row_order(self):
        extensions = ("mp4", "mkv", "mov", "webm", "avi", "m4v", "wmv", "mts", "m2ts")
        titles = ["Later 2026-01-01 00:00", "Earlier 2020-01-01 00:00", "  Preserve title spaces  "]
        rows = [f"{number:03d} | folder\\clip{number}.{extension} | {titles[number - 1] if number <= 3 else 'Clip ' + str(number)}"
                for number, extension in enumerate(extensions, 1)]
        self.write_plan_text(f"# Source root: {ROOT}\n# Channel ID: {CHANNEL_ID}\n" + "\n".join(rows))
        plan = load_upload_plan(self.project)
        self.assertEqual([entry.number for entry in plan.entries], list(range(1, 10)))
        self.assertEqual([entry.title for entry in plan.entries[:3]], titles)
        self.assertEqual([PureWindowsPath(entry.path).suffix for entry in plan.entries], ["." + extension for extension in extensions])

    def test_unc_source_root_is_lexical_and_never_requires_network_access(self):
        self.write_plan_text(
            f"# Source root: \\\\example-server\\share\\clips\n# Channel ID: {CHANNEL_ID}\n"
            "001 | Game\\clip.webm | Network-root clip\n"
        )
        self.assertEqual(load_upload_plan(self.project).entries[0].path,
                         r"\\example-server\share\clips\Game\clip.webm")

    def test_channel_check_does_not_bind_or_change_metadata(self):
        with StateStore(self.project, entries(), source_root=ROOT) as store:
            before = store._snapshot(store._connection)
            store.check_channel(CHANNEL_ID)
            self.assertEqual(store._snapshot(store._connection), before)
            store.bind_channel(CHANNEL_ID)
            bound = store._snapshot(store._connection)
            store.check_channel(CHANNEL_ID)
            with self.assertRaises(QueueError):
                store.check_channel("UC" + "B" * 22)
            self.assertEqual(store._snapshot(store._connection), bound)

    def test_existing_199_completions_fingerprint_schema_and_metadata_survive_portable_plan(self):
        queue = [QueueEntry(number, ROOT + f"\\Game\\clip{number:03d}.mp4", f"Original title {number}")
                 for number in range(1, 200)]
        expected_fingerprint = hashlib.sha256(json.dumps(
            [[entry.number, entry.path, entry.title] for entry in queue],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        with StateStore(self.project, queue) as store:
            store.bind_channel(CHANNEL_ID)
            with store._transaction() as connection:
                connection.executemany(
                    "INSERT INTO uploads (number,path,title,status,size,mtime_ns,offset,video_id) "
                    "VALUES (?,?,?,'completed',100,42,100,?)",
                    [(entry.number, entry.path, entry.title, f"synthetic_{entry.number}") for entry in queue],
                )
            before = store._snapshot(store._connection)
            self.assertEqual(store.fingerprint, expected_fingerprint)
        self.write_manifest(queue)
        plan = load_upload_plan(self.project)
        self.assertEqual(plan.entries, queue)
        with StateStore(self.project, plan.entries, source_root=plan.source_root) as store:
            store.check_channel(plan.channel_id)
            self.assertEqual(store.fingerprint, expected_fingerprint)
            self.assertEqual(store._snapshot(store._connection), before)
            self.assertEqual(dict(before[1])["schema_version"], "1")
            self.assertEqual(store.pending(10), [])
            self.assertEqual(store.summary()["completed"], 199)

    def test_explicit_source_root_protects_state_and_lock_before_filesystem_access(self):
        if os.name != "nt":
            self.skipTest("Windows source path interpretation requires Windows")
        with patch("data.uploader_queue.os.lstat", side_effect=AssertionError("Unexpected filesystem access")):
            with self.assertRaises(QueueError):
                StateStore(Path(ROOT) / "uploader", entries(), source_root=ROOT)
            with self.assertRaises(QueueError):
                with ProjectLock(Path(ROOT) / "uploader", source_root=ROOT):
                    self.fail("A lock was created inside the source")

    def test_source_inside_project_is_rejected_before_state_or_source_creation(self):
        if os.name != "nt":
            self.skipTest("Windows source path interpretation requires Windows")
        source = PureWindowsPath(str(self.project)) / ".uploader-state"
        queue = [QueueEntry(1, str(source / "clip.mp4"), "Synthetic clip")]
        with patch("data.uploader_queue.os.lstat", side_effect=AssertionError("Unexpected filesystem access")), \
                patch("data.uploader_queue.os.mkdir", side_effect=AssertionError("Unexpected directory creation")):
            with self.assertRaises(QueueError):
                StateStore(self.project, queue, source_root=source)
            with self.assertRaises(QueueError):
                with ProjectLock(self.project, source_root=source):
                    self.fail("A lock was created inside the source")
        self.assertEqual(list(self.project.iterdir()), [])

    def test_overlap_comparison_is_case_insensitive_and_unc_component_based(self):
        cases = [
            (r"C:\Uploader", r"c:\UPLOADER\clips"),
            (r"C:\Clips\Uploader", r"c:\CLIPS"),
            (r"\\Server\Share\Uploader", r"\\server\share\UPLOADER\clips"),
            (r"\\Server\Share\Clips\Uploader", r"\\server\share\clips"),
        ]
        if os.name != "nt":
            self.skipTest("Windows project path interpretation requires Windows")
        with patch("data.uploader_queue.os.lstat", side_effect=AssertionError("Unexpected filesystem access")):
            for project, source in cases:
                with self.subTest(project=project, source=source):
                    with self.assertRaises(QueueError):
                        _safe_project(Path(project), PureWindowsPath(source))


if __name__ == "__main__":
    unittest.main()
