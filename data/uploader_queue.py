"""Offline upload-plan parsing, durable progress, and project-local locking.

Source paths are strings throughout this module. No source-file API is used.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat
from typing import Iterator
from urllib.parse import urlsplit
import uuid


MANIFEST_NAME = "upload-order.txt"
STATE_DIRECTORY = ".uploader-state"
_STATUSES = {"initiating", "uploading", "completed", "blocked"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".wmv", ".mts", ".m2ts"})
RETRYABLE_INITIALIZATION_BLOCK_REASON = (
    "Upload initialization was not confirmed. Resolve this row manually "
    "before retrying so a duplicate cannot be created."
)
_RETRYABLE_INITIALIZATION_BLOCK_REASONS = frozenset({
    RETRYABLE_INITIALIZATION_BLOCK_REASON,
    "YouTube refused upload initialization (HTTP 403). Check the API/channel "
    "upload limit and permissions. Resolve this row before retrying.",
})


class QueueError(Exception):
    """An actionable, sanitized local queue/state error."""


@dataclass(frozen=True)
class QueueEntry:
    number: int
    path: str
    title: str


@dataclass(frozen=True)
class UploadPlan:
    entries: list[QueueEntry]
    source_root: PureWindowsPath
    channel_id: str


def _safe_windows_parts(parts: list[str]) -> None:
    if any(
        not part or part in {".", ".."} or part.endswith((" ", "."))
        or (hasattr(ntpath, "isreserved") and ntpath.isreserved(part))
        or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
        for part in parts
    ):
        raise QueueError("A queue path contains an unsafe Windows path component.")


def _absolute_windows_path(value: str, *, directory: bool = False) -> PureWindowsPath:
    """Accept drive or UNC paths without filesystem normalization or inspection."""
    if not isinstance(value, str) or not value or value.startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./")):
        raise QueueError("An absolute Windows path is required.")
    if re.match(r"^[A-Za-z]:[\\/]", value):
        remainder = value[3:]
    elif re.match(r"^[\\/]{2}[^\\/]+[\\/][^\\/]+(?:[\\/]|$)", value):
        remainder = value[2:]
    else:
        raise QueueError("An absolute Windows path is required.")
    if directory:
        remainder = remainder.rstrip("\\/")
    _safe_windows_parts(re.split(r"[\\/]", remainder) if remainder else [])
    path = PureWindowsPath(value)
    if not path.is_absolute():
        raise QueueError("An absolute Windows path is required.")
    return path


def _source_path_key(value: str, source_root: PureWindowsPath | str | None = None) -> str:
    """Validate optional lexical containment without inspecting a source file."""
    path = _absolute_windows_path(value)
    parts = tuple(part.casefold() for part in path.parts)
    if source_root is not None:
        root = _absolute_windows_path(str(source_root), directory=True)
        root_parts = tuple(part.casefold() for part in root.parts)
        if parts[:len(root_parts)] != root_parts or len(parts) <= len(root_parts):
            raise QueueError("A queue path is outside the declared source root.")
    if path.suffix.casefold() not in _VIDEO_EXTENSIONS:
        raise QueueError("A queue path does not have a supported video extension.")
    return "\\".join(parts)


def _validate_entries(entries: list[QueueEntry] | tuple[QueueEntry, ...], source_root: PureWindowsPath | str | None = None) -> None:
    if not entries:
        raise QueueError("The upload queue is empty.")
    seen: set[str] = set()
    for expected, entry in enumerate(entries, 1):
        if not isinstance(entry, QueueEntry) or type(entry.number) is not int or entry.number != expected:
            raise QueueError("Queue numbers must be consecutive, beginning at 1.")
        key = _source_path_key(entry.path, source_root)
        if key in seen:
            raise QueueError("The upload queue contains a duplicate source path.")
        seen.add(key)
        if not isinstance(entry.title, str) or not entry.title.strip() or len(entry.title) > 100:
            raise QueueError("A queue title is invalid or exceeds YouTube's title limit.")
        if any(ord(char) < 32 or char in "<>" for char in entry.title):
            raise QueueError("A queue title contains invalid characters.")


def _infer_source_root(entries: list[QueueEntry] | tuple[QueueEntry, ...]) -> PureWindowsPath:
    """Compatibility for callers with already-absolute entries; no source I/O."""
    _validate_entries(entries)
    try:
        shared = ntpath.commonpath([str(PureWindowsPath(entry.path).parent) for entry in entries])
    except ValueError:
        raise QueueError("Declare a source root for this upload queue.") from None
    return _absolute_windows_path(shared, directory=True)


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_project(project_dir: Path, source_root: PureWindowsPath | str | None = None) -> Path:
    # abspath is lexical. Check protected-root containment BEFORE filesystem calls.
    project = Path(os.path.abspath(os.fspath(project_dir)))
    windows_parts = tuple(part.casefold() for part in PureWindowsPath(str(project)).parts)
    if source_root is not None:
        source = _absolute_windows_path(str(source_root), directory=True)
        source_parts = tuple(part.casefold() for part in source.parts)
        if (windows_parts[:len(source_parts)] == source_parts
                or source_parts[:len(windows_parts)] == windows_parts):
            raise QueueError("The project and source folders must be disjoint directories.")
    try:
        for component in reversed((project, *project.parents)):
            info = os.lstat(component)
            if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise QueueError("The project directory must not use links, junctions, or reparse points.")
    except OSError:
        raise QueueError("The project directory could not be checked safely.") from None
    return project


def _safe_regular_file(path: Path, *, required: bool = False) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise QueueError("A required project file is missing.") from None
        return
    except OSError:
        raise QueueError("A project state file could not be checked safely.") from None
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise QueueError("Project files must be ordinary files, without links or reparse points.")


def _state_directory(project_dir: Path, source_root: PureWindowsPath | str | None = None) -> tuple[Path, Path]:
    project = _safe_project(project_dir, source_root)
    directory = project / STATE_DIRECTORY
    try:
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(directory)
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise QueueError("The upload state directory must not be a link, junction, or reparse point.")
    except OSError:
        raise QueueError("The project-local upload state directory is unavailable.") from None
    return project, directory


def _manifest_lines(project_dir: Path) -> tuple[Path, list[str]]:
    project = _safe_project(project_dir)
    manifest = project / MANIFEST_NAME
    _safe_regular_file(manifest, required=True)
    try:
        lines = manifest.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise QueueError("The upload queue could not be read as UTF-8.") from None
    return project, lines


def _manifest_settings(lines: list[str]) -> dict[str, str]:
    aliases = {"source root": "source_root", "original source root": "source_root",
               "channel id": "channel_id"}
    settings: dict[str, str] = {}
    for line in lines:
        if not line.startswith("#"):
            continue
        key, separator, value = line[1:].partition(":")
        name = aliases.get(key.strip().casefold())
        if name is None:
            continue
        value = value.strip()
        if not separator or not value or any(ord(char) < 32 for char in value):
            raise QueueError("A required upload-plan header is malformed.")
        if name == "source_root":
            _absolute_windows_path(value, directory=True)
        elif name == "channel_id" and not _CHANNEL_ID.fullmatch(value):
            raise QueueError("The upload-plan Channel ID header is invalid.")
        previous = settings.get(name)
        if previous is not None:
            equal = (PureWindowsPath(previous) == PureWindowsPath(value)) if name == "source_root" else previous == value
            if not equal:
                raise QueueError("The upload plan contains conflicting configuration headers.")
        else:
            settings[name] = value
    return settings


def _manifest_entries(lines: list[str], source_root: PureWindowsPath | None) -> list[QueueEntry]:
    entries: list[QueueEntry] = []
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split(" | ")
        if len(fields) != 3 or not re.fullmatch(r"[0-9]+", fields[0]):
            raise QueueError("A queue record does not have the required three fields.")
        value = fields[1]
        path = PureWindowsPath(value)
        if not path.is_absolute():
            if source_root is None or path.drive or path.root:
                raise QueueError("A relative video path requires a declared source root.")
            _safe_windows_parts(re.split(r"[\\/]", value))
            value = str(source_root / path)
        # Absolute path spelling is deliberately never normalized or reformatted.
        entries.append(QueueEntry(int(fields[0]), value, fields[2]))
    _validate_entries(entries, source_root)
    return entries


def load_upload_plan(project_dir: Path) -> UploadPlan:
    project, lines = _manifest_lines(project_dir)
    settings = _manifest_settings(lines)
    if "source_root" not in settings or "channel_id" not in settings:
        raise QueueError("The upload plan requires Source root and Channel ID headers.")
    source_root = _absolute_windows_path(settings["source_root"], directory=True)
    _safe_project(project, source_root)
    return UploadPlan(_manifest_entries(lines, source_root), source_root,
                      settings["channel_id"])


def load_queue(project_dir: Path) -> list[QueueEntry]:
    """Compatibility reader; the uploader uses strict load_upload_plan instead."""
    project, lines = _manifest_lines(project_dir)
    settings = _manifest_settings(lines)
    source_root = (_absolute_windows_path(settings["source_root"], directory=True)
                   if "source_root" in settings else None)
    _safe_project(project, source_root)
    return _manifest_entries(lines, source_root)


class ProjectLock:
    """One cooperating uploader process per project; the descriptor stays held."""

    def __init__(self, project_dir: Path, *, source_root: PureWindowsPath | str | None = None):
        self.project_dir = Path(project_dir)
        self.source_root = source_root
        self._fd: int | None = None

    def __enter__(self) -> "ProjectLock":
        if self._fd is not None:
            raise QueueError("This uploader lock is already held.")
        project, directory = _state_directory(self.project_dir, self.source_root)
        path = directory / "upload.lock"
        _safe_regular_file(path)
        fd: int | None = None
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise QueueError("The uploader lock file is unsafe.")
            if info.st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _safe_project(project, self.source_root)
            _safe_regular_file(path, required=True)
            self._fd = fd
            return self
        except (OSError, QueueError):
            if fd is not None:
                os.close(fd)
            raise QueueError("Another uploader is running, or the project lock is unavailable.") from None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class StateStore:
    """A transactionally pinned queue with a completed prefix and one current row."""

    def __init__(self, project_dir: Path, entries: list[QueueEntry], *, source_root: PureWindowsPath | str | None = None):
        _validate_entries(entries, source_root)
        self.entries = tuple(entries)
        self.source_root = (_absolute_windows_path(str(source_root), directory=True)
                            if source_root is not None else _infer_source_root(self.entries))
        self.project_dir, self.directory = _state_directory(Path(project_dir), self.source_root)
        self.path = self.directory / "upload-state.sqlite3"
        self._connection: sqlite3.Connection | None = None
        fingerprint_data = [[entry.number, entry.path, entry.title] for entry in self.entries]
        self.fingerprint = hashlib.sha256(
            json.dumps(fingerprint_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        try:
            self._check_storage()
            self._connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA journal_mode=DELETE")
            with self._transaction(validate=False) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("""CREATE TABLE IF NOT EXISTS uploads (
                    number INTEGER PRIMARY KEY, path TEXT NOT NULL, title TEXT NOT NULL,
                    status TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                    session_url TEXT, offset INTEGER NOT NULL DEFAULT 0, video_id TEXT, reason TEXT
                )""")
                metadata = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
                if metadata.get("schema_version", "1") != "1":
                    raise QueueError("The upload state uses an unsupported format.")
                old = metadata.get("queue_fingerprint")
                count = connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
                if old != self.fingerprint:
                    if count:
                        raise QueueError("The upload queue changed after progress was recorded. Manual review is required.")
                    connection.execute("INSERT OR REPLACE INTO metadata VALUES ('queue_fingerprint', ?)", (self.fingerprint,))
                connection.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')")
                self._validate_rows(connection)
            if self._connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise QueueError("The upload state failed its integrity check.")
        except (OSError, sqlite3.Error):
            self.close()
            raise QueueError("The durable upload state could not be opened safely.") from None
        except QueueError:
            self.close()
            raise

    def _check_storage(self) -> None:
        _safe_project(self.project_dir, self.source_root)
        try:
            info = os.lstat(self.directory)
        except OSError:
            raise QueueError("The upload state directory is unavailable.") from None
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise QueueError("The upload state directory is unsafe.")
        for suffix in ("", "-journal", "-wal", "-shm"):
            _safe_regular_file(Path(str(self.path) + suffix))
        _safe_regular_file(self.directory / "upload.lock")

    @property
    def state_dir(self) -> Path:
        return self.directory

    def checked_state_file(self, name: str) -> Path:
        """Preflight a fixed project-local report file; callers still own its write."""
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name in {".", ".."}:
            raise QueueError("The project state filename is invalid.")
        self._check_storage()
        path = self.directory / name
        _safe_regular_file(path)
        return path

    @contextmanager
    def _transaction(self, *, validate: bool = True) -> Iterator[sqlite3.Connection]:
        self._check_storage()
        connection = self._connection
        if connection is None:
            raise QueueError("The upload state is closed.")
        begun = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            begun = True
            if validate:
                self._validate_rows(connection)
            yield connection
            self._check_storage()
            connection.execute("COMMIT")
        except (OSError, sqlite3.Error):
            if begun:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise QueueError("The upload state operation could not be saved safely.") from None
        except BaseException:
            if begun:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise

    def _validate_rows(self, connection: sqlite3.Connection) -> None:
        metadata = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
        if metadata.get("schema_version") != "1" or metadata.get("queue_fingerprint") != self.fingerprint:
            raise QueueError("The upload state no longer matches this pinned queue.")
        rows = connection.execute("SELECT * FROM uploads ORDER BY number").fetchall()
        channel = metadata.get("channel_id")
        if channel is not None and not _IDENTIFIER.fullmatch(channel):
            raise QueueError("The upload state contains an invalid channel identifier.")
        if rows and channel is None:
            raise QueueError("Upload progress is missing its confirmed destination channel.")
        unfinished = False
        remote_ids: set[str] = set()
        for expected, row in enumerate(rows, 1):
            if row["number"] != expected or row["number"] > len(self.entries) or unfinished:
                raise QueueError("Upload state is not a valid consecutive queue prefix.")
            entry = self.entries[row["number"] - 1]
            if row["path"] != entry.path or row["title"] != entry.title or row["status"] not in _STATUSES:
                raise QueueError("Upload state does not match the pinned queue.")
            if type(row["size"]) is not int or row["size"] <= 0 or type(row["mtime_ns"]) is not int or row["mtime_ns"] < 0:
                raise QueueError("Upload state has invalid source-file metadata.")
            if type(row["offset"]) is not int or not 0 <= row["offset"] <= row["size"]:
                raise QueueError("Upload state has an invalid confirmed byte offset.")
            session, video_id, status = row["session_url"], row["video_id"], row["status"]
            if status == "blocked" and (not isinstance(row["reason"], str) or not row["reason"].strip()):
                raise QueueError("A blocked upload is missing its reason for review.")
            if session is not None:
                self._validate_session(session)
            if video_id is not None and (not isinstance(video_id, str) or not _IDENTIFIER.fullmatch(video_id)):
                raise QueueError("Upload state has an invalid remote video identifier.")
            if video_id is not None:
                if video_id in remote_ids:
                    raise QueueError("Upload state assigns one remote video to multiple queue entries.")
                remote_ids.add(video_id)
            if status == "initiating" and (session is not None or video_id is not None or row["offset"] != 0):
                raise QueueError("Upload initiation state is inconsistent.")
            if status == "uploading" and (session is None or video_id is not None):
                raise QueueError("The current upload is missing a safe resumable session.")
            if status == "completed" and (video_id is None or row["offset"] != row["size"]):
                raise QueueError("A completed upload is missing confirmed completion information.")
            if status == "blocked" and video_id is not None and (session is not None or row["offset"] != row["size"]):
                raise QueueError("A blocked remote video has inconsistent completion information.")
            unfinished = status != "completed"

    def _entry(self, entry: QueueEntry) -> None:
        if not isinstance(entry, QueueEntry) or type(entry.number) is not int or not 1 <= entry.number <= len(self.entries) or self.entries[entry.number - 1] != entry:
            raise QueueError("The requested video is not in the pinned queue.")

    def _current(self, connection: sqlite3.Connection, entry: QueueEntry) -> sqlite3.Row | None:
        self._entry(entry)
        completed = connection.execute("SELECT COUNT(*) FROM uploads WHERE status='completed'").fetchone()[0]
        if entry.number != completed + 1:
            raise QueueError("Only the current unfinished video may change upload state.")
        return connection.execute("SELECT * FROM uploads WHERE number=?", (entry.number,)).fetchone()

    @staticmethod
    def _validate_session(url: str) -> None:
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None and not parsed.fragment
        except (ValueError, TypeError):
            valid = False
        if not valid or not isinstance(url, str) or any(ord(char) < 32 for char in url):
            raise QueueError("The resumable session address is invalid.")

    def bind_channel(self, channel_id: str) -> None:
        if not isinstance(channel_id, str) or not _IDENTIFIER.fullmatch(channel_id):
            raise QueueError("The destination channel identifier is invalid.")
        with self._transaction() as connection:
            found = connection.execute("SELECT value FROM metadata WHERE key='channel_id'").fetchone()
            if found is not None and found[0] != channel_id:
                raise QueueError("Upload progress belongs to a different destination channel.")
            connection.execute("INSERT OR IGNORE INTO metadata VALUES ('channel_id', ?)", (channel_id,))

    def check_channel(self, channel_id: str) -> None:
        """Compare declared destination with saved progress without binding a new app."""
        if not isinstance(channel_id, str) or not _IDENTIFIER.fullmatch(channel_id):
            raise QueueError("The destination channel identifier is invalid.")
        with self._transaction() as connection:
            found = connection.execute("SELECT value FROM metadata WHERE key='channel_id'").fetchone()
            if found is not None and found[0] != channel_id:
                raise QueueError("Upload progress belongs to a different destination channel.")

    def pending(self, batch_size: int) -> list[QueueEntry]:
        if type(batch_size) is not int or batch_size <= 0:
            raise QueueError("The batch size must be a positive integer.")
        with self._transaction() as connection:
            completed = connection.execute("SELECT COUNT(*) FROM uploads WHERE status='completed'").fetchone()[0]
            return list(self.entries[completed:completed + batch_size])

    def get(self, entry: QueueEntry) -> dict | None:
        self._entry(entry)
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM uploads WHERE number=?", (entry.number,)).fetchone()
            return dict(row) if row is not None else None

    def start(self, entry: QueueEntry, size: int, mtime_ns: int) -> None:
        if type(size) is not int or size <= 0 or type(mtime_ns) is not int or mtime_ns < 0:
            raise QueueError("The source-file size or modification time is invalid.")
        with self._transaction() as connection:
            if self._current(connection, entry) is not None:
                raise QueueError("This video already has upload state. Resume or resolve it before retrying.")
            if connection.execute("SELECT value FROM metadata WHERE key='channel_id'").fetchone() is None:
                raise QueueError("Confirm the destination channel before initiating an upload.")
            connection.execute("INSERT INTO uploads (number,path,title,status,size,mtime_ns,offset) VALUES (?,?,?,'initiating',?,?,0)",
                               (entry.number, entry.path, entry.title, size, mtime_ns))

    def save_session(self, entry: QueueEntry, url: str) -> None:
        self._validate_session(url)
        with self._transaction() as connection:
            row = self._current(connection, entry)
            if row is None or row["status"] != "initiating":
                raise QueueError("The video is not awaiting its initial resumable session.")
            connection.execute("UPDATE uploads SET status='uploading',session_url=?,reason=NULL WHERE number=?", (url, entry.number))

    def progress(self, entry: QueueEntry, offset: int) -> None:
        with self._transaction() as connection:
            row = self._current(connection, entry)
            if row is None or row["status"] not in {"uploading", "blocked"} or row["session_url"] is None or row["video_id"] is not None:
                raise QueueError("The current video has no resumable upload to update.")
            if type(offset) is not int or not 0 <= offset <= row["size"]:
                raise QueueError("The confirmed upload offset is outside the video size.")
            connection.execute("UPDATE uploads SET offset=?,status='uploading',reason=NULL WHERE number=?", (offset, entry.number))

    def complete(self, entry: QueueEntry, video_id: str) -> None:
        self._finish(entry, video_id, None)

    def record_remote_issue(self, entry: QueueEntry, video_id: str, reason: str) -> None:
        self._finish(entry, video_id, reason)

    def _finish(self, entry: QueueEntry, video_id: str, reason: str | None) -> None:
        if not isinstance(video_id, str) or not _IDENTIFIER.fullmatch(video_id):
            raise QueueError("The confirmed video identifier is invalid.")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise QueueError("A remote video issue must have a nonempty reason.")
        with self._transaction() as connection:
            row = self._current(connection, entry)
            if row is None or row["status"] not in {"uploading", "blocked"} or row["session_url"] is None or row["video_id"] is not None:
                raise QueueError("The current video has no active upload to confirm.")
            if connection.execute("SELECT 1 FROM uploads WHERE video_id=?", (video_id,)).fetchone():
                raise QueueError("This remote video is already assigned to another queue entry.")
            status = "completed" if reason is None else "blocked"
            connection.execute("UPDATE uploads SET status=?,offset=size,video_id=?,session_url=NULL,reason=? WHERE number=?",
                               (status, video_id, reason, entry.number))

    def block(self, entry: QueueEntry, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise QueueError("A blocked upload must have a nonempty reason.")
        with self._transaction() as connection:
            if self._current(connection, entry) is None:
                raise QueueError("There is no current upload state to block.")
            connection.execute("UPDATE uploads SET status='blocked',reason=? WHERE number=?", (reason, entry.number))

    @staticmethod
    def _retryable_block(row: sqlite3.Row | None) -> bool:
        return bool(
            row is not None
            and row["status"] == "blocked"
            and row["session_url"] is None
            and row["video_id"] is None
            and row["offset"] == 0
            and row["reason"] in _RETRYABLE_INITIALIZATION_BLOCK_REASONS
        )

    def can_retry_blocked(self, entry: QueueEntry) -> bool:
        """Whether this current row can be offered explicit not-uploaded confirmation.

        Eligibility is not evidence of the remote outcome and never clears state.
        """
        self._entry(entry)
        with self._transaction() as connection:
            completed = connection.execute("SELECT COUNT(*) FROM uploads WHERE status='completed'").fetchone()[0]
            if entry.number != completed + 1:
                return False
            row = connection.execute("SELECT * FROM uploads WHERE number=?", (entry.number,)).fetchone()
            return self._retryable_block(row)

    @staticmethod
    def _snapshot(connection: sqlite3.Connection) -> tuple[tuple, tuple]:
        return (
            tuple(tuple(row) for row in connection.execute("SELECT * FROM uploads ORDER BY number")),
            tuple(tuple(row) for row in connection.execute("SELECT key,value FROM metadata ORDER BY key")),
        )

    def _verified_backup_snapshot(self, path: Path) -> tuple[tuple, tuple]:
        """Read only a checked project backup; never a manifest source path."""
        self._check_storage()
        if path.parent != self.directory or not re.fullmatch(r"retry-backup-[0-9a-f]{32}\.sqlite3", path.name):
            raise QueueError("The upload backup path is invalid.")
        _safe_regular_file(path, required=True)
        backup = None
        try:
            backup = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0, isolation_level=None)
            backup.row_factory = sqlite3.Row
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise QueueError("The upload state backup failed verification.")
            self._validate_rows(backup)
            return self._snapshot(backup)
        except (OSError, sqlite3.Error):
            raise QueueError("The upload state backup could not be verified safely.") from None
        finally:
            if backup is not None:
                backup.close()

    def backup_state(self) -> Path:
        """Create and verify a new durable SQLite snapshot without changing progress."""
        self._check_storage()
        connection = self._connection
        if connection is None or connection.in_transaction:
            raise QueueError("The upload state is unavailable for a safe backup.")
        self._validate_rows(connection)
        path = self.checked_state_file(f"retry-backup-{uuid.uuid4().hex}.sqlite3")
        backup = None
        descriptor = None
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            original_info = os.fstat(descriptor)
            # Hold the created file open through verification and flushing. On
            # Windows this also prevents replacement of its directory entry.
            _safe_regular_file(path, required=True)
            self._check_storage()
            backup = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5.0, isolation_level=None)
            backup.execute("PRAGMA synchronous=FULL")
            connection.backup(backup)
            backup.close()
            backup = None
            self._verified_backup_snapshot(path)
            self._check_storage()
            _safe_regular_file(path, required=True)
            final_info = os.fstat(descriptor)
            path_info = os.lstat(path)
            if (
                (final_info.st_dev, final_info.st_ino) != (original_info.st_dev, original_info.st_ino)
                or (path_info.st_dev, path_info.st_ino) != (final_info.st_dev, final_info.st_ino)
                or not stat.S_ISREG(final_info.st_mode)
                or final_info.st_nlink != 1
            ):
                raise QueueError("The upload state backup changed unexpectedly.")
            # Windows FlushFileBuffers requires the writable creation handle.
            os.fsync(descriptor)
            return path
        except (OSError, sqlite3.Error):
            raise QueueError("The upload state backup could not be created and verified. Progress was not reset.") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if backup is not None:
                backup.close()

    def reset_blocked_for_retry(self, entry: QueueEntry, *, confirmed_not_uploaded: bool) -> Path:
        """Back up then unstart only a reviewed, explicitly confirmed current block.

        No session, remote ID, transferred bytes, completed row, or uncertain
        initiating row can be reset. Nothing here starts a remote operation.
        """
        if confirmed_not_uploaded is not True:
            raise QueueError("Explicit confirmation that this video was not uploaded is required.")
        self._entry(entry)
        self._check_storage()
        connection = self._connection
        if connection is None:
            raise QueueError("The upload state is closed.")
        # Preflight is read-only; the verified backup precedes the write transaction.
        try:
            self._validate_rows(connection)
            row = self._current(connection, entry)
            if not self._retryable_block(row):
                raise QueueError("This video is not eligible for a confirmed initialization retry.")
        except sqlite3.Error:
            raise QueueError("The blocked upload state could not be checked safely.") from None
        backup = self.backup_state()
        before_rows, before_metadata = self._verified_backup_snapshot(backup)
        with self._transaction() as connection:
            row = self._current(connection, entry)
            if not self._retryable_block(row):
                raise QueueError("The blocked video changed before the retry was confirmed.")
            if self._snapshot(connection) != (before_rows, before_metadata):
                raise QueueError("Upload progress changed after the backup. The blocked video was not reset.")
            deleted = connection.execute("DELETE FROM uploads WHERE number=?", (entry.number,))
            if deleted.rowcount != 1:
                raise QueueError("The blocked video could not be reset safely.")
            expected_rows = tuple(row for row in before_rows if row[0] != entry.number)
            if self._snapshot(connection) != (expected_rows, before_metadata):
                raise QueueError("Other upload progress changed during the retry. The operation was cancelled.")
            self._validate_rows(connection)
        return backup

    def rows(self) -> list[dict]:
        with self._transaction() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM uploads ORDER BY number")]

    def summary(self) -> dict:
        records = self.rows()
        completed = sum(row["status"] == "completed" for row in records)
        return {"total": len(self.entries), "completed": completed, "remaining": len(self.entries) - completed,
                "blocked": sum(row["status"] == "blocked" for row in records),
                "current": completed + 1 if completed < len(self.entries) else None}

    def close(self) -> None:
        if self._connection is not None:
            connection, self._connection = self._connection, None
            connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
