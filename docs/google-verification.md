# Google OAuth verification pack

Everything needed to request Google's **sensitive-scope verification** for Canvas2Google. That review removes the
"Google hasn't verified this app" warning and the 100-user cap.

Sources: Google's [sensitive scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification),
[verification requirements](https://support.google.com/cloud/answer/13464321?hl=en),
[demo video](https://support.google.com/cloud/answer/13804565?hl=en) and
[submitting your app](https://support.google.com/cloud/answer/13461325?hl=en) pages. Re-check them before submitting;
requirements change.

## 1. App details

| Item | Value |
|---|---|
| App name | Canvas2Google |
| Home page | https://canvas2google.vercel.app/login |
| Privacy policy | https://canvas2google.vercel.app/privacy |
| Terms of service | https://canvas2google.vercel.app/terms |
| Authorized domain | `canvas2google.vercel.app` (verified in Search Console) |
| OAuth clients | **Web application** (the website) and **Desktop app** (the open-source command-line tool) |

## 2. Scopes

| Scope | Google's classification | Needs justification? |
|---|---|---|
| `openid`, `.../auth/userinfo.email` | Non-sensitive | No |
| `.../auth/calendar.app.created` | Non-sensitive | No (still explained below) |
| `.../auth/tasks` | **Sensitive** | **Yes** |

No restricted scopes are used, so no third-party security assessment is required.

### Justification for `https://www.googleapis.com/auth/tasks` (paste into the form)

> Canvas2Google copies a student's upcoming Canvas LMS assignments into Google Tasks so they can track due dates
> where they already manage their to-dos. To do this, the app creates one task list named "Canvas" in the user's
> Google Tasks, creates a task in that list for each upcoming assignment (title, due date, and notes with the exact
> due time and a link back to Canvas), and updates those tasks when the assignment changes in Canvas. Apart from
> reading the names of the user's task lists to find its own "Canvas" list, it only works inside that list: it never
> reads, modifies or deletes tasks in the user's other lists.
>
> A narrower scope is not sufficient: `https://www.googleapis.com/auth/tasks.readonly` cannot create task lists
> or create or update tasks, which is the app's core function, and the Tasks API offers no scope limited to a
> single task list.

### Why `calendar.app.created` (for reference)

Events go into a dedicated "Canvas" calendar the app creates. `calendar.app.created` only allows access to
calendars the app created, so the app cannot see or change the user's other calendars. This was chosen over the
broader `calendar.events` and `calendar` scopes on purpose (least privilege).

## 3. Data handling answers

- **What's stored:** the Google account email and ID, an encrypted Canvas feed URL, encrypted Google OAuth
  tokens, sync records (which Canvas item maps to which Google task or event), the 20 most recent sync summaries,
  and time zone and auto-sync preferences.
- **How it's protected:** HTTPS only. Feed URLs and tokens are encrypted at rest (Fernet), with the key stored
  separately from the database. Each user can only access their own data, and this is covered by tests.
- **Sharing:** none. Data is used only to provide the sync. It is never sold, never used for advertising, never
  shared with third parties for their own purposes, and never used to train AI models. The service providers
  (Vercel hosting, Neon database) process data only to run the service.
- **Retention and deletion:** sync history is capped at 20 runs. Users can delete their account at any time
  (Settings → Delete my account), which removes all their data and revokes the app's Google access.
- **Limited Use:** the privacy policy states compliance with the Google API Services User Data Policy, including
  the Limited Use requirements.

## 4. Demo video script (about 4–5 minutes, English, uploaded as **Unlisted** on YouTube)

**Before recording**
- Remove the app's existing access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions)
  (Canvas2Google), so the **full consent screen** appears again. Afterwards, use **Reconnect Google** on the site.
- For the command-line part, temporarily rename `token.json` so the CLI has to sign in again.
- Use a screen recorder: Windows **Snipping Tool → Record**, or **Win+Alt+R** (Xbox Game Bar), or OBS.
- Make the browser window large, and **show the address bar** throughout.
- The Canvas feed field hides what you paste, but avoid showing anything else private. Your course names appearing
  in Google Tasks and Calendar is fine (the video is unlisted), but you can crop if you prefer.

### Part 1: the website (Web application client)

| # | Show | Say (or caption) |
|---|---|---|
| 1 | `https://canvas2google.vercel.app` landing page; scroll to "What it asks Google for, and why". | "This is Canvas2Google. It copies Canvas assignments into Google Tasks and Canvas events into a Google Calendar." |
| 2 | Click **Sign in with Google** → choose the account. | "Signing in with Google." |
| 3 | **The consent screen.** Pause. Zoom into the **address bar** to show `client_id=…apps.googleusercontent.com`, then show the app name "Canvas2Google" and the list of permissions. | "The consent screen shows the app name, the web client ID in the URL, and the requested permissions: email, calendars this app creates, and Google Tasks." |
| 4 | Click **Allow** → land on the dashboard. | |
| 5 | **Settings**: paste the Canvas feed link (it's hidden), click **Save feed URL**. | "The user adds their Canvas calendar feed." |
| 6 | **Dashboard → Preview changes**, then **Sync now**. | "Preview shows what will change; Sync now applies it." |
| 7 | Open **Google Tasks** (in Gmail or Calendar's Tasks panel): show the **"Canvas"** list and its tasks; open one to show the due date and the notes with the exact due time and Canvas link. | "**Tasks scope**: the app created the 'Canvas' task list and a task per assignment. It only works in this list." |
| 8 | Open **Google Calendar**: show the **"Canvas"** calendar and an event in it. | "Calendar access is limited to the calendar the app created." |
| 9 | Back in Canvas2Google: **Settings → Delete my account** section (no need to click). Show the **Privacy policy** link. | "Users can delete their data and revoke access at any time." |

### Part 2: the command-line tool (Desktop app client)

| # | Show | Say (or caption) |
|---|---|---|
| 10 | Terminal in the project folder: `python cli.py --check-google`. The browser opens. | "The open-source command-line tool uses the Desktop OAuth client." |
| 11 | **The consent screen.** Zoom into the **address bar** to show the *desktop* `client_id`, and show the permissions. Click **Allow**. | "Same app name and permissions, with the desktop client ID." |
| 12 | Back in the terminal: it lists the task lists. Run `python cli.py --dry-run`. | "**Tasks scope**: the tool reads and updates its 'Canvas' task list." |

After recording, rename `token.json` back if needed (or keep the new one), and use **Settings → Reconnect Google** on
the website.

## 5. Submitting

1. **Google Cloud Console → Google Auth Platform → Branding**: set **Application terms of service link** to
   `https://canvas2google.vercel.app/terms` and save. Reload to confirm it stuck.
2. Upload the video to YouTube as **Unlisted** and copy its link.
3. **Google Auth Platform → Verification Center → Prepare for verification** (wording may vary). Confirm the
   branding details, paste the **tasks justification** from §2, and paste the **video link**.
4. Submit. Watch the developer contact email for questions from Google's review team, and reply in the same thread.
5. Expect up to about 10 days. The app keeps working (unverified) while it waits.
