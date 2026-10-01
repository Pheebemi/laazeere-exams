from django.urls import path

from . import views

app_name = "exams"

urlpatterns = [
    path("", views.exam_list, name="exam_list"),
    path("login/", views.student_login, name="student_login"),
    path("logout/", views.student_logout, name="student_logout"),
    path("<int:exam_id>/start/", views.start_exam, name="start_exam"),
    path("<int:exam_id>/take/", views.take_exam, name="take_exam"),
    path("<int:exam_id>/submit/", views.submit_exam, name="submit_exam"),
    path("<int:exam_id>/save/", views.save_answers, name="save_answers"),
    path("<int:exam_id>/already-submitted/", views.already_submitted, name="already_submitted"),
]
