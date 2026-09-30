"""
Load test for the live exam portal: N students log in, open the exam, and
then ALL submit at the same moment — the worst case on a real exam day
(everyone hitting "Submit" as the timer runs out).

It uses throwaway LOADTEST-#### student accounts in a "LOAD TEST" class,
never real students. Those accounts and the test exam are created before the
run and deleted after it (see README → Load testing).

Needs Python 3 and requests:  pip install requests

Usage:
  python load_test.py --exam 12 --code 482731 --password THE-TEST-PASSWORD
  python load_test.py --exam 12 --code 482731 --password THE-TEST-PASSWORD --students 200 --ramp 30
  python load_test.py --exam 12 --code 482731 --password THE-TEST-PASSWORD --students 100 --first 201   # re-run on fresh accounts

--ramp spreads the logins over that many seconds (students arriving in the
room); 0 means everyone logs in at the same instant. Submits always fire
together, after every student has the exam open.
"""

import argparse
import random
import re
import statistics
import threading
import time
from collections import defaultdict

import requests

STEPS = ["open login page", "log in", "exam list", "start exam", "open questions", "submit"]


def main():
    parser = argparse.ArgumentParser(description="Laazeere exam portal load test")
    parser.add_argument("--url", default="https://laazeere-exams.vercel.app")
    parser.add_argument("--exam", type=int, required=True, help="id of the LOAD TEST exam")
    parser.add_argument("--password", required=True, help="password of the LOADTEST accounts")
    parser.add_argument("--code", required=True, help="the exam's access code (shown on its dashboard page)")
    parser.add_argument("--students", type=int, default=100)
    parser.add_argument("--first", type=int, default=1, help="first account number (use fresh accounts for a re-run)")
    parser.add_argument("--prefix", default="LOADTEST-")
    parser.add_argument("--ramp", type=float, default=0, help="seconds to spread logins over")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    timings = defaultdict(list)  # step -> [seconds]
    failures = defaultdict(list)  # step -> ["LOADTEST-0001: HTTP 500", ...]
    finished = []
    lock = threading.Lock()
    # Everyone waits here with the exam open, then all submit at once.
    ready_to_submit = threading.Barrier(args.students, timeout=600)

    def record(step, started, ok, who, detail=""):
        with lock:
            timings[step].append(time.perf_counter() - started)
            if not ok:
                failures[step].append(f"{who}: {detail}")
        return ok

    def student(number):
        who = f"{args.prefix}{number:04d}"
        time.sleep(random.uniform(0, args.ramp))
        session = requests.Session()
        session.headers["Referer"] = base + "/"  # Django's CSRF check wants a same-site Referer over HTTPS
        answers = None
        try:
            answers = _up_to_exam_open(session, base, args, who, record)
        except requests.RequestException as exc:
            record("log in", time.perf_counter(), False, who, f"network error: {exc}")

        try:
            ready_to_submit.wait()
        except threading.BrokenBarrierError:
            pass
        if answers is None:
            return

        started = time.perf_counter()
        try:
            response = session.post(
                f"{base}/{args.exam}/submit/",
                data={"csrfmiddlewaretoken": session.cookies.get("csrftoken", ""), **answers},
                timeout=90,
            )
            ok = response.status_code == 200 and "/already-submitted/" in response.url
            if record("submit", started, ok, who, f"HTTP {response.status_code} at {response.url}"):
                with lock:
                    finished.append(who)
        except requests.RequestException as exc:
            record("submit", started, False, who, f"network error: {exc}")

    print(f"Load testing {base} with {args.students} students (ramp {args.ramp:g}s)…")
    run_started = time.perf_counter()
    numbers = range(args.first, args.first + args.students)
    threads = [threading.Thread(target=student, args=(n,)) for n in numbers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = time.perf_counter() - run_started

    print()
    print(f"{'step':<16}{'ok':>6}{'failed':>8}{'median':>10}{'95%':>10}{'slowest':>10}")
    for step in STEPS:
        times = timings.get(step, [])
        if not times:
            continue
        failed = len(failures.get(step, []))
        ordered = sorted(times)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(f"{step:<16}{len(times) - failed:>6}{failed:>8}{statistics.median(times):>9.2f}s{p95:>9.2f}s{ordered[-1]:>9.2f}s")

    print()
    print(f"{len(finished)}/{args.students} students submitted successfully in {total:.1f}s total.")
    for step in STEPS:
        for line in failures.get(step, [])[:5]:
            print(f"  FAILED {step}: {line}")
    print("RESULT:", "PASS ✅" if len(finished) == args.students else "SOME STUDENTS FAILED ❌")


def _up_to_exam_open(session, base, args, who, record):
    """Log in, start the exam and load its questions. Returns the answers to submit, or None on failure."""
    started = time.perf_counter()
    response = session.get(f"{base}/login/", timeout=60)
    if not record("open login page", started, response.status_code == 200, who, f"HTTP {response.status_code}"):
        return None

    started = time.perf_counter()
    response = session.post(
        f"{base}/login/",
        data={"username": who, "password": args.password, "csrfmiddlewaretoken": session.cookies.get("csrftoken", "")},
        timeout=60,
    )
    logged_in = response.status_code == 200 and response.url.rstrip("/") == base
    if not record("log in", started, logged_in, who, f"HTTP {response.status_code} at {response.url}"):
        return None

    started = time.perf_counter()
    response = session.get(f"{base}/", timeout=60)
    sees_exam = response.status_code == 200 and f"/{args.exam}/" in response.text
    if not record("exam list", started, sees_exam, who, f"HTTP {response.status_code}, exam listed: {sees_exam}"):
        return None

    started = time.perf_counter()
    response = session.post(
        f"{base}/{args.exam}/start/",
        data={"csrfmiddlewaretoken": session.cookies.get("csrftoken", ""), "access_code": args.code},
        timeout=60,
    )
    on_questions = response.status_code == 200 and f"/{args.exam}/take/" in response.url
    if not record("start exam", started, on_questions, who, f"HTTP {response.status_code} at {response.url}"):
        return None

    started = time.perf_counter()
    response = session.get(f"{base}/{args.exam}/take/", timeout=60)
    choices = defaultdict(list)
    for question, choice in re.findall(r'name="(question_\d+)" value="(\d+)"', response.text):
        choices[question].append(choice)
    if not record("open questions", started, response.status_code == 200 and choices, who, f"HTTP {response.status_code}"):
        return None
    return {question: random.choice(options) for question, options in choices.items()}


if __name__ == "__main__":
    main()
