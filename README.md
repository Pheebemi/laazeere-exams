# Laazeere Academy Exam Portal

Standalone Django app where students take multiple-choice tests (CA1/CA2/CA3/Exam) and their scores flow automatically into the matching `Result` slot on [raddai-backend](https://laazeereacademy.com), the same way a teacher's manual entry would. Built on the same stack proven by `jcda-election` at 200 concurrent users on free tiers: Vercel + Neon + WhiteNoise.

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

Run this on a schedule (Vercel Cron if the plan's limits allow it — verify current limits first, they've shifted over time — or a GitHub Actions scheduled workflow as a guaranteed fallback) or manually before a term's exams start.

## Creating an exam

Staff log into `/dashboard/`, click **+ New Exam**, pick subject/class/academic year/term and which `Result` slot it fills (CA1/CA2/CA3 out of 10, Exam out of 70), then add questions (2–6 options each, one marked correct). **Publish** stays disabled until the question marks add up exactly to the slot's total and every question has a single correct answer. Once any student has started an exam, its questions, subject, class and slot are locked (so grading can't change under them); only the timing can still be edited.

Django admin (`/admin/`) still has the same models as a fallback, but it bypasses the publish checks and the lock — prefer the dashboard.

## Pushing results

After an exam closes, a staff member logs into `/dashboard/`, opens the exam, and clicks "Push results to raddai-backend" — pushes every submitted-but-not-yet-pushed submission's score into the matching `Result` slot for that student/subject/academic year/term, without touching the other three slots. Safe to click more than once (already-pushed submissions are skipped).

Run `python manage.py auto_submit_expired` periodically (same scheduling options as `sync_roster`) to force-submit anyone who abandoned an exam past its deadline, so staff review isn't blocked waiting on stragglers.

## Deployment (Vercel + Neon)

Same pattern as jcda-election: `vercel.json` targets `laazeere_exams/wsgi.py` as a serverless function, running `collectstatic` and `migrate` on every build. Static files are served by WhiteNoise, no separate CDN step needed.

Set in the Vercel project:
- `DATABASE_URL` — Neon's **pooled** connection string (the `-pooler` hostname). SQLite does not work on Vercel's read-only filesystem, and there's no Django-side connection pooling here — each serverless invocation opens a fresh connection, so the pooled string matters.
- `RADDAI_API_BASE_URL` — `https://laazeereacademy.com/api`
- `EXAM_PORTAL_API_KEY` — a long random shared secret, set to the **same value** as `EXAM_PORTAL_API_KEY` on raddai-backend.
- `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` — set to your production domain.
- `SECURE_SSL_REDIRECT=True`, `SESSION_COOKIE_SECURE=True`, `CSRF_COOKIE_SECURE=True` once served over HTTPS.

**Neon compute autoscaling**: when creating the Neon project, set the compute's autoscaling *max* to what your plan actually provides — don't leave it at a conservative default. This is a Neon console setting (Compute → autoscaling), not something in this repo.

## Load testing before real students use it

This is the one thing that has to happen against the real deployment, not locally — reuse the same `load_test.py` approach already validated against jcda-election (200 concurrent, 400 requests, 100% success), but the case an exam presents is harder than voting: many students hit "submit" near the same shared deadline (a thundering herd), not a steadier window.

What to actually test, beyond a plain login/page-load smoke test:
1. Pre-create M `Submission` rows (`in_progress`) for one `Exam` — e.g. via the admin or a quick shell script hitting `/*/start/` as M different logged-in students.
2. Fire M concurrent `POST /<exam_id>/submit/` requests in a tight window — `scripts/concurrency_test.py` does exactly this for a single submission (already verified locally: 30 concurrent submits, exactly one Answer set recorded, zero 5xx); extend it to M distinct students/exams for the real thundering-herd case.
3. Assert afterward: `Submission.objects.filter(exam=exam, status='submitted').count() == M`, and each submission has exactly one `Answer` per question (no dupes, no gaps) — confirmed locally with 30 concurrent submits against a single submission (all handled cleanly, exactly one Answer set recorded, zero 5xx).
4. Watch Neon's connection-count metric live during the burst — Vercel can spin up many concurrent function invocations, each opening its own DB connection (no pooling on the Django side), so this is the number that actually tells you if the compute/pooler size is adequate, rather than guessing.

Run this at a concurrency target at least matching the largest class expected to sit an exam simultaneously, before real students rely on it.
