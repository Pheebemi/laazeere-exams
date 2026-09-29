"""
Fires N concurrent submit requests at the SAME in-progress submission and
verifies the compare-and-swap in exams/views.py::_finalize_submission holds:
exactly one Answer set gets written, no 5xx errors.

This is a correctness check for the double-submission race (refresh,
double-click, a second tab) — it is NOT the same as Phase 5's real
thundering-herd load test against a live deployment (many DIFFERENT
students submitting DIFFERENT exams near a shared deadline). Run that one
against the actual Vercel+Neon deployment, watching Neon's connection count,
before real students rely on this system.

Usage:
  python manage.py runserver 8001   # in one terminal
  python scripts/concurrency_test.py --base-url http://localhost:8001 \
      --exam-id 3 --username STU-TEST-001 --password STU-TEST-001 --concurrency 30
"""

import argparse
import concurrent.futures

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--exam-id", type=int, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--concurrency", type=int, default=30)
    args = parser.parse_args()

    session = requests.Session()
    session.get(f"{args.base_url}/login/")
    csrftoken = session.cookies["csrftoken"]
    session.post(
        f"{args.base_url}/login/",
        data={"username": args.username, "password": args.password, "csrfmiddlewaretoken": csrftoken},
    )

    session.get(f"{args.base_url}/{args.exam_id}/start/")
    session.get(f"{args.base_url}/{args.exam_id}/take/")
    csrftoken = session.cookies["csrftoken"]
    cookies = session.cookies.get_dict()

    def fire_submit(_):
        s = requests.Session()
        s.cookies.update(cookies)
        resp = s.post(
            f"{args.base_url}/{args.exam_id}/submit/",
            data={"csrfmiddlewaretoken": csrftoken},
            allow_redirects=False,
        )
        return resp.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        results = list(executor.map(fire_submit, range(args.concurrency)))

    failures = [r for r in results if r >= 400]
    print(f"{args.concurrency} concurrent submits -> status codes: {results}")
    print(f"non-2xx/3xx: {failures}")
    print(
        "Now check via `manage.py shell`: Submission.objects.get(exam_id="
        f"{args.exam_id}, student__student_id='{args.username}').status should be "
        "'submitted', and its answers.count() should equal the exam's question count "
        "exactly once — not zero, not duplicated."
    )


if __name__ == "__main__":
    main()
