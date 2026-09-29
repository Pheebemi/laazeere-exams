from functools import wraps

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def student_required(view_func):
    @login_required(login_url="exams:student_login")
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not hasattr(request.user, "syncedstudent"):
            return redirect("exams:student_login")
        return view_func(request, *args, **kwargs)

    return wrapper
