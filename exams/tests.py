from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from roster.models import SyncedAcademicYear, SyncedClass, SyncedStudent, SyncedSubject

from .models import Answer, Choice, Exam, Question, Submission

User = get_user_model()


class StudentFlowTests(TestCase):
    def setUp(self):
        year = SyncedAcademicYear.objects.create(
            raddai_id=1, name="2025/2026", start_date="2025-09-01", end_date="2026-07-31", is_active=True
        )
        self.jss1a = SyncedClass.objects.create(raddai_id=1, name="JSS1 A", grade=7, section="A", academic_year=year)
        self.jss1b = SyncedClass.objects.create(raddai_id=2, name="JSS1 B", grade=7, section="B", academic_year=year)
        maths = SyncedSubject.objects.create(raddai_id=1, name="Mathematics")

        now = timezone.now()
        window = {"opens_at": now - timedelta(hours=1), "closes_at": now + timedelta(hours=1)}
        common = {"subject": maths, "academic_year": year, "term": "first", "is_published": True, **window}
        self.second_ca = Exam.objects.create(klass=self.jss1a, score_target="ca2", **common)
        self.other_class_exam = Exam.objects.create(klass=self.jss1b, score_target="exam", **common)

        question = Question.objects.create(exam=self.second_ca, text="2+2?", marks=10)
        Choice.objects.create(question=question, text="3")
        self.correct = Choice.objects.create(question=question, text="4", is_correct=True)

        user = User.objects.create_user(username="STU1", password="pw")
        self.student = SyncedStudent.objects.create(
            raddai_id=1, student_id="STU1", full_name="Ada", current_class=self.jss1a, user=user
        )
        self.client.login(username="STU1", password="pw")

    def test_list_shows_only_own_class_with_ca_label(self):
        response = self.client.get(reverse("exams:exam_list"))
        self.assertEqual([r["exam"] for r in response.context["rows"]], [self.second_ca])
        self.assertContains(response, "Second CA")
        self.assertNotContains(response, "Examination")

    def test_cannot_open_another_class_exam(self):
        for name in ("start_exam", "take_exam", "submit_exam"):
            response = self.client.get(reverse(f"exams:{name}", args=[self.other_class_exam.id]))
            self.assertEqual(response.status_code, 404, name)

    def test_start_shows_confirmation_without_starting_timer(self):
        response = self.client.get(reverse("exams:start_exam", args=[self.second_ca.id]))
        self.assertContains(response, "You are about to take")
        self.assertContains(response, "Second CA")
        self.assertFalse(Submission.objects.exists())

        response = self.client.post(reverse("exams:start_exam", args=[self.second_ca.id]))
        self.assertRedirects(response, reverse("exams:take_exam", args=[self.second_ca.id]))
        self.assertEqual(Submission.objects.get().status, Submission.Status.IN_PROGRESS)

    def test_submit_grades_once(self):
        self.client.post(reverse("exams:start_exam", args=[self.second_ca.id]))
        question = self.second_ca.questions.get()
        url = reverse("exams:submit_exam", args=[self.second_ca.id])
        self.client.post(url, {f"question_{question.id}": self.correct.id})
        self.client.post(url, {})  # a second submit (refresh / double-click) must not overwrite
        submission = Submission.objects.get()
        self.assertEqual(submission.score, 10)
        self.assertEqual(Answer.objects.count(), 1)

    def test_unpublished_exam_hidden(self):
        self.second_ca.is_published = False
        self.second_ca.save()
        response = self.client.get(reverse("exams:exam_list"))
        self.assertEqual(response.context["rows"], [])


class LoginRateLimitTests(TestCase):
    """A whole class logs in from one school IP at the start of an exam — only repeated guessing on ONE account is limited."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        year = SyncedAcademicYear.objects.create(raddai_id=1, name="Y", start_date="2026-09-01", end_date="2027-07-31")
        klass = SyncedClass.objects.create(raddai_id=1, name="JSS1 A", grade=7, academic_year=year)
        for i in range(40):
            user = User.objects.create_user(username=f"LAB-{i:02d}", password=f"LAB-{i:02d}")
            SyncedStudent.objects.create(raddai_id=i + 1, student_id=f"LAB-{i:02d}", full_name=f"S{i}", current_class=klass, user=user)

    def test_forty_students_on_one_ip_can_all_log_in(self):
        for i in range(40):
            client = Client(REMOTE_ADDR="10.0.0.1")
            response = client.post(reverse("exams:student_login"), {"username": f"LAB-{i:02d}", "password": f"LAB-{i:02d}"})
            self.assertRedirects(response, reverse("exams:exam_list"), msg_prefix=f"student {i}")

    def test_guessing_one_account_is_limited(self):
        client = Client()
        for _ in range(10):
            client.post(reverse("exams:student_login"), {"username": "LAB-00", "password": "wrong"})
        response = client.post(reverse("exams:student_login"), {"username": "LAB-00", "password": "LAB-00"})
        self.assertContains(response, "Too many login attempts")
