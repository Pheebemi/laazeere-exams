from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def is_manager(user):
    """Management/Admin on the main portal — publishes exams, pushes results, sees everything."""
    return hasattr(user, "syncedmanager") and user.syncedmanager.is_active


def is_teacher(user):
    return hasattr(user, "syncedstaff") and user.syncedstaff.is_active


def dashboard_required(view_func):
    """Teachers and management. Access comes entirely from the roster sync, never a manual flag."""

    @login_required(login_url="dashboard:staff_login")
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not (is_manager(request.user) or is_teacher(request.user)):
            return redirect("dashboard:staff_login")
        return view_func(request, *args, **kwargs)

    return wrapper


def management_required(view_func):
    @dashboard_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not is_manager(request.user):
            messages.error(request, "Only management can do that.")
            return redirect("dashboard:home")
        return view_func(request, *args, **kwargs)

    return wrapper
