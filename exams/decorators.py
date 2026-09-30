from functools import wraps

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def student_required(view_func):
    @login_required(login_url="exams:student_login")
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not hasattr(request.user, "syncedstudent"):
            return redirect("exams:student_login")
        # One device at a time: each login records its session, so an older
        # session on another phone/computer no longer matches and is signed out.
        current = request.user.syncedstudent.current_session_key
        if current and current != request.session.session_key:
            logout(request)
            messages.warning(request, "You were signed out because your account was opened on another device.")
            return redirect("exams:student_login")
        return view_func(request, *args, **kwargs)

    return wrapper
