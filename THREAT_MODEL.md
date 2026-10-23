# Threat Model: Canvas2Google

Canvas2Google is a personal command-line tool. It reads a Canvas LMS calendar feed (iCal) and syncs it to Google:

- calendar events go to a dedicated **"Canvas"** Google Calendar
- assignments go to a **"Canvas"** Google Tasks list

It runs locally on a single user's machine (Phase 1). There's no server, database or web front end.

*Last reviewed: 2026-10-23 (Milestone 6 security pass).*

---

## 1. Assets

| # | Asset | Where it lives | Why it matters |
|---|---|---|---|
| A1 | **Canvas feed URL** | `.env` (`CANVAS_FEED_URL`) | It works like a password. Anyone with the URL can read the user's whole Canvas calendar and assignment list without signing in. |
| A2 | **Google OAuth token** (access + refresh token) | `token.json` | Grants the app's scopes (see A5) until revoked: full read/write access to the user's Google Tasks, plus calendars the app created. |
| A3 | **OAuth client secret** | `credentials.json` | Identifies this app to Google. For "Desktop app" clients Google doesn't treat it as truly confidential, but it's still kept private. |
| A4 | **Sync state** | `sync_state.json` | Maps Canvas UIDs to Google IDs and holds the calendar and task list IDs. Low confidentiality, but its **integrity** prevents duplicates. |
| A5 | **The user's Google data** | Google Tasks / Calendar | What the token can touch, so the blast radius if A2 leaks. |
| A6 | **Code and dependencies** | Repo and `.venv` | A vulnerable or malicious dependency runs with access to A1–A3. |

## 2. Data flow and trust boundaries

```
 Canvas (HTTPS)                    this machine                         Google APIs (HTTPS)
 ┌──────────────┐  iCal feed  ┌────────────────────────────┐  OAuth-scoped  ┌──────────────────┐
 │ instructure  │ ──────────▶ │ cli.py → canvas_sync/      │ ─────────────▶ │ Calendar  Tasks  │
 │ .com         │  untrusted  │  feed → text → *_sync      │    requests    │                  │
 └──────────────┘    input    │ secrets: .env, token.json, │ ◀───────────── └──────────────────┘
                              │ credentials.json, state    │
                              └────────────────────────────┘
                                ▲ browser sign-in (one time): Google consent → http://localhost loopback
```

There are three trust boundaries:

1. **Canvas → app.** Feed content (titles, HTML descriptions) is **untrusted**. Anyone who can edit course content, such as instructors, TAs or tools integrated with Canvas, controls it.
2. **App → Google.** Requests are authenticated with A2 and limited by the OAuth scopes.
3. **Local disk.** Secrets sit at rest in the project folder.

## 3. Threats and mitigations

| ID | Threat | Mitigations | Status |
|---|---|---|---|
| T1 | **Secrets committed to git** and published | `.gitignore` covers `.env`, `credentials.json`, `token.json`, `sync_state.json`, `*.ics` and `*.tmp`, and was in place **before the first commit**. `.env.example` has only placeholders. Before each commit, the staged files are scanned for secret patterns: client secrets, tokens, the feed URL and calendar IDs. Full-history scan (all 5 commits): **clean**. | Mitigated |
| T2 | **Secrets leaked to terminal, logs or tracebacks** | The feed URL is never printed. `requests` errors (which contain the full URL) are replaced with the exception type only, raised with `from None`. OAuth failures report only the exception type. Google `HttpError`s report only the HTTP status. Tokens are never printed. Every `print` call was reviewed by hand. | Mitigated |
| T3 | **Another local user or process reads secret files** | Windows: the folder is under the user profile, and the ACLs on all four secret files grant access only to the user, SYSTEM and Administrators (verified with `Get-Acl`). Linux/macOS: secret files are created with mode `0600` via `os.open`, and existing ones are `chmod 600` on load. Writes are atomic (temp file plus `os.replace`). | Mitigated, see R2 |
| T4 | **Secrets exfiltrated by cloud file sync** | The project was deliberately placed **outside OneDrive** (`C:\Users\<user>\Projects`), so `.env` and the tokens aren't uploaded to a cloud drive. | Mitigated |
| T5 | **An over-privileged token increases the blast radius** | Least-privilege scopes: **`calendar.app.created`**, which Google classifies as *non-sensitive*: it can create calendars and manage events **only on calendars this app created**. Verified: listing the user's other calendars returns HTTP 403. **`tasks`** is used only for Tasks; Google offers no narrower scope that can create lists and tasks. Partial consent (one scope unticked) is detected and refused. Access can be revoked at myaccount.google.com → Security → Third-party connections. | Mitigated, see R3 |
| T6 | **Malicious feed content** (HTML/script injection, terminal escape injection, oversized input) | HTML is **parsed, never rendered or executed** (stdlib `HTMLParser`), and `<script>`/`<style>` contents are dropped. **Control characters are stripped** from all feed text before printing, preventing terminal escape-sequence injection. iCal escaping is handled explicitly. Feed size capped at **10 MB**. Notes/descriptions capped at 8,000 characters. Parse failures produce a clean error. | Mitigated, see R6 |
| T7 | **Network attacker (MITM)** | The feed must be `https://` (`webcal://` is rewritten to HTTPS), and redirects to non-HTTPS are refused. Google APIs are HTTPS with default certificate verification. The OAuth redirect uses the `http://localhost` loopback, as recommended for native apps (RFC 8252), and **PKCE** protects the authorization code from interception by other local processes. | Mitigated |
| T8 | **Hangs and resource exhaustion** | Timeouts on every network call: feed 5 s connect / 30 s read; Google API requests 30 s; token refresh 120 s (library default); **sign-in token exchange 30 s** (added in this review, see §5); sign-in wait 5 minutes. | Mitigated |
| T9 | **Vulnerable dependencies** | All runtime dependencies (direct and transitive) are **pinned** in `requirements.txt`. Dev tools are pinned separately in `requirements-dev.txt`. `pip-audit`: **no known vulnerabilities** in app requirements, dev requirements, or the whole virtual environment. | Mitigated, see R7 |
| T10 | **Insecure code patterns** | `bandit -r canvas_sync cli.py`: **0 issues, 0 `# nosec` suppressions**. | Mitigated |
| T11 | **Sync integrity: duplicates or corrupted state** | The state file records each synced item. Each Google item is tagged with its Canvas UID (a hidden event property, or a notes line for tasks), so lost state entries are **re-linked, not duplicated**. State is saved after every change and written atomically. A corrupt state file stops the run instead of silently starting over. `--dry-run` previews changes using only reads. | Mitigated, see R4 |
| T12 | **Wrong-type sync** (an unknown Canvas item becomes a calendar event) | Classification is strict on the UID format (`event-assignment-…` / `event-calendar-event-…`). Anything else is "unrecognized": counted, reported and **not synced**. | Mitigated |

## 4. Remaining (accepted) risks

| ID | Risk | Why it's accepted / future work |
|---|---|---|
| R1 | **Testing-mode refresh tokens expire after 7 days.** | An availability issue, not a confidentiality one. It matters for unattended runs (Milestone 8). Publishing the app would remove the limit, but the sensitive `tasks` scope would then need Google verification. |
| R2 | **Secrets are plaintext at rest.** Malware running as the user can read them. | Out of scope for a personal tool; file permissions are the control. Future: store tokens in the OS keyring. |
| R3 | **The `tasks` scope covers all task lists**, not only "Canvas". | No narrower Google scope exists. The code only touches the "Canvas" list, but a stolen token wouldn't be limited that way. |
| R4 | **Losing `sync_state.json` creates a second "Canvas" calendar.** | A deliberate trade-off. `calendar.app.created` can't list calendars, so the calendar ID lives only in the state file. Events and tasks *are* recoverable through UID tags. Mitigation: back up the state file when moving the project. |
| R5 | **Leaked feed URL.** The app has no way to revoke it. | Kept out of git, logs and cloud sync (T1, T2, T4). If it ever leaks, ask the school's Canvas admin how to reset calendar feed access. |
| R6 | **Feed text is forwarded to Google.** Google Calendar descriptions render a subset of HTML, so Canvas text containing literal markup could be shown as formatting. | Google sanitizes event descriptions, and the content is visible only to the user. Low impact. |
| R7 | **Pinned versions don't pick up security fixes automatically.** | Re-run `pip-audit` periodically and before each release; bump pins when fixes land. |
| R8 | **Task due dates use the host's local time zone.** | Correctness, not security. The server must use the same time zone as the user (Milestone 8). Event times are sent in UTC and are unaffected. |

## 5. Findings and incidents during development

- **OAuth client secret exposed in screenshots (setup, 2026-10-23).** While the OAuth client was being created in a browser automation pane, the client-secret dialog was captured in session screenshots stored locally. **Response:** the secret was **rotated** the same day (new secret created, old one disabled and deleted in Google Cloud Console), and only the new secret was saved to `credentials.json`. Lesson: assume any secret that appears on screen during automation is exposed, and rotate it.
- **Missing timeout on the OAuth token exchange (this review).** `google-auth-oauthlib`'s code-for-token request had no timeout, and `run_local_server(**kwargs)` sends extra arguments to the *authorization URL*, not the token request. Passing `timeout=` there would have put `timeout=30` into the URL as a query parameter instead of setting a timeout. **Fix:** wrap `oauth2session.fetch_token` with `functools.partial(..., timeout=30)`. Verified with a mocked request (timeout = 30, sign-in URL unchanged).
- **A secret-scan pattern silently never matched (found in Milestone 7).** The pre-commit scan's refresh-token pattern contained a double quote (`refresh_token":`). Windows PowerShell 5.1 strips embedded double quotes from arguments passed to native programs, so `git grep` searched for a different string and always reported "no matches". It was caught because a new test file contained a known fake token that *should* have matched. **Response:** switched to quote-free patterns and to scanning for real token shapes (`1//0…` refresh tokens, `ya29.…` access tokens, `GOCSPX-…` client secrets). Re-scanned **all history**: clean. Lesson: test your detection with a known positive. A scan that can't fail proves nothing.
- **Unescaped iCal text in descriptions (Milestone 4).** `icalendar` doesn't unescape non-standard properties like `X-ALT-DESC`, so `\,`, `\;` and `\n` leaked into task notes. The problem was caught in a preview **before** writing to Google, and is now handled explicitly.

## 6. How to re-verify

```bash
pip install -r requirements-dev.txt
pip-audit -r requirements.txt
bandit -r canvas_sync cli.py
git status --ignored   # .env, credentials.json, token.json, sync_state.json must show as ignored (!!)
python cli.py --dry-run
```

## 7. Phase 2 (shared website) — threats to revisit

A multi-user web version would change this model substantially:

- A **"Web application"** OAuth client with a confidential secret stored server-side.
- Per-user token storage, **encrypted at rest**.
- Google app verification for the `tasks` scope.
- Tenant isolation, so one user's feed URL or tokens can never reach another user.
- CSRF protection and `state` validation on the OAuth callback.
- Rate limiting.
