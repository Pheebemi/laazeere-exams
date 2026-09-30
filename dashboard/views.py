import re
import time
from io import StringIO

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.management import call_command
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Max, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from exams.models import Answer, Choice, Exam, Question, Submission
from exams.services import push_submission_to_raddai
from roster.models import SyncedAcademicYear, SyncedClass, SyncedManager, SyncedStaff, SyncedStudent

from .decorators import dashboard_required, is_manager, is_teacher, management_required
from .forms import ExamForm, QuestionForm

# Each push is one HTTP call to the main portal, so a big class can take a
# while. No new push starts after the budget, and each one waits at most
# PUSH_REQUEST_TIMEOUT_SECONDS, so a click always ends inside Vercel's 60s
# function limit; pushing is idempotent, so whatever is left goes out on the
# next click.
PUSH_TIME_BUDGET_SECONDS = 40
PUSH_REQUEST_TIMEOUT_SECONDS = 10

# Clearing a full term can mean a million answer rows; delete in batches and
# stop inside Vercel's 60s limit — the next click carries on where this ended.
CLEAR_TIME_BUDGET_SECONDS = 40
CLEAR_BATCH_SIZE = 10_000

EXAM_RELATED = ("subject", "klass", "created_by__syncedstaff", "created_by__syncedmanager")


def _render(request, template, section, context=None, status=200):
    """`section` tells the sidebar which item to highlight."""
    return render(request, template, {**(context or {}), "section": section}, status=status)


def _visible_exams(user):
    """Management sees every exam; a teacher sees only the ones they created."""
    if is_manager(user):
        return Exam.objects.all()
    return Exam.objects.filter(created_by=user)


def _get_exam(request, exam_id):
    try:
        return _visible_exams(request.user).select_related(*EXAM_RELATED).get(pk=exam_id)
    except Exam.DoesNotExist:
        raise Http404("Exam not found")


def _can_edit(user, exam):
    """Once management publishes an exam, only management can change it."""
    return is_manager(user) or not exam.is_published


def _edit_blocked(request, exam, questions=False):
    """A redirect explaining why this change isn't allowed right now, or None if it is."""
    if not _can_edit(request.user, exam):
        messages.error(request, "This exam is published — ask management to unpublish it before making changes.")
        return redirect("dashboard:exam_edit", exam_id=exam.id)
    if questions and exam.is_locked:
        messages.error(request, "Students have already started this exam — questions can't be changed.")
        return redirect("dashboard:exam_edit", exam_id=exam.id)
    return None


def _unpushed_submissions():
    return Submission.objects.filter(status=Submission.Status.SUBMITTED, pushed_to_raddai=False)


# --- Auth -------------------------------------------------------------------


# Per account, not per IP — staff share the school's connection too (see exams.views.student_login).
@ratelimit(key="post:username", rate="10/m", method="POST", block=False)
def staff_login(request):
    if request.user.is_authenticated and (is_manager(request.user) or is_teacher(request.user)):
        return redirect("dashboard:home")

    if getattr(request, "limited", False):
        messages.error(request, "Too many login attempts. Please wait a moment and try again.")
        return render(request, "dashboard/login.html")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None and (is_manager(user) or is_teacher(user)):
            login(request, user)
            return redirect("dashboard:home")
        messages.error(request, "Invalid staff ID/username or password.")

    return render(request, "dashboard/login.html")


@login_required(login_url="dashboard:staff_login")
def staff_logout(request):
    logout(request)
    return redirect("dashboard:staff_login")


@dashboard_required
def change_password(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        for relation in ("syncedmanager", "syncedstaff"):
            profile = getattr(user, relation, None)
            if profile and profile.must_change_password:
                profile.must_change_password = False
                profile.save(update_fields=["must_change_password"])
        messages.success(request, "Password changed.")
        return redirect("dashboard:home")
    return _render(request, "dashboard/change_password.html", "password", {"form": form})


# --- Overview & lists -------------------------------------------------------


@dashboard_required
def home(request):
    now = timezone.now()
    if is_manager(request.user):
        pending_push = (
            Exam.objects.annotate(
                unpushed=Count(
                    "submissions",
                    filter=Q(submissions__status=Submission.Status.SUBMITTED, submissions__pushed_to_raddai=False),
                )
            )
            .filter(unpushed__gt=0)
            .select_related("subject", "klass")
            .order_by("closes_at")[:5]
        )
        context = {
            "stats": {
                "students": SyncedStudent.objects.filter(is_active=True).count(),
                "staff": SyncedStaff.objects.filter(is_active=True).count(),
                "open_now": Exam.objects.filter(is_published=True, opens_at__lte=now, closes_at__gte=now).count(),
                "drafts": Exam.objects.filter(is_published=False).count(),
                "in_progress": Submission.objects.filter(status=Submission.Status.IN_PROGRESS).count(),
            },
            "pending_push": pending_push,
            "drafts": Exam.objects.filter(is_published=False).select_related(*EXAM_RELATED).order_by("-id")[:5],
        }
        return _render(request, "dashboard/home_manager.html", "home", context)

    mine = Exam.objects.filter(created_by=request.user)
    context = {
        "stats": {
            "total": mine.count(),
            "drafts": mine.filter(is_published=False).count(),
            "published": mine.filter(is_published=True).count(),
            "submitted": Submission.objects.filter(exam__in=mine, status=Submission.Status.SUBMITTED).count(),
        },
        "recent": mine.select_related("subject", "klass").order_by("-id")[:6],
    }
    return _render(request, "dashboard/home_teacher.html", "home", context)


@dashboard_required
def exam_list(request):
    exams = (
        _visible_exams(request.user)
        .select_related(*EXAM_RELATED)
        .annotate(submitted=Count("submissions", filter=Q(submissions__status=Submission.Status.SUBMITTED)))
        .order_by("-opens_at")
    )
    status = request.GET.get("status", "")
    if status == "draft":
        exams = exams.filter(is_published=False)
    elif status == "published":
        exams = exams.filter(is_published=True)
    class_id = request.GET.get("class", "")
    if class_id.isdigit():
        exams = exams.filter(klass_id=class_id)
    query = request.GET.get("q", "").strip()
    if query:
        exams = exams.filter(subject__name__icontains=query)

    return _render(request, "dashboard/exam_list.html", "exams", {
        "exams": exams,
        "classes": SyncedClass.objects.order_by("grade", "section"),
        "status": status,
        "class_id": class_id,
        "query": query,
    })


@dashboard_required
def exam_results(request, exam_id):
    exam = _get_exam(request, exam_id)
    submissions = exam.submissions.select_related("student").order_by("student__full_name")
    counts = dict(submissions.order_by().values_list("status").annotate(count=Count("id")))
    return _render(request, "dashboard/exam_results.html", "exams", {
        "exam": exam,
        "submissions": submissions,
        "in_progress_count": counts.get(Submission.Status.IN_PROGRESS, 0),
        "submitted_count": counts.get(Submission.Status.SUBMITTED, 0),
        "unpushed_count": submissions.filter(
            status=Submission.Status.SUBMITTED, pushed_to_raddai=False
        ).count(),
        "class_size": SyncedStudent.objects.filter(current_class=exam.klass, is_active=True).count(),
    })


# --- Pushing results (management only) --------------------------------------


def _push_submissions(request, submissions):
    # Never-failed first, then the oldest failures — so a few rows the main
    # portal keeps rejecting can't use up every click's time budget.
    submissions = list(
        submissions.select_related("student", "exam__subject", "exam__academic_year")
        .order_by(F("push_failed_at").asc(nulls_first=True), "id")
    )
    if not submissions:
        messages.info(request, "Nothing to push — every submitted result is already on the main portal.")
        return

    deadline = time.monotonic() + PUSH_TIME_BUDGET_SECONDS
    attempted = pushed = 0
    errors = []
    for submission in submissions:
        if time.monotonic() > deadline:
            break
        attempted += 1
        success, error = push_submission_to_raddai(submission, timeout=PUSH_REQUEST_TIMEOUT_SECONDS)
        if success:
            pushed += 1
        else:
            errors.append(f"{submission.student.full_name}: {error}")

    if pushed:
        messages.success(request, f"Pushed {pushed} result{'s' if pushed != 1 else ''} to the main portal.")
    if errors:
        messages.error(request, f"{len(errors)} failed — " + "; ".join(errors[:3]) + ("…" if len(errors) > 3 else ""))
    left = len(submissions) - attempted
    if left:
        messages.warning(request, f"{left} more still to push — click Push again to continue.")


@management_required
def results(request):
    exams = (
        Exam.objects.annotate(
            submitted=Count("submissions", filter=Q(submissions__status=Submission.Status.SUBMITTED)),
            unpushed=Count(
                "submissions",
                filter=Q(submissions__status=Submission.Status.SUBMITTED, submissions__pushed_to_raddai=False),
            ),
        )
        .filter(submitted__gt=0)
        .select_related("subject", "klass")
        .order_by("-unpushed", "-closes_at")
    )
    return _render(request, "dashboard/results.html", "results", {"exams": exams})


@management_required
@require_POST
def push_results(request, exam_id):
    exam = get_object_or_404(Exam, pk=exam_id)
    _push_submissions(request, _unpushed_submissions().filter(exam=exam))
    if request.POST.get("next") == "results":
        return redirect("dashboard:results")
    return redirect("dashboard:exam_results", exam_id=exam.id)


@management_required
@require_POST
def push_all(request):
    _push_submissions(request, _unpushed_submissions())
    return redirect("dashboard:results")


# --- Roster (management only) -----------------------------------------------


@management_required
def students(request):
    queryset = SyncedStudent.objects.select_related("current_class").order_by(
        "current_class__grade", "current_class__section", "full_name"
    )
    class_id = request.GET.get("class", "")
    if class_id.isdigit():
        queryset = queryset.filter(current_class_id=class_id)
    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(Q(full_name__icontains=query) | Q(student_id__icontains=query))

    page = Paginator(queryset, 50).get_page(request.GET.get("page"))
    return _render(request, "dashboard/students.html", "students", {
        "page": page,
        "classes": SyncedClass.objects.order_by("grade", "section"),
        "class_id": class_id,
        "query": query,
    })


@management_required
def staff(request):
    return _render(request, "dashboard/staff.html", "staff", {
        "managers": SyncedManager.objects.order_by("-is_active", "full_name"),
        "teachers": SyncedStaff.objects.order_by("-is_active", "full_name"),
    })


# --- Clearing old answers (management only) ---------------------------------


def _clearable_answers(year_id, term):
    """
    Answers of submitted scripts in that term's *closed* exams. The score is
    stored on the submission, so these are only a record of which option each
    student picked — clearing them keeps every score, the push to the main
    portal, and the exam's lock intact. Still-open exams are never touched.
    """
    return Answer.objects.filter(
        submission__exam__academic_year_id=year_id,
        submission__exam__term=term,
        submission__exam__closes_at__lt=timezone.now(),
        submission__status=Submission.Status.SUBMITTED,
    )


@management_required
def clear_answers(request):
    years = SyncedAcademicYear.objects.order_by("-start_date")
    year_id = request.POST.get("year") or request.GET.get("year", "")
    term = request.POST.get("term") or request.GET.get("term", "")
    year = years.filter(pk=year_id).first() if year_id.isdigit() else None
    term_ok = term in Exam.Term.values

    if request.method == "POST":
        if not (year and term_ok):
            messages.error(request, "Pick a session and a term first.")
        elif request.POST.get("confirm") != "yes":
            messages.error(request, "Tick the box to confirm before clearing.")
        else:
            answers = _clearable_answers(year.pk, term)
            deadline = time.monotonic() + CLEAR_TIME_BUDGET_SECONDS
            cleared = 0
            while time.monotonic() < deadline:
                batch = list(answers.values_list("id", flat=True)[:CLEAR_BATCH_SIZE])
                if not batch:
                    break
                Answer.objects.filter(id__in=batch).delete()
                cleared += len(batch)
            left = answers.count()
            label = f"{Exam.Term(term).label}, {year.name}"
            if cleared:
                messages.success(request, f"Cleared {cleared:,} saved answers for {label}. All scores are kept.")
            elif not left:
                messages.info(request, f"Nothing to clear for {label}.")
            if left:
                messages.warning(request, f"{left:,} answers still to clear — click Clear again to finish.")
        return redirect(f"{reverse('dashboard:clear_answers')}?year={year_id}&term={term}")

    preview = None
    if year and term_ok:
        exams = Exam.objects.filter(academic_year=year, term=term)
        closed = exams.filter(closes_at__lt=timezone.now())
        answers = _clearable_answers(year.pk, term)
        preview = {
            "label": f"{Exam.Term(term).label}, {year.name}",
            "closed_exams": closed.count(),
            "open_exams": exams.count() - closed.count(),
            "scripts": answers.values("submission_id").distinct().count(),
            "answers": answers.count(),
            "unpushed": Submission.objects.filter(
                exam__in=closed, status=Submission.Status.SUBMITTED, pushed_to_raddai=False
            ).count(),
        }

    return _render(request, "dashboard/clear_answers.html", "clear_answers", {
        "years": years,
        "terms": Exam.Term.choices,
        "year_id": year_id,
        "term": term,
        "preview": preview,
    })


@management_required
@require_POST
def sync_roster_now(request):
    output = StringIO()
    try:
        call_command("sync_roster", apply=True, stdout=output)
    except Exception as exc:  # raddai down, wrong key, network — tell the user rather than 500
        messages.error(request, f"Sync failed: {exc}")
    else:
        summary = [line for line in output.getvalue().splitlines() if re.match(r"^\w+: \d+ to create", line)]
        messages.success(request, "Synced from the main portal. " + " · ".join(summary))

    next_url = request.POST.get("next", "")
    return redirect(next_url if next_url.startswith("/dashboard/") else "dashboard:home")


# --- Exam authoring ---------------------------------------------------------


@dashboard_required
def exam_create(request):
    form = ExamForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        exam = form.save(commit=False)
        exam.created_by = request.user
        exam.save()
        messages.success(request, "Exam created. Now add its questions.")
        return redirect("dashboard:exam_edit", exam_id=exam.id)
    return _render(request, "dashboard/exam_create.html", "exam_create", {"form": form})


@dashboard_required
def exam_edit(request, exam_id):
    exam = _get_exam(request, exam_id)
    locked = exam.is_locked
    form = ExamForm(request.POST or None, instance=exam, locked=locked)

    if request.method == "POST":
        blocked = _edit_blocked(request, exam)
        if blocked:
            return blocked
        if form.is_valid():
            form.save()
            messages.success(request, "Exam details saved.")
            return redirect("dashboard:exam_edit", exam_id=exam.id)

    return _render(request, "dashboard/exam_edit.html", "exams", _exam_edit_context(request, exam, form, QuestionForm()))


def _exam_edit_context(request, exam, exam_form, question_form):
    return {
        "exam": exam,
        "form": exam_form,
        "question_form": question_form,
        "questions": exam.questions.prefetch_related("choices"),
        "locked": exam.is_locked,
        "can_edit": _can_edit(request.user, exam),
        "allocated_marks": exam.allocated_marks,
        "publish_problems": exam.publish_problems(),
    }


def _delete_pictures_after_commit(names):
    """Remove pictures from storage once the DB change that dropped them is committed (never before)."""
    names = [name for name in names if name]
    if names:
        storage = Question._meta.get_field("image").storage
        transaction.on_commit(lambda: [storage.delete(name) for name in names])


def _save_question(question, cleaned):
    old_picture = question.image.name if question.image else ""
    question.text = cleaned["text"].strip()
    question.marks = cleaned["marks"]
    if cleaned.get("image"):
        question.image = cleaned["image"]
    elif cleaned.get("remove_image"):
        question.image = None
    question.save()
    if old_picture and old_picture != question.image.name:
        _delete_pictures_after_commit([old_picture])
    question.choices.all().delete()
    Choice.objects.bulk_create([
        Choice(question=question, text=text, is_correct=(i == cleaned["correct"]), order=order)
        for order, (i, text) in enumerate(sorted(cleaned["filled_choices"].items()))
    ])


@dashboard_required
@require_POST
def question_add(request, exam_id):
    exam = _get_exam(request, exam_id)
    blocked = _edit_blocked(request, exam, questions=True)
    if blocked:
        return blocked

    question_form = QuestionForm(request.POST, request.FILES)
    if not question_form.is_valid():
        messages.error(request, "Question not added — fix the errors below.")
        context = _exam_edit_context(request, exam, ExamForm(instance=exam), question_form)
        return _render(request, "dashboard/exam_edit.html", "exams", context, status=400)

    next_order = (exam.questions.aggregate(m=Max("order"))["m"] or 0) + 1
    with transaction.atomic():
        _save_question(Question(exam=exam, order=next_order), question_form.cleaned_data)
    messages.success(request, "Question added.")
    return redirect(reverse("dashboard:exam_edit", args=[exam.id]) + "#questions")


@dashboard_required
def question_edit(request, exam_id, question_id):
    exam = _get_exam(request, exam_id)
    question = get_object_or_404(Question, pk=question_id, exam=exam)
    blocked = _edit_blocked(request, exam, questions=True)
    if blocked:
        return blocked

    if request.method == "POST":
        form = QuestionForm(request.POST, request.FILES)
        if form.is_valid():
            with transaction.atomic():
                _save_question(question, form.cleaned_data)
            messages.success(request, "Question updated.")
            return redirect("dashboard:exam_edit", exam_id=exam.id)
    else:
        form = QuestionForm(initial=QuestionForm.initial_for(question))

    return _render(request, "dashboard/question_edit.html", "exams", {"exam": exam, "question": question, "form": form})


@dashboard_required
@require_POST
def question_delete(request, exam_id, question_id):
    exam = _get_exam(request, exam_id)
    blocked = _edit_blocked(request, exam, questions=True)
    if blocked:
        return blocked
    question = get_object_or_404(Question, pk=question_id, exam=exam)
    with transaction.atomic():
        question.delete()
        _delete_pictures_after_commit([question.image.name])
    messages.success(request, "Question deleted.")
    return redirect("dashboard:exam_edit", exam_id=exam.id)


@management_required
@require_POST
def exam_toggle_publish(request, exam_id):
    exam = get_object_or_404(Exam, pk=exam_id)
    if exam.is_published:
        exam.is_published = False
        exam.save(update_fields=["is_published"])
        messages.success(request, "Exam unpublished — students can no longer see it.")
    else:
        problems = exam.publish_problems()
        if problems:
            for problem in problems:
                messages.error(request, problem)
        else:
            exam.is_published = True
            exam.save(update_fields=["is_published"])
            messages.success(request, "Exam published — students in the class will see it when it opens.")
    return redirect("dashboard:exam_edit", exam_id=exam.id)


@dashboard_required
@require_POST
def exam_delete(request, exam_id):
    exam = _get_exam(request, exam_id)
    blocked = _edit_blocked(request, exam)
    if blocked:
        return blocked
    if exam.is_locked:
        messages.error(request, "Students have already taken this exam — it can't be deleted.")
        return redirect("dashboard:exam_edit", exam_id=exam.id)
    with transaction.atomic():
        pictures = list(exam.questions.exclude(image="").values_list("image", flat=True))
        exam.delete()
        _delete_pictures_after_commit(pictures)
    messages.success(request, "Exam deleted.")
    return redirect("dashboard:exam_list")
