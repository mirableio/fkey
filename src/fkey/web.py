"""Minimal public web pages used by the account and authorization flows."""

from __future__ import annotations

import asyncio
import hmac
import html
import secrets
from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from fkey.accounts import AccountStore
from fkey.oauth import SESSION_SECONDS, LoginError, OAuthProvider

MAX_FORM_BYTES = 16 * 1024
SESSION_COOKIE = "fkey_session"


class FormSubmissionError(ValueError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


async def _read_form(request: Request, form_name: str) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type.strip().lower() != "application/x-www-form-urlencoded":
        raise FormSubmissionError("Unsupported form submission.", 415)
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = MAX_FORM_BYTES + 1
    if content_length > MAX_FORM_BYTES:
        raise FormSubmissionError(f"{form_name} form is too large.", 413)
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise FormSubmissionError(f"{form_name} form is too large.", 413)
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError as error:
        raise FormSubmissionError("Invalid form submission.", 400) from error
    return {name: values[0] if values else "" for name, values in fields.items()}


def _security_headers(
    callback_origin: str | None = None, script_nonce: str | None = None
) -> dict[str, str]:
    form_action = "form-action 'self'"
    if callback_origin is not None:
        form_action += f" {callback_origin}"
    script_source = f"; script-src 'nonce-{script_nonce}'" if script_nonce else ""
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; "
            f"{form_action}; frame-ancestors 'none'{script_source}"
        ),
        # Not no-referrer: that makes browsers send "Origin: null" on our own
        # form POSTs, which the sign-in origin check must reject.
        "Referrer-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
    }


def _submission_script(nonce: str) -> str:
    return f"""
      <script nonce="{nonce}">
        document.querySelector("form")?.addEventListener("submit", (event) => {{
          const button = event.currentTarget.querySelector('button[type="submit"]');
          if (!button) return;
          button.disabled = true;
          button.setAttribute("aria-busy", "true");
          button.textContent = button.dataset.submittingLabel;
        }});
      </script>
    """


_STYLE = """
      :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
      * { box-sizing: border-box; }
      body {
        margin: 0; min-height: 100vh; display: grid; place-items: center;
        color: #18181b; background: #f4f4f5; padding: 24px;
      }
      main {
        width: min(100%, 440px); padding: 36px; border: 1px solid #e4e4e7;
        border-radius: 18px; background: #fff; box-shadow: 0 18px 50px #18181b12;
      }
      .brand { margin: 0 0 28px; font: 700 18px ui-monospace, monospace; color: #28623b; }
      h1 { margin: 0 0 8px; font-size: 30px; letter-spacing: -0.03em; }
      .intro { margin: 0 0 22px; color: #5f5f67; line-height: 1.5; }
      .client {
        margin: 0 0 24px; padding: 13px 14px; border-radius: 10px;
        background: #f4f4f5; color: #3f3f46; line-height: 1.45;
      }
      form { display: grid; gap: 9px; }
      label { margin-top: 8px; font-size: 14px; font-weight: 650; }
      input {
        width: 100%; border: 1px solid #d4d4d8; border-radius: 10px;
        padding: 12px 13px; background: #fafafa; color: inherit; font: inherit;
      }
      input:focus { outline: 3px solid #64a97930; border-color: #3f8254; }
      .hint { margin: -3px 0 0; color: #71717a; font-size: 12px; }
      button {
        margin-top: 18px; border: 0; border-radius: 10px; padding: 13px 16px;
        background: #28623b; color: white; font: 700 15px inherit; cursor: pointer;
        transition: background-color 120ms ease, opacity 120ms ease, transform 60ms ease;
      }
      button:hover:not(:disabled) { background: #1f5030; }
      button:not(:disabled):active { transform: translateY(1px); }
      button:disabled { cursor: wait; opacity: 0.72; }
      .notice { margin-bottom: 22px; border-radius: 10px; padding: 13px 14px; line-height: 1.45; }
      .error { color: #7b2525; background: #fff0ef; border: 1px solid #f1c8c5; }
      .success { color: #205b34; background: #edf8ef; border: 1px solid #c5e3cb; }
      footer { margin-top: 28px; color: #8b8b93; font-size: 12px; line-height: 1.4; }
"""


def _document(title: str, content: str, script_nonce: str | None) -> str:
    script = _submission_script(script_nonce) if script_nonce else ""
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} · fkey</title>
    <style>{_STYLE}</style>
  </head>
  <body>
    <main>
      <p class="brand">fkey</p>
      {content}
    </main>
    {script}
  </body>
</html>"""


def _page(
    *,
    error: str = "",
    success: bool = False,
    values: Mapping[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    values = values or {}
    message = ""
    form = ""
    script_nonce = None
    if success:
        message = """
            <div class="notice success" role="status">
              <strong>Account created.</strong>
              Your personal database is ready. Connector authorization is the next step.
            </div>
        """
    else:
        script_nonce = secrets.token_hex(16)
        if error:
            message = (
                f'<div class="notice error" role="alert">{html.escape(error)}</div>'
            )
        form = f"""
          <form method="post" action="/signup">
            <label for="name">Name</label>
            <input id="name" name="name" autocomplete="name" maxlength="100"
                   value="{html.escape(values.get("name", ""), quote=True)}" required>

            <label for="email">Email</label>
            <input id="email" name="email" type="email" autocomplete="email"
                   maxlength="254"
                   value="{html.escape(values.get("email", ""), quote=True)}" required>

            <label for="password">Password</label>
            <input id="password" name="password" type="password"
                   autocomplete="new-password" minlength="10" maxlength="1024" required>
            <p class="hint">At least 10 characters.</p>

            <label for="signup_code">Signup code</label>
            <input id="signup_code" name="signup_code" type="password"
                   autocomplete="one-time-code" required>

            <button type="submit" data-submitting-label="Creating account…">Create account</button>
          </form>
        """

    content = f"""<h1>Create your account</h1>
      <p class="intro">A private database for the things and people you care about.</p>
      {message}
      {form}
      <footer>Signup is invite-only.</footer>"""
    return HTMLResponse(
        _document("Sign up", content, script_nonce),
        status_code=status_code,
        headers=_security_headers(script_nonce=script_nonce),
    )


async def signup_page(
    request: Request,
    store: AccountStore,
    configured_code: str | None,
) -> Response:
    if request.method == "GET":
        if not configured_code:
            return _page(
                error="Signup is not configured on this server.", status_code=503
            )
        return _page()

    if not configured_code:
        return _page(error="Signup is not configured on this server.", status_code=503)
    try:
        fields = await _read_form(request, "Signup")
    except FormSubmissionError as error:
        return _page(error=str(error), status_code=error.status_code)

    values = {"name": fields.get("name", ""), "email": fields.get("email", "")}
    supplied_code = fields.get("signup_code", "")
    if not hmac.compare_digest(supplied_code, configured_code):
        return _page(
            error="The signup code is incorrect.", values=values, status_code=403
        )
    try:
        # Password hashing is deliberately slow; keep it off the event loop.
        await asyncio.to_thread(
            store.create_account,
            values["name"],
            values["email"],
            fields.get("password", ""),
        )
    except ValueError as error:
        return _page(error=str(error), values=values, status_code=400)
    return _page(success=True, status_code=201)


def _login_page(
    request_id: str,
    client_name: str,
    callback_host: str,
    callback_origin: str | None = None,
    *,
    email: str = "",
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    script_nonce = secrets.token_hex(16)
    message = (
        f'<div class="notice error" role="alert">{html.escape(error)}</div>'
        if error
        else ""
    )
    content = f"""<h1>Connect your account</h1>
      <p class="intro">Sign in to let this connector use your personal taste database.</p>
      <p class="client"><strong>{html.escape(client_name)}</strong><br>
        Returns to {html.escape(callback_host)}</p>
      {message}
      <form method="post" action="/oauth/login">
        <input type="hidden" name="request" value="{html.escape(request_id, quote=True)}">
        <label for="email">Email</label>
        <input id="email" name="email" type="email" autocomplete="email"
               maxlength="254" value="{html.escape(email, quote=True)}" required>
        <label for="password">Password</label>
        <input id="password" name="password" type="password"
               autocomplete="current-password" maxlength="1024" required>
        <button type="submit" data-submitting-label="Connecting…">Connect fkey</button>
      </form>
      <footer>Only connect clients you recognize. This grants access to read and update your fkey data.</footer>"""
    return HTMLResponse(
        _document("Connect", content, script_nonce),
        status_code=status_code,
        headers=_security_headers(callback_origin, script_nonce),
    )


async def oauth_login_page(request: Request, provider: OAuthProvider) -> Response:
    if request.method == "GET":
        request_id = request.query_params.get("request", "")
        pending = provider.pending_authorization(request_id)
        if pending is None:
            return _login_page(
                request_id,
                "Unknown connector",
                "the connector",
                error="This authorization request is invalid or expired.",
                status_code=400,
            )
        return _login_page(
            request_id,
            pending["client_name"],
            pending["callback_host"],
            pending["callback_origin"],
        )

    try:
        fields = await _read_form(request, "Login")
    except FormSubmissionError as error:
        return _login_page(
            "",
            "Unknown connector",
            "the connector",
            error=str(error),
            status_code=error.status_code,
        )

    request_id = fields.get("request", "")
    pending = provider.pending_authorization(request_id)
    if pending is None:
        return _login_page(
            request_id,
            "Unknown connector",
            "the connector",
            error="This authorization request is invalid or expired.",
            status_code=400,
        )
    try:
        redirect_uri = await asyncio.to_thread(
            provider.complete_authorization,
            request_id,
            fields.get("email", ""),
            fields.get("password", ""),
        )
    except LoginError as error:
        return _login_page(
            request_id,
            pending["client_name"],
            pending["callback_host"],
            pending["callback_origin"],
            email=fields.get("email", ""),
            error=str(error),
            status_code=401,
        )
    return RedirectResponse(
        redirect_uri,
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


def _same_origin(request: Request) -> bool:
    # Browsers send Origin on form POSTs; compare hosts because a TLS proxy
    # hands the app plain HTTP.
    origin = request.headers.get("origin")
    return origin is None or urlparse(origin).netloc == request.headers.get("host")


def _app_path(value: str) -> str:
    """Return a browse UI path safe to redirect to after sign-in."""
    if value == "/app" or value.startswith(("/app/", "/app?")):
        return value
    return "/app"


def session_user(request: Request, provider: OAuthProvider) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    return provider.session_subject(token) if token else None


def _sign_in_page(
    next_path: str,
    *,
    email: str = "",
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    script_nonce = secrets.token_hex(16)
    message = (
        f'<div class="notice error" role="alert">{html.escape(error)}</div>'
        if error
        else ""
    )
    content = f"""<h1>Sign in</h1>
      <p class="intro">Browse your collections, records, and links.</p>
      {message}
      <form method="post" action="/app/login">
        <input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
        <label for="email">Email</label>
        <input id="email" name="email" type="email" autocomplete="email"
               maxlength="254" value="{html.escape(email, quote=True)}" required>
        <label for="password">Password</label>
        <input id="password" name="password" type="password"
               autocomplete="current-password" maxlength="1024" required>
        <button type="submit" data-submitting-label="Signing in…">Sign in</button>
      </form>
      <footer>Have an invite? <a href="/signup">Create an account</a>.</footer>"""
    return HTMLResponse(
        _document("Sign in", content, script_nonce),
        status_code=status_code,
        headers=_security_headers(script_nonce=script_nonce),
    )


async def app_login_page(request: Request, provider: OAuthProvider) -> Response:
    if request.method == "GET":
        next_path = _app_path(request.query_params.get("next", ""))
        if session_user(request, provider) is not None:
            return RedirectResponse(next_path, status_code=303)
        return _sign_in_page(next_path)

    if not _same_origin(request):
        return _sign_in_page("/app", error="Sign in from this site.", status_code=403)
    try:
        fields = await _read_form(request, "Sign-in")
    except FormSubmissionError as error:
        return _sign_in_page("/app", error=str(error), status_code=error.status_code)
    next_path = _app_path(fields.get("next", ""))
    try:
        # Password hashing is deliberately slow; keep it off the event loop.
        token = await asyncio.to_thread(
            provider.start_session,
            fields.get("email", ""),
            fields.get("password", ""),
        )
    except LoginError as error:
        return _sign_in_page(
            next_path,
            email=fields.get("email", ""),
            error=str(error),
            status_code=401,
        )
    response = RedirectResponse(
        next_path, status_code=303, headers={"Cache-Control": "no-store"}
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_SECONDS,
        path="/app",
        secure=provider.issuer_url.startswith("https://"),
        httponly=True,
        samesite="lax",
    )
    return response


async def app_logout(request: Request, provider: OAuthProvider) -> Response:
    if not _same_origin(request):
        return PlainTextResponse("Sign out from this site.", status_code=403)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        provider.end_session(token)
    response = RedirectResponse("/app/login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/app",
        secure=provider.issuer_url.startswith("https://"),
        httponly=True,
        samesite="lax",
    )
    return response
