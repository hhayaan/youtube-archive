"""Portable connection helpers and launcher routing with no real credentials or services."""

from copy import deepcopy
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from data import uploader_connection as connection


PROJECT = Path(__file__).resolve().parents[1]
CONFIG = {
    "installed": {
        "client_id": "synthetic.apps.googleusercontent.com",
        "client_secret": "synthetic-placeholder-only",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


class ClientConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="connection config ", dir=PROJECT / "tests")
        self.project = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def write(self, config, *, name="client_secret_synthetic.json", encoding="utf-8"):
        path = self.project / name
        path.write_text(json.dumps(config), encoding=encoding)
        return path

    def test_desktop_config_accepts_bom_and_root_relative_location(self):
        self.write(CONFIG, encoding="utf-8-sig")
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No network")):
            self.assertEqual(connection.load_client_config(self.project), CONFIG)

    def test_only_top_level_matching_client_is_read(self):
        self.write(CONFIG)
        nested = self.project / "subfolder"
        nested.mkdir()
        (nested / "client_secret_nested.json").write_text("not read", encoding="utf-8")
        (self.project / "other.json").write_text("not read", encoding="utf-8")
        self.assertEqual(connection.load_client_config(self.project), CONFIG)

    def test_missing_or_multiple_root_clients_stop_with_main_launcher_guidance(self):
        with self.assertRaisesRegex(connection.CheckError, "start.cmd"):
            connection.load_client_config(self.project)
        self.write(CONFIG)
        self.write(CONFIG, name="client_secret_second.json")
        with self.assertRaisesRegex(connection.CheckError, "exactly one"):
            connection.load_client_config(self.project)

    def test_web_mixed_incomplete_and_nonobject_clients_are_rejected(self):
        variants = [
            {"web": CONFIG["installed"]},
            {**CONFIG, "web": CONFIG["installed"]},
            [],
            {"installed": {}},
            {"installed": {**CONFIG["installed"], "client_secret": " "}},
            {"installed": {**CONFIG["installed"], "auth_uri": []}},
        ]
        for config in variants:
            with self.subTest(config_type=type(config).__name__):
                self.write(config)
                with self.assertRaises(connection.CheckError):
                    connection.load_client_config(self.project)

    def test_non_google_endpoints_are_rejected_without_disclosing_values(self):
        for key in ("auth_uri", "token_uri"):
            with self.subTest(field=key):
                config = deepcopy(CONFIG)
                config["installed"][key] = "https://example.invalid/private-token"
                self.write(config)
                with self.assertRaises(connection.CheckError) as caught:
                    connection.load_client_config(self.project)
                self.assertNotIn("private-token", str(caught.exception))

    def test_invalid_json_and_filesystem_failures_have_sanitized_errors(self):
        path = self.project / "client_secret_synthetic.json"
        path.write_text("synthetic-private@example.invalid secret-token", encoding="utf-8")
        with self.assertRaises(connection.CheckError) as caught:
            connection.load_client_config(self.project)
        self.assertNotIn("secret-token", str(caught.exception))
        with patch.object(Path, "glob", side_effect=OSError("synthetic-private@example.invalid")):
            with self.assertRaises(connection.CheckError) as caught:
                connection.load_client_config(self.project)
        self.assertNotIn("example.invalid", str(caught.exception))

    def test_symlink_or_outside_file_is_rejected_before_reading(self):
        candidate = Mock()
        candidate.is_file.return_value = True
        for symbolic, resolved in (
            (True, self.project / "client_secret_synthetic.json"),
            (False, self.project.parent / "outside-client.json"),
        ):
            with self.subTest(symbolic=symbolic):
                candidate.is_symlink.return_value = symbolic
                candidate.resolve.return_value = resolved
                with patch.object(Path, "glob", return_value=iter([candidate])):
                    with self.assertRaises(connection.CheckError):
                        connection.load_client_config(self.project)
                candidate.read_text.assert_not_called()


class ChannelLookupTests(unittest.TestCase):
    def response(self, data, status=200):
        response = Mock(status_code=status)
        response.json.return_value = data
        return response

    def test_channel_lookup_uses_one_fixed_get_with_minimal_fields_and_no_files(self):
        session = Mock(spec=["get"])
        response = self.response({"items": [{"id": "UC_synthetic", "snippet": {"title": "Archive\nName"}}]})
        session.get.return_value = response
        with (
            patch("builtins.open", side_effect=AssertionError("No files")),
            patch("os.scandir", side_effect=AssertionError("No folders")),
        ):
            self.assertEqual(connection.read_channel(session), ("Archive Name", "UC_synthetic"))
        session.get.assert_called_once_with(
            connection.CHANNELS_URL,
            params={"part": "id,snippet", "mine": "true", "fields": "items(id,snippet/title)", "maxResults": 50},
            timeout=30, allow_redirects=False,
        )
        response.close.assert_called_once_with()

    def test_api_failure_never_parses_or_prints_private_body(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                session = Mock(spec=["get"])
                response = self.response({"private": "synthetic-secret-token"}, status=status)
                response.text = "synthetic-private@example.invalid secret-token"
                session.get.return_value = response
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(connection.CheckError) as caught:
                        connection.read_channel(session)
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
                response.json.assert_not_called()
                response.close.assert_called_once_with()

    def test_missing_ambiguous_or_malformed_channel_is_rejected(self):
        for data in (
            None, {"items": []}, {"items": [{}, {}]}, {"items": [None]},
            {"items": [{"id": "UC_synthetic", "snippet": {}}]},
            {"items": [{"id": "\n", "snippet": {"title": "Archive"}}]},
        ):
            with self.subTest(data=data):
                session = Mock(spec=["get"])
                response = self.response(data)
                session.get.return_value = response
                with self.assertRaises(connection.CheckError):
                    connection.read_channel(session)
                response.close.assert_called_once_with()

    def test_network_and_json_errors_are_sanitized(self):
        session = Mock(spec=["get"])
        session.get.side_effect = RuntimeError("synthetic-private@example.invalid secret-token")
        with self.assertRaises(connection.CheckError) as caught:
            connection.read_channel(session)
        self.assertNotIn("secret-token", str(caught.exception))
        session.get.side_effect = None
        response = self.response(None)
        response.json.side_effect = ValueError("synthetic-secret-token")
        session.get.return_value = response
        with self.assertRaises(connection.CheckError) as caught:
            connection.read_channel(session)
        self.assertNotIn("secret-token", str(caught.exception))
        response.close.assert_called_once_with()


@unittest.skipUnless(os.name == "nt", "Launcher routing uses Windows cmd.exe")
class LauncherBootstrapTests(unittest.TestCase):
    """Run the launcher control flow with every Python/pip command replaced by a stub."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="portable launcher ", dir=PROJECT / "tests")
        self.project = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        original = (PROJECT / "start.cmd").read_text(encoding="utf-8")
        routed = []
        for line in original.splitlines():
            if line.startswith("where py "):
                line = 'call "%~dp0stub-bootstrap.cmd" find-py >nul 2>nul'
            elif line.startswith("where python "):
                line = 'call "%~dp0stub-bootstrap.cmd" find-python >nul 2>nul'
            elif line.startswith("py -3 -m venv "):
                line = 'call "%~dp0stub-bootstrap.cmd" create-py'
            elif line.startswith("python -m venv "):
                line = 'call "%~dp0stub-bootstrap.cmd" create-python'
            elif line.startswith("py -3 -B -c "):
                operation = "plan-py" if "load_upload_plan" in line else "check-py"
                line = f'call "%~dp0stub-bootstrap.cmd" {operation} >nul 2>nul'
            elif line.startswith("python -B -c "):
                operation = "plan-python" if "load_upload_plan" in line else "check-python"
                line = f'call "%~dp0stub-bootstrap.cmd" {operation} >nul 2>nul'
            elif line.startswith('"%~dp0.venv\\Scripts\\python.exe"'):
                if "load_upload_plan" in line:
                    line = 'call "%~dp0stub-bootstrap.cmd" plan-venv >nul 2>nul'
                elif ' -c "import ' in line:
                    line = 'call "%~dp0stub-bootstrap.cmd" imports >nul 2>nul'
                elif " -m pip install " in line:
                    line = 'call "%~dp0stub-bootstrap.cmd" install'
                elif " -m data.upload_videos " in line:
                    line = 'call "%~dp0stub-bootstrap.cmd" run %*'
                else:
                    self.fail("A Python launcher command was not replaced by the offline stub")
            routed.append(line)
        (self.project / "start.cmd").write_text("\n".join(routed), encoding="utf-8")
        (self.project / "stub-bootstrap.cmd").write_text(
            '@echo off\n'
            'echo %*>>"%~dp0stub-events.txt"\n'
            'if "%~1"=="find-py" exit /b %TEST_PY_FIND_EXIT%\n'
            'if "%~1"=="find-python" exit /b %TEST_PYTHON_FIND_EXIT%\n'
            'if "%~1"=="check-py" exit /b %TEST_PY_CHECK_EXIT%\n'
            'if "%~1"=="check-python" exit /b 0\n'
            'if "%~1"=="plan-py" exit /b %TEST_PLAN_EXIT%\n'
            'if "%~1"=="plan-python" exit /b %TEST_PLAN_EXIT%\n'
            'if "%~1"=="plan-venv" exit /b %TEST_PLAN_EXIT%\n'
            'if "%~1"=="create-py" if not "%TEST_PY_CREATE_EXIT%"=="0" exit /b %TEST_PY_CREATE_EXIT%\n'
            'if "%~1"=="create-py" goto created\n'
            'if "%~1"=="create-python" goto created\n'
            'if "%~1"=="imports" goto imports\n'
            'if "%~1"=="install" goto install\n'
            'if "%~1"=="run" exit /b %TEST_MAIN_EXIT%\n'
            'exit /b 99\n'
            ':created\n'
            'if not exist "%~dp0.venv\\Scripts" mkdir "%~dp0.venv\\Scripts"\n'
            'type nul>"%~dp0.venv\\Scripts\\python.exe"\n'
            'exit /b 0\n'
            ':imports\n'
            'if exist "%~dp0dependencies-ready" exit /b 0\n'
            'exit /b 1\n'
            ':install\n'
            'if not "%TEST_INSTALL_EXIT%"=="0" exit /b %TEST_INSTALL_EXIT%\n'
            'type nul>"%~dp0dependencies-ready"\n'
            'exit /b 0\n',
            encoding="utf-8",
        )

    def run_launcher(self, *, ready=False, **overrides):
        if ready:
            scripts = self.project / ".venv" / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "python.exe").write_bytes(b"")
            (self.project / "dependencies-ready").write_bytes(b"")
        env = {
            **os.environ,
            "TEST_PY_FIND_EXIT": "0", "TEST_PYTHON_FIND_EXIT": "0",
            "TEST_PY_CHECK_EXIT": "0", "TEST_PLAN_EXIT": "0",
            "TEST_PY_CREATE_EXIT": "0", "TEST_INSTALL_EXIT": "0", "TEST_MAIN_EXIT": "0",
            **overrides,
        }
        with ExitStack() as stack:
            stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("No network")))
            result = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "call", "start.cmd", "--status"],
                cwd=self.project, env=env, input="\n", capture_output=True, text=True, timeout=10,
            )
        self.assertTrue((self.project / "stub-events.txt").exists(), result.stdout + result.stderr)
        events = (self.project / "stub-events.txt").read_text(encoding="utf-8").splitlines()
        return result, events

    def test_first_launch_creates_environment_installs_then_runs(self):
        result, events = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(events, ["find-py", "check-py", "plan-py", "create-py", "imports", "install", "imports", "run --status"])

    def test_python_fallback_handles_missing_or_failing_py_launcher(self):
        for overrides, prefix in (
            ({"TEST_PY_FIND_EXIT": "1"}, ["find-py", "find-python", "check-python", "plan-python", "create-python"]),
            ({"TEST_PY_CREATE_EXIT": "1"}, ["find-py", "check-py", "plan-py", "create-py", "find-python", "check-python", "plan-python", "create-python"]),
            ({"TEST_PY_CHECK_EXIT": "1"}, ["find-py", "check-py", "find-python", "check-python", "plan-python", "create-python"]),
        ):
            with self.subTest(overrides=overrides):
                result, events = self.run_launcher(**overrides)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(events, prefix + ["imports", "install", "imports", "run --status"])
                # Remove only the synthetic stub markers to exercise the second cold launch.
                (self.project / "stub-events.txt").unlink()
                (self.project / ".venv" / "Scripts" / "python.exe").unlink()
                (self.project / "dependencies-ready").unlink()

    def test_ready_environment_skips_bootstrap_and_closes_on_success(self):
        result, events = self.run_launcher(ready=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(events, ["plan-venv", "imports", "run --status"])
        self.assertEqual(result.stdout.strip(), "")

    def test_missing_python_or_failed_install_does_not_start_uploader(self):
        result, events = self.run_launcher(TEST_PY_FIND_EXIT="1", TEST_PYTHON_FIND_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(events, ["find-py", "find-python"])
        self.assertIn("Python 3.10 or newer was not found", result.stdout)
        (self.project / "stub-events.txt").unlink()
        result, events = self.run_launcher(TEST_INSTALL_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("run --status", events)
        self.assertIn("setup did not complete", result.stdout)

    def test_uploader_error_is_retained_and_pauses(self):
        result, events = self.run_launcher(ready=True, TEST_MAIN_EXIT="7")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(events, ["plan-venv", "imports", "run --status"])
        self.assertNotEqual(result.stdout.strip(), "")

    def test_invalid_plan_stops_before_cold_environment_creation_or_package_install(self):
        result, events = self.run_launcher(TEST_PLAN_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(events, ["find-py", "check-py", "plan-py"])
        self.assertFalse((self.project / ".venv").exists())
        self.assertIn("upload plan could not be validated", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_invalid_plan_stops_existing_environment_before_dependency_check(self):
        result, events = self.run_launcher(ready=True, TEST_PLAN_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(events, ["plan-venv"])
        self.assertIn("upload plan could not be validated", result.stdout)

    def test_invalid_plan_stops_python_fallback_before_environment_creation(self):
        result, events = self.run_launcher(TEST_PY_FIND_EXIT="1", TEST_PLAN_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(events, ["find-py", "find-python", "check-python", "plan-python"])
        self.assertFalse((self.project / ".venv").exists())


class RelocatedLayoutTests(unittest.TestCase):
    def test_packaged_helpers_find_root_credentials_and_plan_from_another_working_directory(self):
        with tempfile.TemporaryDirectory(prefix="relocated layout ", dir=PROJECT / "tests") as name:
            project = Path(name) / "Uploader folder with spaces"
            data = project / "data"
            data.mkdir(parents=True)
            elsewhere = Path(name) / "Different working folder"
            elsewhere.mkdir()
            for module in ("__init__.py", "uploader_connection.py", "uploader_queue.py"):
                (data / module).write_text((PROJECT / "data" / module).read_text(encoding="utf-8"), encoding="utf-8")
            (project / "client_secret_synthetic.json").write_text(json.dumps(CONFIG), encoding="utf-8")
            wrong = deepcopy(CONFIG)
            wrong["installed"]["client_id"] = "wrong-folder.apps.googleusercontent.com"
            (data / "client_secret_wrong_folder.json").write_text(json.dumps(wrong), encoding="utf-8")
            (project / "upload-order.txt").write_text(
                "# Source root: Z:\\Synthetic Videos\n"
                "# Channel ID: UCxxxxxxxxxxxxxxxxxxxxxx\n"
                "001 | Game\\synthetic.mp4 | Game 2020-01-01 00:00\n",
                encoding="utf-8",
            )
            code = (
                "import sys; from pathlib import Path; "
                "sys.path.insert(0, sys.argv[1]); "
                "from data.uploader_connection import PROJECT_DIR, load_client_config; "
                "from data.uploader_queue import load_upload_plan; "
                "assert PROJECT_DIR == Path(sys.argv[1]); "
                "assert load_client_config()['installed']['client_id'] == 'synthetic.apps.googleusercontent.com'; "
                "plan = load_upload_plan(PROJECT_DIR); "
                "assert len(plan.entries) == 1; "
                "assert not (PROJECT_DIR / '.venv').exists(); "
                "assert not (PROJECT_DIR / '.uploader-state').exists(); "
                "print('Relocated layout validated.')"
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", code, str(project)],
                cwd=elsewhere, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "Relocated layout validated.")
            self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
