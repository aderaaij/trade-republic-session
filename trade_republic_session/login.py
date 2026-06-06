#!/usr/bin/env python3
"""Trade Republic login — unattended + web-UI wrapper around pytr.

``pytr`` already logs into Trade Republic (it gets the AWS-WAF token, takes your
phone/PIN, handles the 4-digit/SMS code, and saves the cookie). If you just want to
log in on your own machine, use ``pytr login`` — you don't need this. This wrapper
adds only two things, for servers/dashboards: it resolves credentials from a
TPM-sealed blob or env (no plaintext file / prompt) so a systemd unit can run it
unattended, and it offers a ``--ui-dir`` file bridge so a sandboxed web UI can drive
the interactive login. It writes the same web-session cookie pytr resumes from.

    python -m trade_republic_session            # login (creds from sealed blob/env/prompt)
    python -m trade_republic_session --force    # ignore a still-valid session

The cookie is written to ``secrets/cookies.txt`` by default (override with
``--cookies-file`` or ``$TR_COOKIES_FILE``); nothing else is touched.

Credentials (phone + PIN) are resolved, in order, from:
  1. ``$CREDENTIALS_DIRECTORY/tr-secrets`` — a TPM-sealed KEY=VALUE blob, when run
     under a systemd unit with ``LoadCredentialEncrypted=tr-secrets:...cred``
  2. ``TR_PHONE_NO`` / ``TR_PIN`` / ``TR_TOTP_SECRET`` in the environment
  3. an interactive prompt for anything still missing

The AWS WAF needs a token; pytr can obtain one for you:
  --waf playwright  headless Chromium (DEFAULT, most reliable; run once:
                    ``playwright install chromium``)
  --waf awswaf      pure-Python challenge solver (no browser, lighter but flaky)
  --waf <token>     a token you captured yourself

Confirmation is a 4-digit code TR pushes to its app (or SMS). NOTE: an external
authenticator's TOTP code does NOT work for TR web login (the confirm endpoint
rejects it) — only the push/SMS code does. ``--totp-secret`` is kept as a probe
for the rare linked-authenticator case, but expect the interactive flow.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import subprocess
import sys
import time
from getpass import getpass
from pathlib import Path

logger = logging.getLogger(__name__)

# UI mode: how long the host login process waits for a web UI to submit the 4-digit
# code before giving up (a driving systemd unit's TimeoutStartSec must exceed this).
UI_CODE_TIMEOUT_S = 240

DEFAULT_COOKIES = "secrets/cookies.txt"


def _resolve_cookies_file(arg: str) -> Path:
    path = arg or os.environ.get("TR_COOKIES_FILE") or DEFAULT_COOKIES
    return Path(path).expanduser()


def _read_sealed() -> dict[str, str]:
    """Parse KEY=VALUE lines from a TPM-sealed ``tr-secrets`` blob, if mounted by
    systemd via ``LoadCredentialEncrypted`` (``$CREDENTIALS_DIRECTORY``)."""
    out: dict[str, str] = {}
    creds_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not creds_dir:
        return out
    sealed = Path(creds_dir) / "tr-secrets"
    if not sealed.exists():
        return out
    for line in sealed.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _resolve_credentials() -> tuple[str, str, str]:
    sealed = _read_sealed()
    phone = sealed.get("TR_PHONE_NO") or os.environ.get("TR_PHONE_NO", "")
    pin = sealed.get("TR_PIN") or os.environ.get("TR_PIN", "")
    totp = sealed.get("TR_TOTP_SECRET") or os.environ.get("TR_TOTP_SECRET", "")
    if not phone:
        phone = input("Trade Republic phone number (e.g. +4912345678): ").strip()
    if not pin:
        pin = getpass("Trade Republic PIN (hidden): ").strip()
    return phone, pin, totp


def _generate_totp(secret: str) -> str:
    try:
        import pyotp
    except ImportError:
        sys.exit("pyotp is required for --totp-secret (pip install pyotp).")
    return pyotp.TOTP(secret.replace(" ", "")).now()


def _lockdown(path: Path) -> None:
    """The cookie is a live session credential — keep it 0600."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _run_on_success(cmd: str) -> None:
    """Run an optional shell command after a successful login (e.g. restart a service
    or re-sync). Best-effort — login already succeeded, so a failure here is logged
    but not fatal."""
    if not cmd:
        return
    try:
        subprocess.run(cmd, shell=True, check=False, timeout=300)
    except Exception:  # noqa: BLE001
        logger.exception("--on-success command failed")


def _ui_login(tr, ui_dir: Path, cookies_file: Path, totp_secret: str, on_success: str) -> int:
    """Non-interactive login driven by an external UI via files in ``ui_dir``.

    Status is written to ``status.json`` (a web layer can serve it to a modal); the
    4-digit code is read from the ``code`` file (the web layer writes it when the user
    submits). ``code`` may also be the sentinel ``RESEND`` (deliver via SMS) or
    ``CANCEL`` (abort). One process spans initiate→complete so the pytr session and
    WAF token stay in memory. This is the reusable "re-auth a host process from a
    sandboxed web UI" bridge — see the README.
    """
    ui_dir.mkdir(parents=True, exist_ok=True)
    status_file = ui_dir / "status.json"
    code_file = ui_dir / "code"

    def report(phase: str, **extra) -> None:
        payload = {"phase": phase, "ts": int(time.time()), **extra}
        tmp = status_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, status_file)  # atomic — reader never sees a partial file
        try:
            os.chmod(status_file, 0o644)  # readable by a separate web process
        except OSError:
            pass

    code_file.unlink(missing_ok=True)  # drop any stale code from a prior attempt
    report("starting")

    try:
        countdown = tr.initiate_weblogin()
    except Exception as e:  # noqa: BLE001
        report("error", message=f"Could not start login: {e}")
        return 1

    # Optional headless probe — generate the code locally if an authenticator is set.
    if totp_secret:
        try:
            tr.complete_weblogin(_generate_totp(totp_secret))
        except Exception as e:  # noqa: BLE001
            report("error", message=f"Authenticator code rejected: {e}")
            return 1
        return _ui_finish(tr, cookies_file, report, on_success)

    report("awaiting_code", countdown=int(countdown), can_sms=True)

    deadline = time.time() + UI_CODE_TIMEOUT_S
    resent = False
    code = ""
    while time.time() < deadline:
        if code_file.exists():
            val = code_file.read_text().strip()
            code_file.unlink(missing_ok=True)
            if val == "CANCEL":
                report("cancelled")
                return 1
            if val == "RESEND":
                if not resent:
                    try:
                        tr.resend_weblogin()
                        resent = True
                    except Exception as e:  # noqa: BLE001
                        report("error", message=f"Could not resend the code: {e}")
                        return 1
                report("awaiting_code", countdown=int(countdown), can_sms=False, resent=True)
                continue
            if val:
                code = val
                break
        time.sleep(1)

    if not code:
        report("error", message="Timed out waiting for the code. Try again.")
        return 1

    report("verifying")
    try:
        tr.complete_weblogin(code)
    except Exception as e:  # noqa: BLE001
        report("error", message=f"Code rejected: {e}")
        return 1
    return _ui_finish(tr, cookies_file, report, on_success)


def _ui_finish(tr, cookies_file: Path, report, on_success: str) -> int:
    if not tr.resume_websession():
        report("error", message="Logged in but the session did not persist.")
        return 1
    _lockdown(cookies_file)
    if on_success:
        report("syncing")  # let the UI show progress while the hook runs
        _run_on_success(on_success)
    report("ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="trade-republic-session",
        description="Mint / refresh a Trade Republic web-session cookie (for pytr).",
    )
    parser.add_argument(
        "--cookies-file",
        default="",
        help=f"Where to read/write the session cookie. Env: TR_COOKIES_FILE. "
        f"Default: {DEFAULT_COOKIES}",
    )
    parser.add_argument(
        "--waf",
        default=os.environ.get("TR_WAF", "playwright"),
        help="WAF token strategy: playwright (default, most reliable), awswaf "
        "(pure-Python, flaky), or a literal token. Env: TR_WAF.",
    )
    parser.add_argument(
        "--totp-secret",
        default="",
        help="Base32 secret of a linked authenticator (headless probe; usually does "
        "NOT work for TR — kept for the rare linked-authenticator case). Falls back "
        "to TR_TOTP_SECRET from the env/sealed creds.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip resuming the existing session and force a fresh web login.",
    )
    parser.add_argument(
        "--on-success",
        default="",
        help="Shell command to run after a successful login (e.g. restart a service "
        "or re-sync). Best-effort.",
    )
    parser.add_argument(
        "--ui-dir",
        default="",
        help="Run non-interactively, driven by a web UI: write phase to "
        "<dir>/status.json and read the 4-digit code from <dir>/code (sentinels "
        "RESEND / CANCEL). See the README's re-login bridge section.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from pytr.api import TradeRepublicApi

    cookies_file = _resolve_cookies_file(args.cookies_file)
    cookies_file.parent.mkdir(parents=True, exist_ok=True)

    phone, pin, sealed_totp = _resolve_credentials()
    if not (phone and pin):
        print("Phone number and PIN are required.", file=sys.stderr)
        return 1
    totp_secret = args.totp_secret or sealed_totp

    tr = TradeRepublicApi(
        phone_no=phone,
        pin=pin,
        locale=os.environ.get("TR_LOCALE", "en"),
        save_cookies=True,
        cookies_file=str(cookies_file),
        waf_token=args.waf,
    )

    # UI mode (web-driven): always start a fresh login and report via files.
    if args.ui_dir:
        return _ui_login(tr, Path(args.ui_dir), cookies_file, totp_secret, args.on_success)

    if not args.force and tr.resume_websession():
        print(f"Existing session is still valid — nothing to do.\n  {cookies_file}")
        print("(Pass --force to start a fresh login anyway.)")
        _lockdown(cookies_file)
        return 0

    print("Starting web login..." + (" (forced)" if args.force else " (no valid session)"))
    try:
        countdown = tr.initiate_weblogin()
    except Exception as e:  # noqa: BLE001
        print(f"\nLogin initiation failed: {e}", file=sys.stderr)
        if args.waf == "awswaf":
            print(
                "The pure-Python WAF solver may have been blocked. Retry with:\n"
                "  python -m trade_republic_session --waf playwright\n"
                "(first run once: playwright install chromium)",
                file=sys.stderr,
            )
        return 1

    if totp_secret:
        code = _generate_totp(totp_secret)
        print(f"Submitting locally-generated authenticator code ({code}).")
    else:
        print("Enter the code from your Trade Republic app notification.")
        print(f"Leave empty to receive it as SMS instead. (Countdown: {countdown}s)")
        request_time = time.time()
        code = input("Code: ").strip()
        if code == "":
            wait = countdown - (time.time() - request_time)
            for remaining in range(int(max(0, wait))):
                print(f"  waiting {int(wait - remaining)}s before SMS...", end="\r")
                time.sleep(1)
            print()
            tr.resend_weblogin()
            code = input("SMS sent. Enter the confirmation code: ").strip()

    try:
        tr.complete_weblogin(code)
    except Exception as e:  # noqa: BLE001
        print(f"\nLogin completion failed: {e}", file=sys.stderr)
        if totp_secret:
            print(
                "TR rejected the authenticator code — the headless TOTP path does "
                "not work for TR; use the interactive (push/SMS) flow.",
                file=sys.stderr,
            )
        return 1

    if not tr.resume_websession():
        print("Logged in but session did not persist — check the cookie file.", file=sys.stderr)
        return 1

    _lockdown(cookies_file)
    print(f"\nLogged in. Session saved to:\n  {cookies_file}")
    if args.on_success:
        _run_on_success(args.on_success)
    else:
        print("pytr (and anything built on it) can now resume this cookie.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
