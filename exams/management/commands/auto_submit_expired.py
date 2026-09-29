"""
Force-submit any in_progress Submission past its deadline, so a student who
abandons the tab doesn't block staff review indefinitely. Uses the exact
same compare-and-swap finalize path as a normal submit, so it's safe to run
concurrently with a student's own late submit (whichever gets there first wins,
the other is a silent no-op).
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from exams.models import Submission
from exams.views import _finalize_submission


class Command(BaseCommand):
    help = "Force-submit expired in-progress exam submissions."

    def handle(self, *args, **options):
        now = timezone.now()
        candidates = Submission.objects.filter(status=Submission.Status.IN_PROGRESS).select_related(
            "exam", "student"
        )

        finalized = 0
        for submission in candidates:
            deadline = min(
                submission.exam.closes_at,
                submission.started_at + timezone.timedelta(minutes=submission.exam.duration_minutes),
            )
            if now >= deadline:
                _finalize_submission(submission, {})
                finalized += 1
                self.stdout.write(f"Force-submitted: {submission}")

        self.stdout.write(self.style.SUCCESS(f"Done. {finalized} submission(s) force-submitted."))
