# trade-republic-session

A thin layer over [`pytr`](https://github.com/pytr-org/pytr) that runs **Trade Republic
login unattended** (TPM-sealed credentials, systemd) and lets you **re-authenticate it
from a web UI**. It writes the same session cookie `pytr` uses.

> **If you just want to log into TR on your own machine, use `pytr login` directly.**
> It already handles the AWS-WAF, your phone/PIN, the 4-digit (or SMS) code, and saving
> the cookie. This project does **not** reinvent any of that — it only adds value for
> **headless servers** and **dashboards**, where the interactive `pytr login` is awkward.

## What `pytr` already does (and this reuses, doesn't replace)

- Obtains the **AWS-WAF token** (`playwright` default, or `awswaf`) — pytr's code.
- Prompts for / reads **phone + PIN**, runs the web login, takes the **4-digit code with
  SMS fallback**, and **saves + resumes** the cookie.

So the login itself is pytr. This wrapper is worth installing only if you need one of the
two things below.

## What this adds

1. **Unattended, sealed credentials.** `pytr` reads phone/PIN from an interactive prompt
   or a **plaintext** credentials file. This resolves them from a **TPM-sealed blob**
   (`$CREDENTIALS_DIRECTORY/tr-secrets`, mounted by systemd `LoadCredentialEncrypted=`)
   or env vars — no plaintext on disk, no prompt — so a scheduled/`oneshot` unit can run it.
2. **A web re-login bridge (`--ui-dir`).** A small file protocol (`status.json` + `code`,
   with `RESEND`/`CANCEL` sentinels) so a **sandboxed web app** can drive the interactive
   login — show progress, collect the 4-digit code — without the browser ever touching
   your credentials. This is the genuinely novel piece; it isn't in pytr.

Plus minor conveniences: the cookie is written `chmod 0600`, an `--on-success` hook,
`--force`, and `--cookies-file` / `TR_COOKIES_FILE`.

If neither (1) nor (2) applies to you, **you don't need this — use `pytr login`.**

## Install

```bash
pip install -e .            # depends only on pytr
playwright install chromium # once, for pytr's default WAF strategy
```

Python ≥ 3.10.

## Usage

```bash
# Unattended login (creds from sealed blob / env; cookie to secrets/cookies.txt):
tr-session
#   or: python -m trade_republic_session

tr-session --force                                  # ignore a still-valid session
tr-session --cookies-file ~/.config/tr/cookies.txt  # or set TR_COOKIES_FILE
tr-session --on-success "systemctl --user restart my-tr-reader"
```

Then point `pytr` at the same cookie:

```python
from pytr.api import TradeRepublicApi
tr = TradeRepublicApi(phone_no=PHONE, pin=PIN, cookies_file="secrets/cookies.txt")
assert tr.resume_websession()   # no interactive login needed
```

### Credential resolution (the unattended bit)

Resolved in order:

1. **TPM-sealed blob** — `$CREDENTIALS_DIRECTORY/tr-secrets` (KEY=VALUE lines), mounted by
   systemd via `LoadCredentialEncrypted=`.
2. **Environment** — `TR_PHONE_NO`, `TR_PIN`, optional `TR_TOTP_SECRET`.
3. **Interactive prompt** for anything missing.

Other env: `TR_LOCALE` (default `en`), `TR_WAF` (passed straight to pytr; default
`playwright`).

> **Note on TOTP:** an external authenticator's code does **not** work for TR web login —
> TR's confirm endpoint rejects it; only the push/SMS code does. `--totp-secret` is kept
> for the rare linked-authenticator case, but expect the interactive flow.

## The web re-login bridge (`--ui-dir`)

The one thing here that pytr can't do. Re-authenticating a **host** process (it needs your
phone/PIN and a browser for the WAF) from a **sandboxed web UI** (e.g. a containerised
dashboard) needs an out-of-band channel. `--ui-dir` is that channel:

```bash
tr-session --ui-dir /run/tr-login --on-success "..."
```

- The process writes its phase to `<dir>/status.json` (atomic): `starting` →
  `awaiting_code` → `verifying` → `syncing` → `ok` (or `error` / `cancelled`).
- It reads the 4-digit code from `<dir>/code`; `RESEND` (send SMS) and `CANCEL` are also
  accepted.

Your web layer polls `status.json` and writes `code`. Trigger this host process from a
file-watch (e.g. a systemd `.path` unit) so the browser never sees credentials. The whole
login runs in one process so the WAF token and session stay in memory.

## Running unattended (systemd + TPM)

```ini
# ~/.config/systemd/user/tr-login.service  (Type=oneshot)
[Service]
LoadCredentialEncrypted=tr-secrets:%h/secrets/tr-secrets.cred
Environment=TR_COOKIES_FILE=%h/secrets/cookies.txt
ExecStart=%h/.venv/bin/python -m trade_republic_session --ui-dir %h/run/tr-login
TimeoutStartSec=300
```

Seal the blob once:

```bash
printf 'TR_PHONE_NO=+49...\nTR_PIN=1234\n' > /dev/shm/tr
systemd-creds encrypt --with-key=tpm2 --name=tr-secrets /dev/shm/tr ~/secrets/tr-secrets.cred
shred -u /dev/shm/tr
```

(`TimeoutStartSec` must exceed the in-tool code wait, ~240 s.)

## Security

- **`secrets/cookies.txt` is a live session credential** — created `0600`, and the whole
  `secrets/` dir is gitignored. Treat it like a password.
- Prefer the TPM-sealed path over a plaintext credentials file for phone/PIN.
- This tool reads credentials and writes a cookie; it makes no trades and moves no money.

## Caveats

- **Unofficial.** TR has no public API; everything here builds on the reverse-engineered
  `pytr`. Use at your own risk and within TR's terms.
- **Fragile.** TR and the WAF change; logins can break (the `awswaf` strategy is the
  flakier one — prefer `playwright`). Best-effort.

## Credits

All the heavy lifting — the Trade Republic protocol, the AWS-WAF token, the login flow — is
[`pytr`](https://github.com/pytr-org/pytr). This project is only the unattended-credentials
and web-re-login wrapper around it. Released into the **public domain**
([The Unlicense](https://unlicense.org)) — no copyright, use it however you like.
