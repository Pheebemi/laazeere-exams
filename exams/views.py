import json
import random
import secrets

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .decorators import student_required
from roster.models import SyncedStudent

from .models import Answer, Exam, Submission
from .services import grade_submission

# An autosave only carries {question id: choice id} pairs; anything bigger is junk.
MAX_AUTOSAVE_BYTES = 20_000

# Exams this browser session has unlocked with the access code. Starting, and
# continuing on a new device or after signing in again, both need the code.
UNLOCKED_EXAMS_KEY = "unlocked_exams"


def _is_unlocked(request, exam):
    return exam.id in request.session.get(UNLOCKED_EXAMS_KEY, [])


def _unlock(request, exam):
    request.session[UNLOCKED_EXAMS_KEY] = [*request.session.get(UNLOCKED_EXAMS_KEY, []), exam.id]


def _deadline(submission):
    exam = submission.exam
    return min(exam.closes_at, submission.started_at + timezone.timedelta(minutes=exam.duration_minutes))


def _get_student_exam(exam_id, student, require_published=True):
    """
    Fetch the Exam for this student's class, scoped to their class. Not
    get_object_or_404(Exam, klass=...) — Django's get_object_or_404() itself
    takes a positional parameter literally named `klass` for the model, which
    collides with our Exam.klass field name if passed as a keyword.
    """
    qs = Exam.objects.filter(pk=exam_id, klass=student.current_class)
    if require_published:
        qs = qs.filter(is_published=True)
    exam = qs.first()
    if exam is None:
        raise Http404("Exam not found")
    return exam


# Limited per student ID, not per IP: a whole computer lab shares one public
# IP (and behind Vercel's proxy the IP may not even be the student's), so an
# IP limit would lock out most of a class logging in at the start of an exam.
@ratelimit(key="post:username", rate="10/m", method="POST", block=False)
def student_login(request):
    if getattr(request, "limited", False):
        messages.error(request, "Too many login attempts. Please wait a moment and try again.")
        return render(request, "exams/login.html")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None and hasattr(user, "syncedstudent"):
            login(request, user)
            # This session becomes the only one allowed; any other device is signed out (student_required).
            SyncedStudent.objects.filter(pk=user.syncedstudent.pk).update(
                current_session_key=request.session.session_key
            )
            return redirect("exams:exam_list")
        messages.error(request, "Invalid student ID or password.")

    return render(request, "exams/login.html")


@login_required(login_url="exams:student_login")
def student_logout(request):
    logout(request)
    return redirect("exams:student_login")


@student_required
def exam_list(request):
    student = request.user.syncedstudent
    now = timezone.now()
    exams = (
        Exam.objects.filter(klass=student.current_class, is_published=True)
        .select_related("subject")
        .order_by("opens_at")
    )

    existing = {
        s.exam_id: s for s in Submission.objects.filter(student=student, exam__in=exams)
    }

    rows = []
    for exam in exams:
        submission = existing.get(exam.id)
        if submission and submission.status == Submission.Status.SUBMITTED:
            state = "submitted"
        elif exam.is_open(now):
            state = "in_progress" if submission else "open"
        elif now < exam.opens_at:
            state = "upcoming"
        else:
            state = "closed"
        rows.append({"exam": exam, "submission": submission, "state": state})

    return render(request, "exams/exam_list.html", {"rows": rows, "student": student})


@student_required
@ratelimit(key="user", rate="10/m", method="POST", block=False)
def start_exam(request, exam_id):
    """GET shows what's about to be taken (so a student can't open the wrong CA by
    accident) and asks for the access code the invigilator gives out. The timer
    only starts on POST with the right code. A student already writing who comes
    back on a new device or after signing in again enters the code to continue."""
    student = request.user.syncedstudent
    exam = _get_student_exam(exam_id, student)

    existing = Submission.objects.filter(student=student, exam=exam).select_related("exam").first()
    if existing and existing.status == Submission.Status.SUBMITTED:
        return redirect("exams:already_submitted", exam_id=exam.id)
    if existing and timezone.now() >= _deadline(existing):
        _finalize_submission(existing, {})
        return redirect("exams:already_submitted", exam_id=exam.id)
    if existing and _is_unlocked(request, exam):
        return redirect("exams:take_exam", exam_id=exam.id)

    if not existing and not exam.is_open():
        messages.error(request, f"{exam.subject} {exam.get_score_target_display()} is not open right now.")
        return redirect("exams:exam_list")

    if not exam.access_code:
        # Management hasn't issued a code yet. Never compare against the empty
        # code — an empty box would match it.
        return render(request, "exams/start_exam.html", {
            "exam": exam,
            "student": student,
            "question_count": exam.questions.count(),
            "resuming": existing is not None,
            "waiting_for_code": True,
        })

    if request.method == "POST":
        code = request.POST.get("access_code", "").strip()
        if getattr(request, "limited", False):
            messages.error(request, "Too many wrong codes. Wait a minute, then try again.")
        elif secrets.compare_digest(code, exam.access_code):
            if not existing:
                Submission.objects.get_or_create(student=student, exam=exam)
            _unlock(request, exam)
            return redirect("exams:take_exam", exam_id=exam.id)
        else:
            messages.error(request, "That access code is not correct. Ask your teacher for the code.")

    return render(request, "exams/start_exam.html", {
        "exam": exam,
        "student": student,
        "question_count": exam.questions.count(),
        "resuming": existing is not None,
    })


@student_required
def take_exam(request, exam_id):
    student = request.user.syncedstudent
    exam = _get_student_exam(exam_id, student)
    submission = get_object_or_404(Submission, student=student, exam=exam)

    if submission.status == Submission.Status.SUBMITTED:
        return redirect("exams:already_submitted", exam_id=exam.id)

    deadline = _deadline(submission)
    if timezone.now() >= deadline:
        _finalize_submission(submission, request.POST)
        return redirect("exams:already_submitted", exam_id=exam.id)

    if not _is_unlocked(request, exam):
        messages.info(request, "Enter the access code to continue.")
        return redirect("exams:start_exam", exam_id=exam.id)

    # Every student gets the questions, and each question's options, in their
    # own order — seeded by their submission, so a refresh keeps the same order.
    # Grading is by choice id, so the order never affects marks.
    questions = list(exam.questions.prefetch_related("choices"))
    random.Random(submission.pk).shuffle(questions)
    saved = submission.draft_answers or {}
    for question in questions:
        question.shuffled_choices = list(question.choices.all())
        random.Random(f"{submission.pk}-{question.pk}").shuffle(question.shuffled_choices)
        # Autosaved pick, so a reload or coming back after logging out restores it.
        question.saved_choice_id = saved.get(str(question.pk))

    return render(
        request,
        "exams/take_exam.html",
        {"exam": exam, "questions": questions, "deadline": deadline, "draft_seq": submission.draft_seq},
    )


@student_required
@require_POST
def save_answers(request, exam_id):
    """
    Autosave. After every tap the page sends all currently picked options;
    this stores them with one small UPDATE (no grading, no answer rows). The
    sequence number makes a slow, older save that arrives after a newer one
    a no-op, so the newest picks always win.
    """
    student = request.user.syncedstudent
    exam = _get_student_exam(exam_id, student)
    if not _is_unlocked(request, exam):
        return JsonResponse({"saved": False, "reason": "Enter the access code again."}, status=403)
    if len(request.body) > MAX_AUTOSAVE_BYTES:
        return JsonResponse({"saved": False, "reason": "Too large."}, status=413)
    try:
        payload = json.loads(request.body)
        seq = int(payload["seq"])
        answers = {str(int(question)): int(choice) for question, choice in payload["answers"].items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        return JsonResponse({"saved": False, "reason": "Bad request."}, status=400)

    submission = Submission.objects.filter(student=student, exam=exam).select_related("exam").first()
    if submission is None or submission.status != Submission.Status.IN_PROGRESS:
        return JsonResponse({"saved": False, "reason": "Already submitted."}, status=409)
    if timezone.now() >= _deadline(submission):
        return JsonResponse({"saved": False, "reason": "Time is up."}, status=409)

    Submission.objects.filter(
        pk=submission.pk, status=Submission.Status.IN_PROGRESS, draft_seq__lt=seq
    ).update(draft_answers=answers, draft_seq=seq)
    return JsonResponse({"saved": True, "seq": seq})


@student_required
def submit_exam(request, exam_id):
    student = request.user.syncedstudent
    exam = _get_student_exam(exam_id, student)
    submission = get_object_or_404(Submission, student=student, exam=exam)

    if request.method != "POST":
        return redirect("exams:take_exam", exam_id=exam.id)
    if not _is_unlocked(request, exam):
        return redirect("exams:start_exam", exam_id=exam.id)

    _finalize_submission(submission, request.POST)
    return redirect("exams:already_submitted", exam_id=exam.id)


def _finalize_submission(submission, post_data):
    """
    The compare-and-swap that makes double-submission (refresh, double-click,
    a second tab, the deadline-triggered auto-submit in take_exam, and the
    auto_submit_expired command all racing each other) safe: only the
    request that actually flips in_progress -> submitted gets to write
    Answers and grade. Everyone else's write here is a silent no-op.
    """
    with transaction.atomic():
        updated = Submission.objects.filter(
            pk=submission.pk, status=Submission.Status.IN_PROGRESS
        ).update(status=Submission.Status.SUBMITTED, submitted_at=timezone.now())

        if not updated:
            return  # lost the race — someone/something else already submitted this one

        # The submitted form wins; anything it lacks (time ran out offline, a
        # reload after the deadline, the auto_submit_expired command) comes from
        # the latest autosave, so a student never loses what was saved.
        draft = Submission.objects.filter(pk=submission.pk).values_list("draft_answers", flat=True).first() or {}
        answers = []
        for question in submission.exam.questions.prefetch_related("choices"):
            raw = post_data.get(f"question_{question.id}") or draft.get(str(question.id))
            try:
                choice_id = int(raw)
            except (TypeError, ValueError):
                choice_id = None
            # Only an option of *this* question counts — never another question's choice id.
            if choice_id not in {choice.id for choice in question.choices.all()}:
                choice_id = None
            answers.append(Answer(submission=submission, question=question, selected_choice_id=choice_id))
        Answer.objects.bulk_create(answers)

    grade_submission(submission)


@student_required
def already_submitted(request, exam_id):
    student = request.user.syncedstudent
    exam = _get_student_exam(exam_id, student, require_published=False)
    submission = get_object_or_404(Submission, student=student, exam=exam)
    return render(request, "exams/already_submitted.html", {"exam": exam, "submission": submission})
