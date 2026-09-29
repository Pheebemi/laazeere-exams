from functools import wraps

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def staff_required(view_func):
    @login_required(login_url="dashboard:staff_login")
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not hasattr(request.user, "syncedstaff"):
            return redirect("dashboard:staff_login")
        return view_func(request, *args, **kwargs)

    return wrapper
