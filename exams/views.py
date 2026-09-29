from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django_ratelimit.decorators import ratelimit

from .decorators import student_required
from .models import Answer, Exam, Submission
from .services import grade_submission


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


@ratelimit(key="ip", rate="30/m", method="POST", block=False)
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
            if user.syncedstudent.must_change_password:
                messages.info(request, "Please change your password before continuing.")
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
def start_exam(request, exam_id):
    """GET shows what's about to be taken (so a student can't open the wrong CA by
    accident); the timer only starts on POST, when they confirm."""
    student = request.user.syncedstudent
    exam = _get_student_exam(exam_id, student)

    existing = Submission.objects.filter(student=student, exam=exam).first()
    if existing and existing.status == Submission.Status.SUBMITTED:
        return redirect("exams:already_submitted", exam_id=exam.id)
    if existing:
        return redirect("exams:take_exam", exam_id=exam.id)

    if not exam.is_open():
        messages.error(request, f"{exam.subject} {exam.get_score_target_display()} is not open right now.")
        return redirect("exams:exam_list")

    if request.method == "POST":
        Submission.objects.get_or_create(student=student, exam=exam)
        return redirect("exams:take_exam", exam_id=exam.id)

    return render(request, "exams/start_exam.html", {
        "exam": exam,
        "student": student,
        "question_count": exam.questions.count(),
    })


@student_required
def take_exam(request, exam_id):
    student = request.user.syncedstudent
    exam = _get_student_exam(exam_id, student)
    submission = get_object_or_404(Submission, student=student, exam=exam)

    if submission.status == Submission.Status.SUBMITTED:
        return redirect("exams:already_submitted", exam_id=exam.id)

    deadline = min(exam.closes_at, submission.started_at + timezone.timedelta(minutes=exam.duration_minutes))
    if timezone.now() >= deadline:
        _finalize_submission(submission, request.POST)
        return redirect("exams:already_submitted", exam_id=exam.id)

    questions = exam.questions.prefetch_related("choices")
    return render(
        request,
        "exams/take_exam.html",
        {"exam": exam, "questions": questions, "deadline": deadline},
    )


@student_required
def submit_exam(request, exam_id):
    student = request.user.syncedstudent
    exam = _get_student_exam(exam_id, student)
    submission = get_object_or_404(Submission, student=student, exam=exam)

    if request.method != "POST":
        return redirect("exams:take_exam", exam_id=exam.id)

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

        answers = []
        for question in submission.exam.questions.all():
            choice_id = post_data.get(f"question_{question.id}")
            answers.append(
                Answer(submission=submission, question=question, selected_choice_id=choice_id or None)
            )
        Answer.objects.bulk_create(answers)

    grade_submission(submission)


@student_required
def already_submitted(request, exam_id):
    student = request.user.syncedstudent
    exam = _get_student_exam(exam_id, student, require_published=False)
    submission = get_object_or_404(Submission, student=student, exam=exam)
    return render(request, "exams/already_submitted.html", {"exam": exam, "submission": submission})
