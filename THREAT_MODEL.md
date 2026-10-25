# Threat Model: Canvas2Google

Canvas2Google reads a Canvas LMS calendar feed (iCal) and syncs it to Google:

- calendar events go to a dedicated **"Canvas"** Google Calendar
- assignments go to a **"Canvas"** Google Tasks list

It runs in two forms that share one core (`canvas_sync/`):

- **Website:** `webapp/`, Flask on Vercel at `https://canvas2google.vercel.app`, with Postgres on Neon and a daily Vercel cron. It's live for one allowlisted user. The Google OAuth app is "In production" but **unverified** (100-user cap).
- **Command-line tool:** `cli.py`, run locally with secrets in local files.

*Last reviewed: 2026-10-25 (Milestone 12: public-launch hardening).*

---

## 1. Assets

| # | Asset | Where it lives | Why it matters |
|---|---|---|---|
| A1 | **Canvas feed URL** | Web: Postgres, **encrypted**. CLI: `.env` | Works like a password: anyone with it can read the user's whole Canvas calendar without signing in. |
| A2 | **Google refresh/access tokens** | Web: Postgres, **encrypted**. CLI: `token.json` | Grant the app's scopes until revoked: read/write on all of the user's Google Tasks, plus calendars the app created. |
| A3 | **App encryption key** (`APP_ENCRYPTION_KEY`) | Vercel environment variable (sensitive) | Decrypts A1 and A2 for every user. The single most valuable secret. |
| A4 | **Session signing key** (`FLASK_SECRET_KEY`) | Vercel environment variable (sensitive) | Anyone holding it can forge a signed-in session cookie for any user. |
| A5 | **Cron secret** (`CRON_SECRET`) | Vercel environment variable (sensitive) | Lets the caller trigger a sync for all users. |
| A6 | **OAuth client secrets** | Web: Vercel environment variable. CLI: `credentials.json` | Identify the app to Google. They can't grant user access on their own, but combined with a stolen code or token they help an attacker. |
| A7 | **Database** (Neon Postgres) | Neon, reached only through `DATABASE_URL` | Holds A1/A2 (encrypted), user emails, sync records and sync history (assignment titles). |
| A8 | **Sync records** (state) | Postgres / `sync_state.json` | Low confidentiality, but their **integrity** prevents duplicate calendars and tasks. |
| A9 | **Admin accounts** | GitHub, Vercel, Neon, Google Cloud | Whoever controls these controls the code, the secrets and the data. |
| A10 | **Code and dependencies** | GitHub repo; pinned `requirements.txt` | A malicious or vulnerable dependency runs with access to everything above. |

## 2. Data flow and trust boundaries

```
                     ┌─────────────── Vercel (HTTPS, serverless) ───────────────┐
 Browser ──HTTPS──▶  │ Flask app (webapp/)                                       │ ──OAuth-scoped──▶ Google Calendar / Tasks
 (user)    cookies   │  auth · views · tasks(/tasks/sync-all) · sync_runner      │
                     │  env: FLASK_SECRET_KEY, APP_ENCRYPTION_KEY, CRON_SECRET… │ ──HTTPS (SSRF-guarded)──▶ *.instructure.com feed
 Vercel cron ─Bearer─▶│                                                           │
                     └──────────────┬────────────────────────────────────────────┘
                                    │ TLS (sslmode=require)
                              Neon Postgres: users, encrypted secrets, state, history, locks

 Google sign-in: browser ⇄ accounts.google.com → /auth/callback (state + PKCE) → code exchanged server-side
```

There are five trust boundaries:

1. **Internet → app.** Every request is untrusted until it has a valid session, CSRF token or cron secret.
2. **Canvas feed → app.** Feed text is **untrusted**: anyone who can edit course content controls it.
3. **App → Google.** Requests are authenticated with A2 and limited by the OAuth scopes.
4. **App → database.** Data crosses to a third-party host (Neon). Secrets are encrypted *before* they leave the app.
5. **Platform operators.** Vercel, Neon and GitHub can see the code, environment variables or database. They're trusted, and their accounts are part of the attack surface (A9).

## 3. Threats and mitigations

### Web application

| ID | Threat | Mitigations | Status |
|---|---|---|---|
| W1 | **Unauthorized sign-in.** A stranger uses the site. | Google sign-in only. `SIGNUP_MODE=invite` (live setting) checks an **email allowlist** *before anything is stored*; `open` admits any verified Google account. The ID token's signature, audience, issuer and expiry are verified. Refused accounts have their fresh Google grant **revoked**. Tested, including planted bugs that removed the allowlist (caught). | Mitigated |
| W2 | **Login CSRF / sign-in hijack.** An attacker swaps in their own OAuth code. | A random `state` value is stored in the session and checked with a constant-time comparison; it can only be used once. **PKCE** binds the code to this browser's verifier. The code is exchanged server-side with the client secret. | Mitigated |
| W3 | **CSRF.** Another site makes the user's browser submit forms. | A Flask-WTF token on every POST. `SameSite=Lax` cookies. Over HTTPS, a strict same-site `Referer` check (this is why the Referrer-Policy is `same-origin`; see §5). | Mitigated |
| W4 | **XSS.** Feed titles or descriptions inject script into pages. | Jinja auto-escaping is on and `|safe` is never used. **CSP `default-src 'none'`**: no scripts at all, only our stylesheet. Feed HTML is converted to plain text before use. Tested with `<script>` titles. | Mitigated |
| W5 | **Session theft or fixation** | `__Host-` prefixed, `Secure`, `HttpOnly`, `SameSite=Lax` cookie with a 12-hour lifetime. A fresh session starts on sign-in. HSTS (1 year). | Mitigated |
| W6 | **Clickjacking / framing** | `frame-ancestors 'none'` and `X-Frame-Options: DENY`. | Mitigated |
| W7 | **SSRF via the feed URL.** A user makes the server fetch internal addresses. | Only `https://*.instructure.com/feeds/calendars/*.ics` is accepted. No credentials or ports in the URL. **Redirects are never followed**. Tested against the cloud metadata IP, localhost, lookalike domains, `user:pass@` and odd ports. | Mitigated |
| W8 | **Unauthorized sync trigger** | `/tasks/sync-all` needs `Authorization: Bearer <CRON_SECRET>` (32+ characters, constant-time comparison). It's disabled when the secret is unset. It's CSRF-exempt because no browser should call it. Returns counts only. | Mitigated |
| W9 | **Database theft** (Neon breach, leaked `DATABASE_URL`) | The feed URL and tokens are **encrypted with Fernet** (AES + HMAC) using a key that is **not** in the database. Each value is sealed with its row's context (`user:<id>:<name>`), so a ciphertext can't be moved to another user's row. Queries are built with SQLAlchemy, never string-built. TLS to Neon. | Mitigated, see R9 |
| W10 | **Sensitive data in logs** | The feed URL travels in a POST body, never a URL. Errors report only types or statuses. The local dev server's request logs have query strings redacted. | Partly, see R11 |
| W11 | **Data retention / user control** | **Delete my account** removes every row (cascade) and revokes the Google token. A public `/privacy` page includes the Google API Limited Use statement. | Mitigated |
| W12 | **Duplicate or corrupted sync** (cron, button and overlapping runs) | A per-user lock taken with **one atomic conditional upsert**, which works across processes and expires in case of crashes. Sync records are saved after every change. UID tags allow re-linking. CLI sync records were imported before the first cloud sync. | Mitigated |
| W13 | **Misconfigured deploy loses data** | The app refuses to start on Vercel without a Postgres `DATABASE_URL`. Neon is connected to Production only, with no per-deploy database branches. | Mitigated |
| W14 | **Secrets shipped in the deployment** | Secrets were never committed (history scans are clean). `.vercelignore` additionally blocks `.env*`, credentials, tokens, state and databases. Verified live: `/.env`, `/.env.production`, `/credentials.json`, `/token.json` and source files all return 404. | Mitigated |
| W15 | **Abuse and resource exhaustion** (sign-in floods, sync spam, refresh-spam hitting Canvas, cron-secret guessing) | Database-backed fixed-window **rate limits** (one atomic upsert, works across serverless instances): sign-in 10/10 min per IP, Sync/Preview 6/10 min per user, dashboard Canvas fetches 30/10 min per user, and a lockout after 5 wrong cron secrets per IP (further attempts aren't even checked). `429` with `Retry-After`. Counters pruned daily. | Mitigated |
| W16 | **Unbounded growth / burning Google's 100-user cap** | `MAX_USERS` (default 90): new accounts are refused, and their grant revoked, once reached; returning users can always sign in. | Mitigated, see R1 |
| W17 | **Cross-user data access** (multi-tenancy bugs, IDOR) | Every route acts on the session's user; a test asserts **no route takes a user ID**. 12 isolation tests with two users and separate fake Google accounts cover reading, syncing (manual and scheduled), locks, settings, deletion, forged session cookies, and ciphertexts copied between rows. Planted cross-user bugs are caught. | Mitigated |
| W18 | **Over-retention of personal data** | Sync history kept to the **20 most recent runs** per user. IP addresses never stored, only an HMAC with the app's secret key, pruned after about a day. Public privacy policy and terms. | Mitigated |
| W19 | **Encryption key compromise or loss** | **Key rotation:** `APP_ENCRYPTION_KEY=NEW,OLD` (MultiFernet). New data uses the newest key; the daily sync and `flask rotate-keys` re-encrypt the rest while keeping each value's row binding; then the old key is removed. Unreadable values are counted, never modified. | Mitigated, see R8 |
| W20 | **Supply-chain / CI compromise** | CI runs tests, bandit and pip-audit on every push (Python 3.12, as in production) with **read-only** token permissions, actions **pinned to commit SHAs**, and no persisted checkout credentials. **Dependabot** alerts, security updates, and weekly version updates are on. | Mitigated, see R7 |

### Core and command-line tool (carried over from Milestone 6)

| ID | Threat | Mitigations | Status |
|---|---|---|---|
| T1 | **Secrets committed to git** | `.gitignore` was in place before the first commit and covers `.env`, `.env.*`, credentials, tokens, state and databases. Every commit gets a staged-file scan for real token *shapes*, with a **self-test against a known fake**. The full history was scanned before every push. | Mitigated |
| T2 | **Secrets in terminal output or tracebacks** | The feed URL is never printed. Network errors are re-raised as types only (`from None`). Tokens are never printed. | Mitigated |
| T3 | **Local secret files read by other users** | Windows ACLs limited to the user, SYSTEM and Administrators. `0600` on POSIX. Atomic writes. The project is kept **outside OneDrive**. | Mitigated, see R2 |
| T5 | **Over-privileged Google access** | `calendar.app.created` (non-sensitive: only calendars this app made; verified with a 403 on others) and `tasks`. Partial consent is refused. | Mitigated, see R3 |
| T6 | **Malicious feed content** | HTML parsed, never rendered. Script/style dropped. **Control characters stripped** (terminal escape injection). 10 MB cap. Length caps. | Mitigated |
| T7 | **Network attacker** | HTTPS only. Certificate verification. PKCE. Loopback redirect for the CLI (RFC 8252). | Mitigated |
| T8 | **Hangs** | Timeouts on every call: feed 5/30 s, Google API 30 s, token exchange 30 s, refresh 120 s, database connect 10 s. | Mitigated |
| T9 | **Vulnerable dependencies** | All runtime dependencies pinned. `pip-audit`: no known vulnerabilities. | Mitigated, see R7 |
| T10 | **Insecure code patterns** | `bandit` (including `scripts/`): 0 issues. Three justified suppressions: `# nosec B105` on Google's public token URL (mistaken for a password) and two `# nosec B106` on fake values in the demo script. | Mitigated |
| T12 | **Wrong-type sync** | Strict UID classification. Unknown items are reported, never synced. | Mitigated |

## 4. Remaining (accepted) risks

| ID | Risk | Why it's accepted / future work |
|---|---|---|
| R1 | **The Google app is unverified.** Users see a warning screen; there's a 100-user lifetime cap. | The verification pack is ready (`docs/google-verification.md`): scope justification, data-handling answers, and a demo video script. **Pending:** the developer records the video and submits. Until approved, `MAX_USERS` keeps sign-ups under the cap. |
| R2 | **CLI secrets are plaintext on the local disk.** | Personal tool; file permissions are the control. Future: OS keyring. |
| R3 | **The `tasks` scope covers all task lists**, not just "Canvas". | No narrower Google scope exists. The code only touches "Canvas"; a stolen token wouldn't be limited that way. |
| R4 | **Losing the sync records creates a second "Canvas" calendar.** | `calendar.app.created` can't list calendars, so the ID exists only in the sync records. Events and tasks are recoverable through tags. Neon offers point-in-time restore within its plan's (short, on the free tier) history window. |
| R5 | **A leaked feed URL can't be revoked by the app.** | Kept out of git, logs and URLs. Ask the school's Canvas admin to reset the feed if it leaks. |
| R6 | **Feed text is forwarded to Google descriptions**, which render limited HTML. | Google sanitizes it, and only the user sees it. |
| R7 | **Pinned versions don't pick up fixes automatically.** | **Reduced:** Dependabot opens update PRs weekly and security PRs as advisories appear, and CI runs `pip-audit` on every push. Each update still needs a human to merge it. |
| R8 | **If the encryption key leaks, stored secrets are exposed until rotated.** | **Reduced:** rotation now exists (W19). Rotate on any suspicion of exposure; Google tokens can also be revoked by users. Losing the key isn't catastrophic: users re-enter their feed and reconnect, and sync records aren't encrypted, so nothing is duplicated. |
| R9 | **Platform trust.** Vercel, Neon and GitHub staff or breaches. | Accepted for a free personal project. Encryption at rest limits what a database-only breach reveals. |
| R10 | **Rate limits are per IP and per user, with fixed windows.** A distributed attacker with many IPs can still send more sign-in attempts. | Accepted: sign-in itself is handled by Google, every request still needs a valid Google sign-in to do anything, and Vercel provides platform-level DDoS protection. |
| R11 | **Platform request logs may record the `/auth/callback?code=…` URL.** | The local filter doesn't control Vercel's own logs. The code is single-use, expires in minutes, and is useless without the client secret and the PKCE verifier. |
| R12 | **Admin account takeover** (GitHub, Vercel, Neon, Google). | **2-factor authentication is on for GitHub, Vercel and Neon** (2026-10-25). **Google is still pending:** Google's risk checks blocked enabling 2-Step Verification on an account and device it doesn't yet trust; retry from a long-used device. Until then, Google relies on a long, unique password stored in a password manager, plus watching the account's security activity. Vercel's GitHub app is limited to this one repo. |
| R13 | **Daily sync only, best effort.** Vercel Hobby cron runs once a day within a one-hour window, and runs can occasionally be skipped or repeated. | The sync is idempotent and locked, so repeats are harmless. "Sync now" covers urgent changes. |
| R14 | **Production secrets on the developer's PC** (`.env.production`, git-ignored). | Needed for setup. **Action item:** move them to a password manager and delete the file. |

## 5. Findings and incidents during development

- **OAuth client secret exposed in screenshots (2026-10-23).** The client-secret dialog appeared in browser-automation screenshots. **Rotated the same day**; the old secret was deleted. Lesson: treat any secret that appears on screen during automation as exposed.
- **Missing timeout on the OAuth token exchange (Milestone 6).** `run_local_server(**kwargs)` sends extra arguments to the *authorization URL*, so `timeout=` there would have gone into the query string. Fixed by wrapping `fetch_token` instead, and verified with a mocked request.
- **A secret-scan pattern never matched (Milestone 7).** Windows PowerShell 5.1 stripped a double quote from the pattern. Caught by a known fake token. All scans now use quote-free real-token-shape patterns plus a self-test. Lesson: a scan that can't fail proves nothing.
- **OAuth code in local request logs (Milestone 8).** Fixed with a query-string redaction filter and a test.
- **Referrer-Policy would have broken every form in production (Milestone 10).** `no-referrer` conflicts with Flask-WTF's HTTPS same-site `Referer` check. Caught by reasoning *before* deployment; switched to `same-origin` (still never sent to other sites), and a test proves no-Referer and cross-site posts are refused while same-site posts succeed.
- **A stale environment variable after a redeploy (Milestone 10).** The live site kept the placeholder `OAUTH_REDIRECT_URI` after a dashboard "Redeploy". It was **caught by inspecting the live sign-in redirect before anyone signed in**, and fixed with a fresh production deploy. Lesson: verify configuration from the outside, not just in the dashboard.
- **A flaky test (Milestone 11).** A scheduler test failed about 1 run in 10: it read a sync's result after the run had *started* but before it *finished*. It was a test bug, not an app bug (the same race showed up once in a manual live check). Fixed by waiting for completion; then 0 failures in 40 runs.
- **Mutation checks that silently tested nothing (Milestone 12).** Two attempts to plant bugs for checking the isolation tests broke the module's syntax, so pytest failed to *collect*, and the report showed "0 failures". Caught by checking the number of tests that ran. Lesson: a passing result only counts if the expected tests actually ran.
- **Unescaped iCal text (Milestone 4)** and **time-zone handling (Milestones 5 and 9)** were caught by previews and tests before reaching Google.

## 6. How to re-verify

```bash
pip install -r requirements-dev.txt
pytest --cov=canvas_sync --cov=webapp      # 191 tests; network is blocked inside tests
pip-audit -r requirements.txt
bandit -r canvas_sync webapp cli.py app.py scripts
git status --ignored                       # .env, .env.production, credentials.json, token.json, instance/ must be ignored (!!)
```

Live checks:
- `GET /tasks/sync-all` without the secret returns **401**.
- `/.env` and `/credentials.json` return **404**.
- The response headers include the CSP, HSTS, `X-Frame-Options: DENY` and `Referrer-Policy: same-origin`.
- The `/auth/google` redirect has `redirect_uri=https://canvas2google.vercel.app/auth/callback`, `code_challenge_method=S256` and a `state`.

## 7. Public-launch checklist (Milestone 12)

| Item | Status |
|---|---|
| Multi-user isolation tests | Done (W17) |
| Revoke grants of refused sign-ins; history and IP minimization | Done (W1, W18) |
| Rate limits, user cap, invite-only/open sign-up mode | Done (W15, W16) |
| Encryption key rotation | Done (W19) |
| CI with pinned actions; Dependabot | Done (W20) |
| Landing page, privacy policy for multiple users, terms of service | Done |
| 2-factor authentication on admin accounts | GitHub, Vercel and Neon done; **Google pending** (R12) |
| Google OAuth verification | **Pack ready; submission pending** (R1) |
| Switch `SIGNUP_MODE` to `open` | **Developer's decision**, after verification is approved |
| Monitoring and alerts for failed syncs | Future work: failures are visible on each user's dashboard and as non-200 cron responses in Vercel logs, but nothing alerts the developer yet |
