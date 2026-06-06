# trade-republic-session

Mint and refresh a **Trade Republic** web-session cookie on a **headless server**,
past the **AWS-WAF challenge** — so [`pytr`](https://github.com/pytr-org/pytr) (and
anything built on it) can read your portfolio without you logging in by hand each time.

Trade Republic has no public API. `pytr` already handles reading data; the part
everyone gets stuck on is **authentication on a server with no browser**. This tool
does just that one job, cleanly:

```bash
python -m trade_republic_session          # interactive login → writes secrets/cookies.txt
python -m trade_republic_session --force  # ignore a still-valid session, log in fresh
```

It is **read-session only** by intent: it produces a cookie. It never places orders or
moves money — and neither can `pytr` with it beyond what your account allows.

## Why this exists

Logging into TR programmatically means clearing three hurdles, all handled here:

1. **The AWS WAF.** TR sits behind an AWS WAF that blocks naive HTTP logins. `pytr`
   can obtain a token two ways and this tool exposes both:
   - `--waf playwright` (default) — headless Chromium solves the challenge. Most
     reliable. Run `playwright install chromium` once.
   - `--waf awswaf` — a pure-Python solver (no browser, lighter, but flakier).
   - `--waf <token>` — a token you captured yourself.
2. **The confirmation code.** TR pushes a **4-digit code** to its app (or SMS). You
   type it once; the session then persists for days.
   > **Dead end worth knowing:** an external authenticator's **TOTP code does _not_
   > work** for TR web login — TR's confirm endpoint rejects it. Only the push/SMS
   > code works. (`--totp-secret` is kept for the rare linked-authenticator case, but
   > expect the interactive flow.)
3. **Session persistence.** The result is a single cookie file, `chmod 0600`, that
   `pytr` resumes. TR sessions are short-lived (a few days), so re-run this when reads
   start failing.

## Install

```bash
pip install -e .            # or: pip install trade-republic-session  (once published)
playwright install chromium # once, for the default --waf playwright
```

Python ≥ 3.10.

## Usage

```bash
# Interactive (prompts for phone + PIN if not provided, then the 4-digit code):
python -m trade_republic_session
# or the console script:
tr-session

# Choose where the cookie lives (default: secrets/cookies.txt):
tr-session --cookies-file ~/.config/tr/cookies.txt
#   …or set TR_COOKIES_FILE.

# Run something after a successful login (restart a service, kick a sync, …):
tr-session --on-success "systemctl --user restart my-tr-reader"
```

Then point `pytr` at the same file:

```python
from pytr.api import TradeRepublicApi
tr = TradeRepublicApi(phone_no=PHONE, pin=PIN, cookies_file="secrets/cookies.txt")
assert tr.resume_websession()      # no interactive login needed
```

### Credential resolution

Phone + PIN (and optional TOTP secret) are resolved in order:

1. **TPM-sealed blob** — `$CREDENTIALS_DIRECTORY/tr-secrets` (KEY=VALUE lines), as
   mounted by a systemd unit via `LoadCredentialEncrypted=` (see below).
2. **Environment** — `TR_PHONE_NO`, `TR_PIN`, `TR_TOTP_SECRET`.
3. **Interactive prompt** for anything still missing (PIN is read hidden).

Keys: `TR_PHONE_NO` (e.g. `+4912345678`), `TR_PIN`, optional `TR_TOTP_SECRET`,
`TR_LOCALE` (default `en`), `TR_WAF` (default `playwright`).

## The web re-login bridge (optional pattern)

Re-authenticating a **host** process (it needs your phone/PIN and a browser) from a
**sandboxed web UI** (e.g. a containerised dashboard) is a recurring problem. `--ui-dir`
implements a small file-based bridge for it:

```bash
tr-session --ui-dir /run/tr-login --on-success "..."
```

- The process writes its phase to `<dir>/status.json` (atomic): `starting` →
  `awaiting_code` → `verifying` → `syncing` → `ok` (or `error` / `cancelled`).
- It reads the 4-digit code from `<dir>/code`; the sentinels `RESEND` (send SMS) and
  `CANCEL` are also accepted.

Your web layer polls `status.json` and writes `code`. Trigger this host process from a
file-watch (e.g. a systemd `.path` unit) so the browser never touches credentials. The
whole login (initiate → confirm) runs in one process so the WAF token and session stay
in memory.

## Running unattended (systemd + TPM)

Keep secrets off disk in plaintext by sealing them to the TPM and mounting them only
into the unit:

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

- **`secrets/cookies.txt` is a live session credential.** It is created `0600` and the
  whole `secrets/` directory is gitignored. Treat it like a password.
- Prefer the TPM-sealed-creds path over plaintext env files for phone/PIN.
- This tool reads credentials and writes a cookie — it makes no trades and moves no
  money.

## Caveats

- **Unofficial.** TR has no public API; this builds on the reverse-engineered `pytr`.
  Use at your own risk and within TR's terms.
- **Fragile by nature.** TR and the AWS WAF change; logins can break and the `awswaf`
  solver is the flakier path (prefer `playwright`). Best-effort, not a guarantee.
- **Heavyweight default.** The `playwright` strategy needs a Chromium download.

## Credits

Built on [`pytr`](https://github.com/pytr-org/pytr), which does the Trade Republic
protocol and the WAF-token strategies. This project is just the headless login/session
lifecycle around it. MIT licensed.
