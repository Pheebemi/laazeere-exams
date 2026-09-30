from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from roster.models import SyncedAcademicYear, SyncedClass, SyncedStudent, SyncedSubject

from .models import Answer, Choice, Exam, Question, Submission

User = get_user_model()


class StudentExamBase(TestCase):
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
        self.second_ca = Exam.objects.create(klass=self.jss1a, score_target="ca2", access_code="482731", **common)
        self.other_class_exam = Exam.objects.create(klass=self.jss1b, score_target="exam", **common)

        question = Question.objects.create(exam=self.second_ca, text="2+2?", marks=10)
        Choice.objects.create(question=question, text="3")
        self.correct = Choice.objects.create(question=question, text="4", is_correct=True)

        user = User.objects.create_user(username="STU1", password="pw")
        self.student = SyncedStudent.objects.create(
            raddai_id=1, student_id="STU1", full_name="Ada", current_class=self.jss1a, user=user
        )
        self.client.login(username="STU1", password="pw")

    def start(self, client=None, code=None):
        return (client or self.client).post(
            reverse("exams:start_exam", args=[self.second_ca.id]),
            {"access_code": self.second_ca.access_code if code is None else code},
        )


class StudentFlowTests(StudentExamBase):
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

        response = self.start()
        self.assertRedirects(response, reverse("exams:take_exam", args=[self.second_ca.id]))
        self.assertEqual(Submission.objects.get().status, Submission.Status.IN_PROGRESS)

    def test_submit_grades_once(self):
        self.start()
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

    # Rate-limit windows are aligned to the clock; freeze it so the 11 slow
    # (password-hashing) attempts can't straddle a window boundary and reset.
    @mock.patch("django_ratelimit.core.time.time", return_value=1_800_000_000)
    def test_guessing_one_account_is_limited(self, _frozen_clock):
        client = Client()
        for _ in range(10):
            client.post(reverse("exams:student_login"), {"username": "LAB-00", "password": "wrong"})
        response = client.post(reverse("exams:student_login"), {"username": "LAB-00", "password": "LAB-00"})
        self.assertContains(response, "Too many login attempts")


class ExamSecurityTests(StudentExamBase):
    """Access code, per-student shuffling, one device at a time, and hidden scores."""

    def test_cannot_start_before_management_issues_a_code(self):
        Exam.objects.filter(pk=self.second_ca.pk).update(access_code="")
        response = self.start(code="")
        self.assertContains(response, "has not been opened yet")
        self.assertFalse(Submission.objects.exists())

    def test_wrong_code_does_not_start(self):
        response = self.start(code="000000" if self.second_ca.access_code != "000000" else "111111")
        self.assertContains(response, "access code is not correct")
        self.assertFalse(Submission.objects.exists())

    @mock.patch("django_ratelimit.core.time.time", return_value=1_800_000_000)
    def test_guessing_codes_is_limited(self, _frozen_clock):
        from django.core.cache import cache

        cache.clear()
        wrong = "000000" if self.second_ca.access_code != "000000" else "111111"
        for _ in range(10):
            self.start(code=wrong)
        response = self.start()
        self.assertContains(response, "Too many wrong codes")
        self.assertFalse(Submission.objects.exists())

    def test_continuing_on_a_new_session_needs_the_code_again(self):
        self.start()
        other = Client()
        other.login(username="STU1", password="pw")
        take = reverse("exams:take_exam", args=[self.second_ca.id])
        self.assertRedirects(other.get(take), reverse("exams:start_exam", args=[self.second_ca.id]))
        self.assertContains(other.get(reverse("exams:start_exam", args=[self.second_ca.id])), "Continue writing")
        self.assertRedirects(self.start(client=other), take)
        self.assertEqual(Submission.objects.count(), 1)

    def test_questions_and_options_are_shuffled_per_student_but_stable(self):
        for i in range(8):
            question = Question.objects.create(exam=self.second_ca, text=f"Q{i}", marks=1)
            for letter in "ABCD":
                Choice.objects.create(question=question, text=f"{i}{letter}")
        self.start()
        take = reverse("exams:take_exam", args=[self.second_ca.id])

        def order(client):
            questions = client.get(take).context["questions"]
            return [(q.id, [c.id for c in q.shuffled_choices]) for q in questions]

        first = order(self.client)
        self.assertEqual(first, order(self.client), "a refresh must keep the same order")
        self.assertEqual(sorted(q for q, _ in first), sorted(self.second_ca.questions.values_list("id", flat=True)))

        other_user = User.objects.create_user(username="STU2", password="pw")
        SyncedStudent.objects.create(raddai_id=2, student_id="STU2", full_name="Bo", current_class=self.jss1a, user=other_user)
        other = Client()
        other.login(username="STU2", password="pw")
        self.start(client=other)
        self.assertNotEqual(first, order(other))

    def test_logging_in_elsewhere_signs_out_the_first_device(self):
        login_url = reverse("exams:student_login")
        first, second = Client(), Client()
        first.post(login_url, {"username": "STU1", "password": "pw"})
        self.assertEqual(first.get(reverse("exams:exam_list")).status_code, 200)

        second.post(login_url, {"username": "STU1", "password": "pw"})
        response = first.get(reverse("exams:exam_list"), follow=True)
        self.assertContains(response, "opened on another device")
        self.assertEqual(second.get(reverse("exams:exam_list")).status_code, 200)

    def test_students_never_see_their_score(self):
        self.start()
        question = self.second_ca.questions.get()
        response = self.client.post(
            reverse("exams:submit_exam", args=[self.second_ca.id]), {f"question_{question.id}": self.correct.id}, follow=True
        )
        self.assertEqual(Submission.objects.get().score, 10)
        self.assertContains(response, "Your answers have been received")
        self.assertNotContains(response, "Your score")
        listing = self.client.get(reverse("exams:exam_list"))
        self.assertContains(listing, "Submitted")
        self.assertNotContains(listing, "10/10")
