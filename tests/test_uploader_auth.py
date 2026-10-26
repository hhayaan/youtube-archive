"""OAuth behavior with synthetic credentials, callbacks, and entirely mocked I/O."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from google_auth_oauthlib.flow import InstalledAppFlow

from data.uploader_connection import CheckError
from data.uploader_auth import AUTHORIZATION_PROMPT, SCOPES, authorized_session


REAL_LOCAL_SERVER = InstalledAppFlow.run_local_server
SYNTHETIC_URL = "https://example.invalid/authorize?synthetic=true&state=test-only"


class AuthTests(unittest.TestCase):
    def setUp(self):
        owner = self
        self.credentials = SimpleNamespace(
            refresh_token="synthetic-memory-only", granted_scopes=list(SCOPES)
        )
        self.config = {"synthetic": "not a real client configuration"}
        self.flow = None
        self.refresh_session = None
        self.authorized = None
        self.failure = None
        previous_disable = logging.root.manager.disable
        self.addCleanup(logging.disable, previous_disable)

        class FakeFlow:
            def __init__(self):
                self.oauth2session = MagicMock()
                self.credentials = owner.credentials

            @classmethod
            def from_client_config(cls, config, **kwargs):
                owner.flow_config = config
                owner.flow_options = kwargs
                owner.flow = cls()
                return owner.flow

            def run_local_server(self, **kwargs):
                owner.callback_options = kwargs
                if owner.failure is not None:
                    raise owner.failure
                self.fetch_token(code="synthetic-callback-only")
                return self.credentials

            def authorization_url(self, **kwargs):
                owner.authorization_options = kwargs
                return SYNTHETIC_URL, "synthetic-state-only"

            def fetch_token(self, **kwargs):
                owner.fetch_options = kwargs

        class FakeRequest:
            def __init__(self, session):
                self.session = session

            def __call__(self, *args, **kwargs):
                owner.refresh_options = kwargs
                return "synthetic-refresh-result"

        class FakeRefreshSession:
            def __init__(self):
                self.closed = False
                owner.refresh_session = self

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.closed = True

        class FakeAuthorizedSession:
            def __init__(self, credentials, **kwargs):
                self.closed = False
                self.credentials = credentials
                self.auth_request = kwargs["auth_request"]
                owner.authorized_options = kwargs
                owner.authorized = self

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.closed = True

        self.fake_flow_type = FakeFlow
        for target, value in (
            ("google_auth_oauthlib.flow.InstalledAppFlow", FakeFlow),
            ("google.auth.transport.requests.Request", FakeRequest),
            ("google.auth.transport.requests.AuthorizedSession", FakeAuthorizedSession),
            ("requests.Session", FakeRefreshSession),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_manual_url_prompt_disables_browser_and_retains_callback_protection(self):
        with authorized_session(self.config) as session:
            self.assertIs(session, self.authorized)
            self.assertIs(self.flow_config, self.config)
            self.assertEqual(self.flow_options, {
                "scopes": list(SCOPES), "autogenerate_code_verifier": True,
            })
            options = self.callback_options
            self.assertIs(options["open_browser"], False)
            self.assertEqual(options["host"], "127.0.0.1")
            self.assertEqual(options["port"], 0)
            self.assertEqual(options["timeout_seconds"], 300)
            self.assertEqual(options["prompt"], "select_account consent")
            self.assertEqual(options["access_type"], "offline")
            self.assertEqual(options["include_granted_scopes"], "false")
            prompt = options["authorization_prompt_message"]
            self.assertIn("\n{url}\n", prompt)
            self.assertIn("Keep this console open", prompt)
            self.assertIn("Ctrl+C", prompt)
            self.assertEqual(self.fetch_options["timeout"], 30)
        self.assertTrue(self.authorized.closed)
        self.assertTrue(self.refresh_session.closed)
        self.flow.oauth2session.close.assert_called_once_with()

    def test_memory_refresh_has_timeout_and_no_automatic_post_replay(self):
        with authorized_session(self.config) as session:
            self.assertEqual(self.authorized_options["max_refresh_attempts"], 0)
            self.assertIs(session.credentials, self.credentials)
            self.assertIs(session.auth_request.session, self.refresh_session)
            result = session.auth_request("synthetic-token-endpoint", timeout=999)
            self.assertEqual(result, "synthetic-refresh-result")
            self.assertEqual(self.refresh_options["timeout"], 30)

    def test_actual_library_prints_copyable_synthetic_url_once_without_opening_browser(self):
        server = SimpleNamespace(server_port=43123, server_close=MagicMock())

        def make_server(host, port, app, **kwargs):
            self.assertEqual((host, port), ("127.0.0.1", 0))
            server.handle_request = lambda: setattr(
                app, "last_request_uri",
                "http://127.0.0.1:43123/?code=synthetic-only&state=test-only",
            )
            return server

        captured = io.StringIO()
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        with (
            patch.object(self.fake_flow_type, "run_local_server", REAL_LOCAL_SERVER),
            patch("google_auth_oauthlib.flow.wsgiref.simple_server.make_server", side_effect=make_server),
            patch("google_auth_oauthlib.flow.webbrowser.get") as browser,
            redirect_stdout(captured),
        ):
            with authorized_session(self.config):
                pass
        browser.assert_not_called()
        self.assertEqual(captured.getvalue().count(SYNTHETIC_URL), 1)
        self.assertIn("\n" + SYNTHETIC_URL + "\n", captured.getvalue())
        self.assertEqual(logs.getvalue(), "")
        self.assertEqual(server.timeout, 300)
        self.assertEqual(self.flow.redirect_uri, "http://127.0.0.1:43123/")
        self.assertEqual(self.fetch_options["timeout"], 30)
        server.server_close.assert_called_once_with()
        self.flow.oauth2session.close.assert_called_once_with()

    def test_missing_refresh_token_stops_before_authorized_session_is_exposed(self):
        self.credentials.refresh_token = None
        with self.assertRaisesRegex(CheckError, "long batch"):
            with authorized_session(self.config):
                self.fail("No authorized session should be yielded")
        self.assertIsNone(self.authorized)
        self.flow.oauth2session.close.assert_called_once_with()

    def test_missing_required_scope_stops_before_upload_session(self):
        self.credentials.granted_scopes = [SCOPES[0]]
        with self.assertRaisesRegex(CheckError, "Both"):
            with authorized_session(self.config):
                self.fail("No authorized session should be yielded")
        self.assertIsNone(self.authorized)
        self.flow.oauth2session.close.assert_called_once_with()

    def test_unspecified_granted_scopes_preserve_existing_library_behavior(self):
        self.credentials.granted_scopes = None
        with authorized_session(self.config) as session:
            self.assertIs(session, self.authorized)

    def test_cancellation_cleans_flow_without_logging_or_saving_credentials(self):
        self.failure = KeyboardInterrupt()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(KeyboardInterrupt):
                with authorized_session(self.config):
                    self.fail("Cancelled authorization must never yield")
        self.flow.oauth2session.close.assert_called_once_with()
        self.assertIsNone(self.authorized)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_caller_failure_propagates_and_closes_all_in_memory_resources(self):
        failure = RuntimeError("synthetic caller failure")
        with self.assertRaises(RuntimeError) as caught:
            with authorized_session(self.config):
                raise failure
        self.assertIs(caught.exception, failure)
        self.assertTrue(self.authorized.closed)
        self.assertTrue(self.refresh_session.closed)
        self.flow.oauth2session.close.assert_called_once_with()

    def test_auth_does_not_open_any_file_or_print_credentials(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("builtins.open", side_effect=AssertionError("No file access is allowed")),
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            with authorized_session(self.config):
                pass
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(set(SCOPES), {
            "https://www.googleapis.com/auth/youtube.readonly",
            "https://www.googleapis.com/auth/youtube.upload",
        })


if __name__ == "__main__":
    unittest.main()
