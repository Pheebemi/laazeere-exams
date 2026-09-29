from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from exams.models import Choice, Exam, Question, Submission
from exams.services import push_submission_to_raddai

from .decorators import staff_required
from .forms import ExamForm, QuestionForm


@ratelimit(key="ip", rate="5/m", method="POST", block=False)
def staff_login(request):
    if getattr(request, "limited", False):
        messages.error(request, "Too many login attempts. Please wait a moment and try again.")
        return render(request, "dashboard/login.html")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None and hasattr(user, "syncedstaff"):
            login(request, user)
            return redirect("dashboard:exam_monitor_list")
        messages.error(request, "Invalid staff ID or password.")

    return render(request, "dashboard/login.html")


@login_required(login_url="dashboard:staff_login")
def staff_logout(request):
    logout(request)
    return redirect("dashboard:staff_login")


@staff_required
def exam_monitor_list(request):
    exams = Exam.objects.select_related("subject", "klass").order_by("-opens_at")
    return render(request, "dashboard/exam_monitor_list.html", {"exams": exams})


@staff_required
def exam_monitor(request, exam_id):
    exam = get_object_or_404(Exam, pk=exam_id)
    counts = dict(
        Submission.objects.filter(exam=exam).values_list("status").annotate(count=Count("id"))
    )
    unpushed_count = Submission.objects.filter(
        exam=exam, status=Submission.Status.SUBMITTED, pushed_to_raddai=False
    ).count()
    return render(
        request,
        "dashboard/exam_monitor.html",
        {
            "exam": exam,
            "in_progress_count": counts.get(Submission.Status.IN_PROGRESS, 0),
            "submitted_count": counts.get(Submission.Status.SUBMITTED, 0),
            "unpushed_count": unpushed_count,
        },
    )


@staff_required
def push_results(request, exam_id):
    exam = get_object_or_404(Exam, pk=exam_id)
    if request.method != "POST":
        return redirect("dashboard:exam_monitor", exam_id=exam.id)

    submissions = Submission.objects.filter(
        exam=exam, status=Submission.Status.SUBMITTED, pushed_to_raddai=False
    )
    pushed = failed = 0
    for submission in submissions:
        success, error = push_submission_to_raddai(submission)
        if success:
            pushed += 1
        else:
            failed += 1
            messages.warning(request, f"Failed to push {submission.student}: {error}")

    messages.success(request, f"Pushed {pushed} result(s) to raddai-backend. {failed} failed.")
    return redirect("dashboard:exam_monitor", exam_id=exam.id)


# --- Exam authoring ---------------------------------------------------------


@staff_required
def exam_create(request):
    form = ExamForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        exam = form.save()
        messages.success(request, "Exam created. Now add its questions.")
        return redirect("dashboard:exam_edit", exam_id=exam.id)
    return render(request, "dashboard/exam_create.html", {"form": form})


@staff_required
def exam_edit(request, exam_id):
    exam = get_object_or_404(Exam.objects.select_related("subject", "klass"), pk=exam_id)
    locked = exam.is_locked
    form = ExamForm(request.POST or None, instance=exam, locked=locked)

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Exam details saved.")
        return redirect("dashboard:exam_edit", exam_id=exam.id)

    return render(request, "dashboard/exam_edit.html", _exam_edit_context(exam, form, QuestionForm()))


def _exam_edit_context(exam, exam_form, question_form):
    return {
        "exam": exam,
        "form": exam_form,
        "question_form": question_form,
        "questions": exam.questions.prefetch_related("choices"),
        "locked": exam.is_locked,
        "allocated_marks": exam.allocated_marks,
        "publish_problems": exam.publish_problems(),
    }


def _save_question(question, cleaned):
    question.text = cleaned["text"].strip()
    question.marks = cleaned["marks"]
    question.save()
    question.choices.all().delete()
    Choice.objects.bulk_create([
        Choice(question=question, text=text, is_correct=(i == cleaned["correct"]), order=order)
        for order, (i, text) in enumerate(sorted(cleaned["filled_choices"].items()))
    ])


@staff_required
@require_POST
def question_add(request, exam_id):
    exam = get_object_or_404(Exam, pk=exam_id)
    if exam.is_locked:
        messages.error(request, "Students have already started this exam — questions can't be changed.")
        return redirect("dashboard:exam_edit", exam_id=exam.id)

    question_form = QuestionForm(request.POST)
    if not question_form.is_valid():
        messages.error(request, "Question not added — fix the errors below.")
        context = _exam_edit_context(exam, ExamForm(instance=exam), question_form)
        return render(request, "dashboard/exam_edit.html", context, status=400)

    next_order = (exam.questions.aggregate(m=Max("order"))["m"] or 0) + 1
    with transaction.atomic():
        _save_question(Question(exam=exam, order=next_order), question_form.cleaned_data)
    messages.success(request, "Question added.")
    return redirect(reverse("dashboard:exam_edit", args=[exam.id]) + "#questions")


@staff_required
def question_edit(request, exam_id, question_id):
    exam = get_object_or_404(Exam, pk=exam_id)
    question = get_object_or_404(Question, pk=question_id, exam=exam)
    if exam.is_locked:
        messages.error(request, "Students have already started this exam — questions can't be changed.")
        return redirect("dashboard:exam_edit", exam_id=exam.id)

    if request.method == "POST":
        form = QuestionForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                _save_question(question, form.cleaned_data)
            messages.success(request, "Question updated.")
            return redirect("dashboard:exam_edit", exam_id=exam.id)
    else:
        form = QuestionForm(initial=QuestionForm.initial_for(question))

    return render(request, "dashboard/question_edit.html", {"exam": exam, "question": question, "form": form})


@staff_required
@require_POST
def question_delete(request, exam_id, question_id):
    exam = get_object_or_404(Exam, pk=exam_id)
    if exam.is_locked:
        messages.error(request, "Students have already started this exam — questions can't be changed.")
    else:
        get_object_or_404(Question, pk=question_id, exam=exam).delete()
        messages.success(request, "Question deleted.")
    return redirect("dashboard:exam_edit", exam_id=exam.id)


@staff_required
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


@staff_required
@require_POST
def exam_delete(request, exam_id):
    exam = get_object_or_404(Exam, pk=exam_id)
    if exam.is_locked:
        messages.error(request, "Students have already taken this exam — it can't be deleted.")
        return redirect("dashboard:exam_edit", exam_id=exam.id)
    exam.delete()
    messages.success(request, "Exam deleted.")
    return redirect("dashboard:exam_monitor_list")
