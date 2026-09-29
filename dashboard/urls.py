from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("login/", views.staff_login, name="staff_login"),
    path("logout/", views.staff_logout, name="staff_logout"),
    path("", views.exam_monitor_list, name="exam_monitor_list"),
    path("<int:exam_id>/", views.exam_monitor, name="exam_monitor"),
    path("<int:exam_id>/push/", views.push_results, name="push_results"),
]
