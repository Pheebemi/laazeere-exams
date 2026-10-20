# laazeere-exams (exam / CBT portal)

Django portal on Vercel (exams.laazeereacademy.com) with Neon Postgres. Students write CA and exams here; scores are pushed to the main portal (raddai-backend).

## Deploy
- Merge to `main`. The Vercel build runs `collectstatic && migrate && sync_roster --apply`. No cPanel step.
- Project: `prj_leYmcl8j4SDYpstgT766hPFNFLbm`, Neon project `orange-brook-96783752`.
- CSS is compiled and committed: after template changes run `npm run build-css` and commit `static/css/styles.css`. Do not commit `package-lock.json` changes.

## Run and test locally
- `.venv/bin/python manage.py test` is slow (about 7 minutes). Run a single class while working, the full suite before reporting.
- Run `collectstatic --noinput` once in a fresh clone. Local runs use SQLite when `DATABASE_URL` is unset. Never point tests at Neon.

## Things to know
- Management (not teachers) can review a student's answers: `dashboard:submission_answers`. "Clear old answers" deletes the saved answers it reads.
- Roster comes from the main portal via `sync_roster`. Deleted-and-re-added students are relinked there.
- Exam deadline is server-side; expired attempts are finalised by `finalize_expired_submissions`.

## How to work with me
- Short, plain answers. No long recaps.
- Test before reporting (run the tests; for UI changes check it in a browser). Say plainly if something failed or was skipped.
- Never merge until I say "merge". Then create the PR, merge it, check the deploy is READY and report in a line or two.
- Say whether a change is frontend-only, needs a migration, or needs a cPanel pull + Restart.
- Do not touch the other portal when a change is for one portal only.
- Never decrypt Vercel env values. No destructive Neon/database actions without asking first.
- Commit trailer: `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Keep token use low: read only the files needed, no huge outputs, screenshots only for UI changes.

