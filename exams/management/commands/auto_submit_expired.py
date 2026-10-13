"""
Force-submit any in_progress Submission past its deadline, so a student who
abandons the tab doesn't block staff review indefinitely. Uses the exact
same compare-and-swap finalize path as a normal submit, so it's safe to run
concurrently with a student's own late submit (whichever gets there first wins,
the other is a silent no-op).
"""

from django.core.management.base import BaseCommand

from exams.views import finalize_expired_submissions


class Command(BaseCommand):
    help = "Force-submit expired in-progress exam submissions."

    def handle(self, *args, **options):
        # The management pages do the same thing whenever they are opened;
        # this command is for running it by hand or on a schedule.
        closed, left = finalize_expired_submissions(budget_seconds=600)
        self.stdout.write(self.style.SUCCESS(f"Done. {closed} submission(s) force-submitted, {left} left."))
