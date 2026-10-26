"""User-started OAuth with a console URL and credentials kept in memory only."""

from __future__ import annotations

from contextlib import contextmanager
import logging

from .uploader_connection import CheckError


SCOPES = (
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
)

AUTHORIZATION_PROMPT = (
    "\nCopy the complete Google sign-in URL below and paste it into your browser.\n"
    "Select the Google account and YouTube channel configured in upload-order.txt.\n"
    "Keep this console open while you sign in.\n\n"
    "{url}\n\n"
    "Waiting up to five minutes. Ctrl+C cancels.\n"
)


@contextmanager
def authorized_session(config: dict):
    """Display the requested sign-in link; keep access/refresh tokens in memory."""
    # The OAuth library normally logs the prompt and callback URI. Only the
    # explicit console prompt is allowed; URLs/codes/tokens must not reach logs.
    logging.disable(logging.CRITICAL)
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import AuthorizedSession, Request
        import requests
    except ImportError:
        raise CheckError("Dependencies are missing. Run start.cmd to install them.") from None

    class TimedFlow(InstalledAppFlow):
        def fetch_token(self, **kwargs):
            kwargs.setdefault("timeout", 30)
            return super().fetch_token(**kwargs)

    class TimedRefreshRequest(Request):
        def __call__(self, *args, **kwargs):
            kwargs["timeout"] = 30
            return super().__call__(*args, **kwargs)

    flow = TimedFlow.from_client_config(
        config, scopes=list(SCOPES), autogenerate_code_verifier=True
    )
    try:
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=False,
            timeout_seconds=300,
            authorization_prompt_message=AUTHORIZATION_PROMPT,
            success_message=(
                "Google sign-in returned to the uploader. "
                "You may close this tab and read the result in the console."
            ),
            prompt="select_account consent",
            access_type="offline",
            include_granted_scopes="false",
        )
        if not credentials.refresh_token:
            raise CheckError(
                "Google did not supply authorization that can last through a long batch. "
                "Run again and approve the requested permissions."
            )
        granted = credentials.granted_scopes
        if granted is not None and not set(SCOPES).issubset(set(granted)):
            raise CheckError("Both the YouTube read-only and upload permissions are required.")
        with requests.Session() as refresh_session:
            # Refresh before expired requests, but never silently replay an upload POST.
            with AuthorizedSession(
                credentials,
                auth_request=TimedRefreshRequest(session=refresh_session),
                max_refresh_attempts=0,
            ) as session:
                yield session
    finally:
        flow.oauth2session.close()
