"""User-started upload batches, launched from the project root with python -m."""

from __future__ import annotations

import argparse
from datetime import datetime
import logging
import os
from pathlib import Path
import re
import sys
import tempfile

from .uploader_connection import CheckError, load_client_config, read_channel
from .uploader_auth import authorized_session
from .uploader_queue import ProjectLock, QueueEntry, QueueError, StateStore, load_upload_plan
from .uploader_transport import ResumableUploader, UploadError


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_BATCH_SIZE = 10
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


def verify_channel(session, store: StateStore, expected_channel_id: str) -> None:
    _, channel_id = read_channel(session)
    if channel_id != expected_channel_id:
        raise CheckError(
            "The selected channel does not match Channel ID in upload-order.txt. "
            "This batch has not started. Run again and select the configured YouTube channel."
        )
    store.bind_channel(expected_channel_id)


def _positive_count(value: str) -> int:
    try:
        count = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("Batch size must be a positive whole number.") from None
    if count <= 0 or not str(value).strip().isascii() or not str(value).strip().isdigit():
        raise argparse.ArgumentTypeError("Batch size must be a positive whole number.")
    return count


def show_status(store: StateStore, output=print) -> None:
    summary = store.summary()
    output(
        f"Queue: {summary['total']} videos | Uploaded: {summary['completed']} | "
        f"Remaining: {summary['remaining']}"
    )
    if summary["current"] is None:
        output("All queued uploads are complete.")
        return
    entry = store.entries[summary["current"] - 1]
    record = store.get(entry)
    status = record["status"] if record else "not started"
    output(f"Next/current: {entry.number:03d} | {entry.title} | {status}")
    if record and record["status"] == "blocked":
        output("This row needs manual review before any further uploads.")
        output(record["reason"] or "The current upload could not be confirmed.")
        if record.get("video_id") and _VIDEO_ID.fullmatch(record["video_id"]):
            output(f"Recorded video: https://www.youtube.com/watch?v={record['video_id']}")


def show_preview(store: StateStore, batch_size: int, output=print,
                 *, destination: str = "configured channel") -> list[QueueEntry]:
    batch = store.pending(batch_size)
    output("")
    output(f"Destination: {destination} | Visibility: unlisted")
    output(f"Selected batch: {len(batch)} video(s), requested limit {batch_size}.")
    for entry in batch:
        output(f"  {entry.number:03d} | {entry.title}")
    if batch:
        output("One upload at a time; pause after this batch. Processing does not delay the next upload.")
    return batch


def check_current(store: StateStore, batch: list[QueueEntry]) -> None:
    if not batch:
        return
    record = store.get(batch[0])
    if record and record["status"] == "blocked":
        raise QueueError(
            "The current row is blocked. Check its status and YouTube Studio, "
            "then resolve the recorded issue before continuing. Do not delete the state folder."
        )
    if record and record["status"] == "initiating":
        raise QueueError(
            "The current row has an unconfirmed upload initialization. "
            "Manual review is required; another upload will not be started."
        )


def _console_keys(output=print):
    if os.name != "nt" or not sys.stdin.isatty():
        output("An interactive Windows console is required. Progress is saved; reopen start.cmd to continue.")
        return None
    import msvcrt
    return msvcrt


def confirm_block_retry(entry: QueueEntry, output=print) -> bool:
    """Require a fresh explicit confirmation before clearing an eligible failed attempt."""
    keys = _console_keys(output)
    if keys is None:
        return False
    while keys.kbhit():
        keys.getwch()
    output("")
    output(f"Blocked video: {entry.number:03d} | {entry.title}")
    output("Clear this block only if you confirmed that this video did NOT upload and the upload limit has reset.")
    output("Completed uploads will be kept. The next batch will begin with this same video.")
    output("ENTER: confirm and clear this failed attempt | ESC: keep the block and exit")
    sys.stdout.flush()
    while True:
        key = keys.getwch()
        if key in ("\r", "\n"):
            return True
        if key == "\x1b":
            return False
        if key == "\x03":
            raise KeyboardInterrupt
        if key in ("\x00", "\xe0"):
            keys.getwch()


def recover_current_block(store: StateStore, *, output=print, retry_fn=None) -> bool:
    """Offer recovery only for the eligible current failed initialization, never advance it."""
    pending = store.pending(1)
    if not pending:
        return True
    entry = pending[0]
    record = store.get(entry)
    if record is None or record["status"] != "blocked" or not store.can_retry_blocked(entry):
        return True  # All other blocked/uncertain cases still fail check_current().
    confirm = retry_fn or confirm_block_retry
    if confirm(entry, output=output) is not True:
        return False
    store.reset_blocked_for_retry(entry, confirmed_not_uploaded=True)
    output(f"Block cleared for {entry.number:03d}. Progress was backed up; completed uploads are unchanged.")
    show_status(store, output)
    return True


def wait_for_batch_action(batch_size: int, *, signed_in: bool, output=print) -> str:
    """Read one fresh menu action without requiring a line or confirmation phrase."""
    keys = _console_keys(output)
    if keys is None:
        return "exit"
    # Keys pressed while the upload was running must not start another batch.
    while keys.kbhit():
        keys.getwch()
    output("")
    start = "upload" if signed_in else "sign in and upload"
    output(
        f"ENTER: {start} {batch_size} video(s) | B: change batch size | ESC: exit and continue later"
    )
    sys.stdout.flush()
    while True:
        key = keys.getwch()
        if key in ("\r", "\n"):
            return "start"
        if key == "\x1b":
            return "exit"
        if key in ("b", "B"):
            return "size"
        if key == "\x03":
            raise KeyboardInterrupt
        if key in ("\x00", "\xe0"):
            keys.getwch()  # Consume the second half of an extended key.


def read_batch_size(current: int, output=print) -> int | None:
    """Edit a positive batch size; Escape exits immediately even while editing."""
    keys = _console_keys(output)
    if keys is None:
        return None
    output(f"New batch size [{current}]: type a number and press ENTER; ESC exits.")
    sys.stdout.flush()
    digits = ""
    while True:
        key = keys.getwch()
        if key == "\x1b":
            output("")
            return None
        if key == "\x03":
            raise KeyboardInterrupt
        if key in ("\x00", "\xe0"):
            keys.getwch()
        elif key in ("\r", "\n"):
            output("")
            if not digits:
                return current
            count = int(digits)
            if count > 0:
                return count
            output("Enter a positive whole number, such as 10; ESC exits.")
            digits = ""
        elif key == "\b" and digits:
            digits = digits[:-1]
            sys.stdout.write("\b \b")
        elif key in "0123456789" and len(key) == 1 and len(digits) < 9:
            digits += key
            sys.stdout.write(key)
        sys.stdout.flush()


def choose_batch(store: StateStore, count: int, *, signed_in: bool, output=print,
                 action_fn=None, size_fn=None, retry_fn=None,
                 destination: str = "configured channel") -> int | None:
    """Preview each selection before accepting Enter; B never starts sign-in or transfer."""
    action_reader = action_fn or wait_for_batch_action
    size_reader = size_fn or read_batch_size
    while True:
        if not recover_current_block(store, output=output, retry_fn=retry_fn):
            return None
        batch = show_preview(store, count, output, destination=destination)
        check_current(store, batch)
        action = action_reader(len(batch), signed_in=signed_in, output=output)
        if action == "exit":
            return None
        if action == "start":
            return count
        if action != "size":
            raise QueueError("The menu action was not recognized. No additional upload was started.")
        updated = size_reader(count, output=output)
        if updated is None:
            return None
        if type(updated) is not int or updated <= 0:
            raise QueueError("The batch size must be a positive whole number.")
        count = updated


def write_results(store: StateStore, batch: list[QueueEntry],
                  *, destination: str = "configured channel") -> Path:
    """Export a readable review list, with durable SQLite state remaining authoritative."""
    report = store.checked_state_file("last-batch.txt")
    lines = [
        "YouTube upload batch", f"Updated: {datetime.now().isoformat(timespec='seconds')}",
        f"Channel: {destination}", "Requested visibility: unlisted",
        "Only final confirmed uploads count as completed. Processing can still be underway.", "",
    ]
    for entry in batch:
        record = store.get(entry)
        lines.append(f"{entry.number:03d} | {entry.title}")
        if record and record["status"] == "completed":
            lines.append(f"Uploaded | https://www.youtube.com/watch?v={record['video_id']}")
        elif record and record.get("video_id"):
            lines.append(f"NEEDS REVIEW | https://www.youtube.com/watch?v={record['video_id']}")
        else:
            status = record["status"] if record else "not started"
            lines.append(f"Not confirmed complete | {status}")
        lines.append("")
    # Create only in the checked project-local state folder; never in the source.
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix="batch-", suffix=".tmp", dir=report.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        store.checked_state_file(temporary.name)
        store.checked_state_file(report.name)
        os.replace(temporary, report)
        temporary = None
    except OSError:
        raise QueueError(
            "The review file could not be saved. Completed uploads remain recorded "
            "in the database; do not delete the state folder or restart those videos."
        ) from None
    finally:
        if temporary is not None:
            # This is the exact project-local temporary file just created above.
            try:
                store.checked_state_file(temporary.name).unlink(missing_ok=True)
            except (OSError, QueueError):
                pass
    return report


def run_batch(store: StateStore, uploader, batch_size: int, output=print,
              *, destination: str = "configured channel") -> int:
    """Complete at most N current rows; no later call can overlap the current upload."""
    batch = store.pending(batch_size)
    check_current(store, batch)
    if not batch:
        return 0
    write_results(store, batch, destination=destination)
    completed = 0
    try:
        for entry in batch:
            output("")
            output(f"Uploading {entry.number:03d}: {entry.title}")
            video_id = uploader.upload(entry)
            record = store.get(entry)
            if (
                not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id)
                or record is None or record["status"] != "completed"
                or record["video_id"] != video_id
            ):
                raise UploadError(
                    "The upload did not have durable confirmed completion. "
                    "The batch has stopped on this row."
                )
            completed += 1
            write_results(store, batch, destination=destination)
            output(f"Uploaded unlisted: https://www.youtube.com/watch?v={video_id}")
    finally:
        # Include any known remote ID even when a response needs manual review.
        write_results(store, batch, destination=destination)
    output("")
    output(f"Batch finished: {completed} video(s) uploaded. Uploads are paused.")
    output("Review this batch in YouTube Studio before starting another batch.")
    output(f"Review file: {store.checked_state_file('last-batch.txt')}")
    return completed


def main(argv=None, *, project_dir: Path = PROJECT_DIR, output=print, action_fn=None,
         size_fn=None, retry_fn=None) -> int:
    parser = argparse.ArgumentParser(description="Upload a selected batch to the configured channel, unlisted, in queue order.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--preview", action="store_true", help="Offline queue preview; no sign-in or video access.")
    modes.add_argument("--status", action="store_true", help="Offline saved progress; no sign-in or video access.")
    parser.add_argument("--batch-size", type=_positive_count, help="Maximum uploads per batch (default 10).")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        plan = load_upload_plan(project_dir)
        destination = plan.channel_id
        with ProjectLock(project_dir, source_root=plan.source_root), StateStore(
            project_dir, plan.entries, source_root=plan.source_root
        ) as store:
            store.check_channel(plan.channel_id)
            show_status(store, output)
            if args.status:
                output("Offline status only. No sign-in or source-video access.")
                return 0
            if not store.summary()["remaining"]:
                return 0
            count = args.batch_size or DEFAULT_BATCH_SIZE
            if args.preview:
                show_preview(store, count, output, destination=destination)
                output("Offline preview only. No sign-in or source-video access.")
                return 0
            output("")
            output("YouTube archive uploader | Existing source videos are opened read-only only during upload.")
            count = choose_batch(store, count, signed_in=False, output=output,
                                 action_fn=action_fn, size_fn=size_fn, retry_fn=retry_fn,
                                 destination=destination)
            if count is None:
                output("Exiting. Progress is saved. No sign-in or source-video access.")
                return 0
            config = load_client_config(project_dir)
            output("Copy the sign-in link below into your browser of choice.")
            output("Select the Google account and YouTube channel configured in the upload-order file.")
            output("Both YouTube read-only and upload permissions are needed. Login tokens stay in memory.")
            output("Keep this console open while signing in. Waiting up to five minutes; Ctrl+C cancels.")
            with authorized_session(config) as session:
                uploader = None
                while True:
                    verify_channel(session, store, plan.channel_id)
                    if uploader is None:
                        uploader = ResumableUploader(session, store, output=output, expected_channel_id=plan.channel_id)
                    output(f"Confirmed destination: {destination}. Starting the selected batch.")
                    try:
                        run_batch(store, uploader, count, output, destination=destination)
                    except UploadError as error:
                        pending = store.pending(1)
                        if not pending or not store.can_retry_blocked(pending[0]):
                            raise
                        # Stay on this clip and wait for a reviewed manual recovery.
                        output(f"Uploads paused: {error}")
                    show_status(store, output)
                    if not store.summary()["remaining"]:
                        return 0
                    count = choose_batch(store, count, signed_in=True, output=output,
                                         action_fn=action_fn, size_fn=size_fn, retry_fn=retry_fn,
                                         destination=destination)
                    if count is None:
                        output("Exiting. Progress is saved; reopen the uploader whenever you are ready to continue.")
                        return 0
    except (QueueError, CheckError, UploadError) as error:
        output(f"Stopped: {error}")
        return 1
    except (KeyboardInterrupt, EOFError):
        output("\nStopped. Saved progress is retained; later videos will not start.")
        return 130
    except Exception:
        # Auth/network failures may contain private account details or session tokens.
        output(
            "Stopped: Sign-in, the network, or local state could not be confirmed. "
            "Progress is retained. Check YouTube Studio and run --status before resuming. "
            "No later videos will start."
        )
        return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    raise SystemExit(main())
