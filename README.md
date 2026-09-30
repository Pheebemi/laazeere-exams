# Laazeere Academy Exam Portal

Standalone Django app where students take multiple-choice tests (CA1/CA2/CA3/Exam) and their scores flow automatically into the matching `Result` slot on [raddai-backend](https://laazeereacademy.pw), the same way a teacher's manual entry would. Built on the same stack proven by `jcda-election` at 200 concurrent users on free tiers: Vercel + Neon + WhiteNoise.

## Apps

- **`core`** — infra only (health check, DB-backed cache table for rate limiting across serverless invocations).
- **`roster`** — local mirror of raddai-backend's students/staff/classes/subjects/academic years, kept in sync via `sync_roster`. This is the *only* place student/staff accounts get created — there is no manual signup.
- **`exams`** — student-facing: login, take a test, submit. `exams/views.py::_finalize_submission` is the load-bearing concurrency-safety code (an atomic compare-and-swap that makes double-submits — refresh, double-click, a second tab, the deadline auto-submit, and the `auto_submit_expired` command — all safe to race against each other).
- **`dashboard`** — staff-facing: create/edit exams and questions, publish, monitor submissions, push graded results to raddai-backend.

## Local setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # leave DATABASE_URL blank for local SQLite
npm install && npm run build-css   # only needed after changing templates/styles; compiled CSS is committed
python manage.py migrate
python manage.py test
python manage.py createsuperuser
python manage.py runserver
```

## Syncing roster data

Requires `EXAM_PORTAL_API_KEY` (shared with raddai-backend) and `RADDAI_API_BASE_URL` set in `.env`.

```bash
python manage.py sync_roster                    # dry run — prints the diff, writes nothing
python manage.py sync_roster --apply            # actually writes it
python manage.py sync_roster --academic-year 13 --apply   # scope to one raddai academic year id
```

**On Vercel there's no shell**, so the sync runs two other ways: management clicks **Sync from main portal** in the dashboard sidebar (starter passwords use a fast hash, so a whole-school sync fits in one request), and every deploy runs `sync_roster --apply` as the last build step (see `vercel.json`). To pull new students/staff from the main portal, management clicks **Sync from main portal** in the dashboard sidebar (or you click **Redeploy** on the Vercel project). If the sync fails (e.g. raddai-backend is down or the key is wrong), the build logs it and still deploys with the roster it already had.

## Who can do what

Everyone's access comes from the roster sync — nobody is added by hand:

- **Management** — the main portal's **Management/Admin** accounts, logging in with the same username (starter password = the username). They get the full dashboard sidebar: every exam, **publishing**, **pushing results**, the student and staff lists, and **Sync from main portal**. When someone stops being Management/Admin on the main portal, the next sync removes their access here.
- **Teachers** — every other staff member, logging in with their staff ID. They create exams and questions, see only their own exams, and can view their scripts' scores. They can't publish, can't push results, and can't change an exam once management has published it.
- **Students** — log in at `/login/` with their student ID. Staff log in at `/dashboard/login/`; the student pages deliberately don't link to it.

Every new account starts with its own ID/username as the password and is nudged to change it. Starter passwords use a deliberately fast hash (`core/hashers.py`) so a whole-school sync fits in one request; Django re-hashes to full strength on first login.

## Exam security

- **Access code**: every exam has a 6-digit code, shown to its teacher and to management on the exam's dashboard pages (with **New code** if it leaks). The invigilator writes it on the board; students must enter it to begin, and again to continue on another device or after signing in again. Wrong codes are limited to 10 a minute per student.
- **Shuffled order**: each student gets the questions, and each question's options, in their own order (seeded by their submission, so a refresh keeps it). Grading is by choice id, so order never affects marks.
- **One device at a time**: a student logging in on a second phone/computer signs out the first one ("opened on another device").
- **Scores hidden**: students only see "Submitted"; they get their result on the main portal when the school releases it.

## Creating an exam

Teachers or management log into `/dashboard/`, click **+ New exam**, pick subject/class/academic year/term and which `Result` slot it fills (CA1/CA2/CA3 out of 10, Exam out of 70), then add questions (2–6 options each, one marked correct). Management's **Publish** stays disabled until the question marks add up exactly to the slot's total and every question has a single correct answer. Once any student has started an exam, its questions, subject, class and slot are locked (so grading can't change under them); only the timing can still be edited.

A question can carry one optional **picture** (diagram, map, shape…). Big phone photos are shrunk in the browser before upload (Vercel refuses request bodies over ~4.5 MB), then the server re-encodes every picture as a WebP of at most 1600 px (typically 100–200 KB) so a whole class loads it quickly. Pictures live in Vercel Blob and students load them straight from its CDN. Replacing or removing a picture, or deleting its question or exam, deletes the old file from Blob.

Django admin (`/admin/`) still has the same models as a fallback, but it bypasses the publish checks and the lock — prefer the dashboard.

## Pushing results

After an exam closes, **management** opens **Push results** in the dashboard sidebar and clicks **Push** on an exam (or **Push all**) — pushes every submitted-but-not-yet-pushed submission's score into the matching `Result` slot for that student/subject/academic year/term, without touching the other three slots. Safe to click more than once (already-pushed submissions are skipped). Each click stops after ~40s to stay inside Vercel's 60s limit and says how many are left, so a big batch may need a second click.

**Clearing old answers**: saved answer choices are most of the database (~1M rows a term at full use). Once a term's results are on the main portal, management can open **Clear old answers**, pick the session and term, check the preview and clear them. Scores live on the submission, so every score, the push to the main portal and the exam locks are unaffected; only which option each student picked is removed. Still-open exams are always skipped, and big terms clear in ~40s batches (click again to continue).

Run `python manage.py auto_submit_expired` periodically (same scheduling options as `sync_roster`) to force-submit anyone who abandoned an exam past its deadline, so staff review isn't blocked waiting on stragglers.

## Deployment (Vercel + Neon)

Same pattern as jcda-election: `vercel.json` targets `laazeere_exams/wsgi.py` as a serverless function, running `collectstatic` and `migrate` on every build. Static files are served by WhiteNoise, no separate CDN step needed.

Set in the Vercel project:
- `DATABASE_URL` — Neon's **pooled** connection string (the `-pooler` hostname). SQLite does not work on Vercel's read-only filesystem, and there's no Django-side connection pooling here — each serverless invocation opens a fresh connection, so the pooled string matters.
- `RADDAI_API_BASE_URL` — `https://laazeereacademy.pw/api` (a trailing `/` is fine)
- `EXAM_PORTAL_API_KEY` — a long random shared secret, set to the **same value** as `EXAM_PORTAL_API_KEY` on raddai-backend.
- `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` — set to your production domain.
- `SESSION_COOKIE_SECURE=True`, `CSRF_COOKIE_SECURE=True`.
- `BLOB_READ_WRITE_TOKEN` — added automatically when a **Blob** store is connected to the project (Storage → Create → Blob). Question pictures are stored there; without it they'd go to the local disk, which is read-only on Vercel.
- Do **not** set `SECURE_SSL_REDIRECT=True` — Vercel already forces HTTPS, and Django here isn't configured to trust Vercel's forwarded-protocol header, so it would see every request as plain HTTP and redirect forever.

**Neon compute autoscaling**: when creating the Neon project, set the compute's autoscaling *max* to what your plan actually provides — don't leave it at a conservative default. This is a Neon console setting (Compute → autoscaling), not something in this repo.

## Load testing before real students use it

`scripts/load_test.py` plays N students against the live site: each logs in, opens the exam list, starts the exam and loads the questions, then **all N submit at the same instant** (the "timer ran out" rush — harder than a steady flow). It prints how many made it and how slow each step was.

It never touches real students. Before a run, throwaway accounts `LOADTEST-0001…` are put in a separate **LOAD TEST** class (roster ids from 9000000, so no sync ever overwrites them), with one published 10-question First CA exam for that class; they all share one test password. Afterwards all of it is deleted — never push that exam's results.

```bash
pip install requests
python scripts/load_test.py --exam <exam id> --password <test password> --students 100
python scripts/load_test.py --exam <exam id> --password <test password> --students 200 --first 101   # bigger run, fresh accounts
```

`--ramp 30` spreads logins over 30 seconds instead of all at once. Check afterwards that every test submission is `submitted` with exactly one answer per question, and watch Neon's compute/connection graphs during the run.

Logins are rate-limited **per account, not per IP**, because a whole computer lab shares one public IP (an IP limit locked out part of a class), and a failing counter lets the login through (`RATELIMIT_FAIL_OPEN`) rather than blocking a student mid-exam.

`scripts/concurrency_test.py` is the narrower check that one student double-submitting (refresh, double-click, two tabs) records exactly one set of answers.
