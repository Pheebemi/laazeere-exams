from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("login/", views.staff_login, name="staff_login"),
    path("logout/", views.staff_logout, name="staff_logout"),
    path("password/", views.change_password, name="change_password"),
    path("", views.home, name="home"),
    path("exams/", views.exam_list, name="exam_list"),
    path("exams/new/", views.exam_create, name="exam_create"),
    path("<int:exam_id>/", views.exam_results, name="exam_results"),
    path("<int:exam_id>/edit/", views.exam_edit, name="exam_edit"),
    path("<int:exam_id>/publish/", views.exam_toggle_publish, name="exam_toggle_publish"),
    path("<int:exam_id>/code/", views.exam_generate_code, name="exam_generate_code"),
    path("<int:exam_id>/delete/", views.exam_delete, name="exam_delete"),
    path("<int:exam_id>/questions/add/", views.question_add, name="question_add"),
    path("<int:exam_id>/questions/<int:question_id>/edit/", views.question_edit, name="question_edit"),
    path("<int:exam_id>/questions/<int:question_id>/delete/", views.question_delete, name="question_delete"),
    path("<int:exam_id>/push/", views.push_results, name="push_results"),
    path("results/", views.results, name="results"),
    path("results/push-all/", views.push_all, name="push_all"),
    path("students/", views.students, name="students"),
    path("staff/", views.staff, name="staff"),
    path("sync/", views.sync_roster_now, name="sync_roster_now"),
    path("answers/clear/", views.clear_answers, name="clear_answers"),
]
