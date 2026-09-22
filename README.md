# Canvas Assistant

A private web app that watches Canvas for you and submits your finished work.

It does two things:

1. Checks Canvas every 30 minutes. Texts your phone when a new assignment appears or a grade lands.
2. Gives you a web page to upload finished work. The app submits it to Canvas so you never log in.

It runs on Railway for a few dollars a month. Only you have the password.

Follow the steps in order. Nothing here needs coding experience. Budget about 30 minutes.

---

## What you need before you start

- A GitHub account, with this repository pushed to it.
- A Railway account. Sign up at [railway.app](https://railway.app) with your GitHub login.
- An Anthropic account with API credit, from [console.anthropic.com](https://console.anthropic.com). Only needed for the AI deck feature.
- Your phone.

---

## Step 1: Get your Canvas access token

This token lets the app read your courses and submit on your behalf. Treat it like your password.

1. Log in to Canvas in a browser.
2. Click Account in the far left sidebar, then Settings.
3. Scroll to Approved Integrations.
4. Click the "+ New Access Token" button.
5. For Purpose, type `Canvas Assistant`.
6. Leave the expiry date blank so the token never expires. If your school forces a date, pick the furthest one allowed and set a calendar reminder to make a new one.
7. Click Generate Token.
8. Copy the long string it shows you. Paste it somewhere safe right now.

Canvas shows that token exactly once. If you close the box without copying it, delete the token and make another.

While you are here, note your Canvas web address. It looks like `https://yourschool.instructure.com`. You need it in Step 5.

### If your school blocks access tokens

Some schools turn this feature off. The New Access Token button will be missing. Ask your IT help desk for a Canvas API token for personal use. If they say no, this app cannot work for your account.

---

## Step 2: Set up ntfy on your phone

ntfy sends the alerts. It is free and needs no account.

1. Install the ntfy app: [iPhone](https://apps.apple.com/us/app/ntfy/id1625396347) or [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy).
2. Invent a topic name. This is the only thing keeping your alerts private, so make it long and random. Not `canvas`. Something like `canvas-alerts-7f3a91b2c4e8`.
3. Open the app, tap the + button, type your topic name exactly, and subscribe.
4. Write the topic name down. You need it in Step 5.

Anyone who guesses your topic name sees your alerts. Anyone who does not, cannot.

---

## Step 3: Create the Railway project

1. Go to [railway.app](https://railway.app) and log in.
2. Click New Project.
3. Choose Deploy from GitHub repo.
4. Grant Railway access to your GitHub account when asked.
5. Pick this repository from the list.

Railway starts building right away. The first build fails or the app starts unconfigured. That is expected. Finish Steps 4 and 5 and it comes up.

---

## Step 4: Add the volume

A volume is a disk that survives restarts. Without one, your database and uploaded files vanish every deploy.

1. In your project, click the service card (the box named after your repo).
2. Open the Settings tab.
3. Scroll to Volumes and click New Volume.
4. Set the mount path to exactly `/data`.
5. Save.

The mount path must be `/data`, with the slash, all lowercase. The app writes its database and your uploads there.

---

## Step 5: Set the environment variables

These are your settings and secrets. Railway keeps them out of your code.

1. Click your service, then the Variables tab.
2. Add each of the following. Use Raw Editor to paste them all at once if you prefer.

| Variable | What to put |
|---|---|
| `CANVAS_BASE_URL` | Your Canvas address, no trailing slash. Example: `https://yourschool.instructure.com` |
| `CANVAS_TOKEN` | The token from Step 1 |
| `APP_PASSWORD` | A long password you invent. This is what you type to get into the app. |
| `SECRET_KEY` | A long random string. Mash your keyboard for 40 characters. Never reuse your password here. |
| `NTFY_TOPIC` | The topic name from Step 2 |
| `ANTHROPIC_API_KEY` | Your key from [console.anthropic.com](https://console.anthropic.com), under API Keys. Starts with `sk-ant-`. |
| `DATA_DIR` | `/data` |
| `POLL_MINUTES` | `30` |
| `TIMEZONE` | Your timezone, like `America/New_York`. Canvas sends due dates in UTC, so without this an 11:59 PM deadline shows as 3:59 AM the next day. |

`.env.example` in this repository lists the same variables with notes. Two optional ones:

- `NTFY_SERVER` defaults to `https://ntfy.sh`. Leave it out unless you host your own ntfy.
- `MAX_SUBMIT_ATTEMPTS` defaults to `3`. The app stops retrying a submission after this many Canvas failures so a broken upload cannot alert you forever.

Railway redeploys automatically after you save. Give it a minute.

---

## Step 6: Open the app

1. Go to Settings, find Networking, and click Generate Domain.
2. Railway gives you a web address ending in `.up.railway.app`.
3. Open it on your phone. Log in with your `APP_PASSWORD`.
4. Add it to your home screen so it opens like an app. On iPhone: Share, then Add to Home Screen.

If the login page shows a yellow box listing missing variables, go back to Step 5 and add the ones it names.

The first check runs quietly. The app records everything already in Canvas without alerting you, so you do not get 40 notifications on day one. Real alerts start from the next check.

---

## Checking your setup before you trust it

`check_setup.py` verifies your tokens, your Canvas address, your courses, and your
notifications. It runs on your own machine. Your tokens go only to Canvas, ntfy, and
Anthropic, never anywhere else.

```bash
python3 check_setup.py
```

It reads `.env` if you have one, or the variables already in your shell. Every check
prints ok, warn, or FAIL, and each FAIL says what to fix.

To share the output with someone helping you, mask your course and account names:

```bash
python3 check_setup.py --redact
```

Everything above is read-only. To also test a real submission:

```bash
python3 check_setup.py --submit 101:555
```

That uploads a small text file to the assignment you name. It asks you to type the
assignment name first. Canvas cannot delete a submission once made and your instructor
sees the attempt, so pick an assignment where an extra attempt does not matter.

---

## Using it day to day

### Dashboard

Every course, every assignment, due dates, and whether each one is submitted or graded. Recent activity sits at the bottom so you can see what the app has been doing. "Check Canvas now" runs a check immediately instead of waiting for the next one.

### Upload page

Pick one of two modes at the top.

Manual. For work you already finished. Pick the assignment, choose your file, submit. It goes into the queue and uploads to Canvas within 30 minutes. Nothing reviews it. Use this when the work is done.

AI Approved. Pick the assignment, then pick how to work:

- "I have source material" takes a PDF, Word document, PowerPoint, or text file and turns it straight into a slide deck. One shot, no conversation.
- "Just a prompt" opens a conversation with Claude about that assignment. See below.

Either way the app holds the result. Nothing reaches Canvas until you approve it.

### Ask page

This is the conversation. Pick an assignment and Claude starts already knowing:

- The assignment brief, converted from Canvas HTML to plain text.
- The full grading rubric, every criterion and every rating band with its points.
- Any specification files the instructor attached to the description. Instructors often leave the brief nearly empty and put the real requirements in an attached PDF, so the app downloads those and reads them too.

Talk to it like you would in any chat. Ask what a rubric criterion is actually asking for, have it draft a section, or paste your own writing and ask where it loses points. The conversation is saved per assignment, so you can close the page and pick it up later.

When the work is ready, "Build it and send for review" turns the conversation into a file and puts it in the Review queue. Format is chosen for you from the assignment, and you can override it:

- Presentations become an accessible PowerPoint. Every slide has a real title, body text sits in proper placeholders so reading order works, and speaker notes are kept.
- Everything else becomes an accessible Word document. Real heading styles, real list styles, and the document language set.

Accessible here means a screen reader can navigate it. Text that is merely bold and large looks like a heading but carries no structure, so nothing in these files fakes formatting that way.

Generating takes up to a minute. Stay on the page.

You can also reach the chat for any assignment by tapping its name on the dashboard.

### Review page

Every AI deck waits here. Download it, open it, read it. Then:

- Approve puts it in the queue. It submits within 30 minutes.
- Reject deletes the deck and the file. Nothing was sent.

Nothing the AI writes reaches Canvas without you pressing Approve. That is the whole point of the two modes.

### Notifications

Four kinds reach your phone:

- New assignment, with the due date.
- Graded, with the grade.
- Submitted, when a file reaches Canvas.
- Submit failed, at urgent priority, with the reason Canvas gave.

A failure alert means the file is still sitting in the queue. Read the reason on the dashboard. Common causes: the due date passed, the assignment does not take file uploads, or your Canvas token expired.

---

## When something breaks

The dashboard activity list is the first place to look. Railway's Deployments tab has the full logs.

App will not start. Check the Variables tab against Step 5. A typo in `CANVAS_BASE_URL` is the usual cause. It needs `https://` at the front and no slash at the end.

No notifications arrive. Confirm the topic in the ntfy app matches `NTFY_TOPIC` exactly, character for character. Then press "Check Canvas now" on the dashboard.

Dashboard shows a Canvas error. Your token expired or was revoked. Make a new one from Step 1 and update `CANVAS_TOKEN`.

Submit keeps failing. Open the assignment in Canvas yourself. Check it still accepts submissions and takes file uploads. After 3 failures the app gives up on that file so it stops alerting you.

Deck generation fails. Check your Anthropic account has credit. Source files must be PDF, DOCX, PPTX, TXT, MD, or CSV, and PDFs must be under 25 MB.

Everything vanished after a deploy. The volume is missing or mounted somewhere other than `/data`. Go back to Step 4.

---

## Costs

Railway runs about 5 dollars a month for a project this small. The Anthropic API charges per deck, typically a few cents. Canvas and ntfy are free.

---

## Keeping it private

Your Railway URL is public. Your password is what protects it, so make it long.

Never commit your tokens. They belong in Railway's Variables tab, never in a file you push to GitHub. `.gitignore` already blocks `.env`.

If you think a secret leaked: delete the Canvas token in Canvas settings, roll your Anthropic key in the console, and change `APP_PASSWORD` and `SECRET_KEY` on Railway. Changing `SECRET_KEY` logs you out everywhere.

---

## How it fits together

| File | Job |
|---|---|
| `app/main.py` | The web pages and the login gate |
| `app/scheduler.py` | The background check that runs every `POLL_MINUTES` |
| `app/canvas.py` | Talks to Canvas, including the three step file submission |
| `app/db.py` | SQLite storage on the volume |
| `app/ai_generate.py` | Builds a deck from a source document |
| `app/assistant.py` | The per-assignment chat, and turning it into a file |
| `app/render.py` | Writes the accessible .docx and .pptx files |
| `app/extract.py` | Reads text out of PDF, Word, PowerPoint, and Canvas HTML |
| `app/notify.py` | Sends the pushes to ntfy |
| `Procfile` | Tells Railway how to start the app |

## Running it on your own computer

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in .env
export $(grep -v '^#' .env | xargs)
export DATA_DIR=./data
uvicorn app.main:app --reload --port 8000
```

Open http://127.0.0.1:8000.
