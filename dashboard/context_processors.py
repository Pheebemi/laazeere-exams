from exams.models import Submission

from .decorators import is_manager


def dashboard(request):
    """Who is looking at the dashboard, for the sidebar. Skipped everywhere else to save queries."""
    user = getattr(request, "user", None)
    if not request.path.startswith("/dashboard/") or user is None or not user.is_authenticated:
        return {}

    manager = is_manager(user)
    profile = user.syncedmanager if manager else getattr(user, "syncedstaff", None)
    context = {"dashboard_is_manager": manager, "dashboard_profile": profile}
    if manager:
        context["dashboard_unpushed_count"] = Submission.objects.filter(
            status=Submission.Status.SUBMITTED, pushed_to_raddai=False
        ).count()
    return context
