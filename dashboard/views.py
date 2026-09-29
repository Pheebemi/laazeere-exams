from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django_ratelimit.decorators import ratelimit

from exams.models import Exam, Submission
from exams.services import push_submission_to_raddai

from .decorators import staff_required


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
