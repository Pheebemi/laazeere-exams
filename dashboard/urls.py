from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("login/", views.staff_login, name="staff_login"),
    path("logout/", views.staff_logout, name="staff_logout"),
    path("", views.exam_monitor_list, name="exam_monitor_list"),
    path("exams/new/", views.exam_create, name="exam_create"),
    path("<int:exam_id>/", views.exam_monitor, name="exam_monitor"),
    path("<int:exam_id>/edit/", views.exam_edit, name="exam_edit"),
    path("<int:exam_id>/publish/", views.exam_toggle_publish, name="exam_toggle_publish"),
    path("<int:exam_id>/delete/", views.exam_delete, name="exam_delete"),
    path("<int:exam_id>/questions/add/", views.question_add, name="question_add"),
    path("<int:exam_id>/questions/<int:question_id>/edit/", views.question_edit, name="question_edit"),
    path("<int:exam_id>/questions/<int:question_id>/delete/", views.question_delete, name="question_delete"),
    path("<int:exam_id>/push/", views.push_results, name="push_results"),
]
