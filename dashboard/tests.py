from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from exams.models import Exam, Submission
from roster.models import SyncedAcademicYear, SyncedClass, SyncedStaff, SyncedStudent, SyncedSubject

User = get_user_model()


class AuthoringTestBase(TestCase):
    def setUp(self):
        self.year = SyncedAcademicYear.objects.create(
            raddai_id=1, name="2025/2026", start_date="2025-09-01", end_date="2026-07-31", is_active=True
        )
        self.klass = SyncedClass.objects.create(raddai_id=1, name="JSS1 A", grade=7, section="A", academic_year=self.year)
        self.subject = SyncedSubject.objects.create(raddai_id=1, name="Mathematics")

        staff_user = User.objects.create_user(username="STAFF1", password="pw", is_staff=True)
        SyncedStaff.objects.create(raddai_id=1, staff_id="STAFF1", full_name="Teacher", user=staff_user)
        self.client.login(username="STAFF1", password="pw")

        now = timezone.now()
        self.exam = Exam.objects.create(
            subject=self.subject, klass=self.klass, academic_year=self.year, term="first",
            score_target="ca1", opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=2),
        )

    def add_question(self, marks, correct=2, text="Q?"):
        return self.client.post(reverse("dashboard:question_add", args=[self.exam.id]), {
            "text": text, "marks": marks, "correct": correct,
            "choice_1": "A", "choice_2": "B", "choice_3": "C",
        })

    def start_submission(self):
        student_user = User.objects.create_user(username="STU1", password="pw")
        student = SyncedStudent.objects.create(
            raddai_id=1, student_id="STU1", full_name="Student", current_class=self.klass, user=student_user
        )
        return Submission.objects.create(student=student, exam=self.exam)


class ExamCreateTests(AuthoringTestBase):
    def test_create_sets_total_marks_from_slot(self):
        now = timezone.localtime()
        response = self.client.post(reverse("dashboard:exam_create"), {
            "subject": self.subject.id, "klass": self.klass.id, "academic_year": self.year.id,
            "term": "first", "score_target": "exam", "duration_minutes": 40,
            "opens_at": now.strftime("%Y-%m-%dT%H:%M"),
            "closes_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        })
        exam = Exam.objects.exclude(pk=self.exam.pk).get()
        self.assertRedirects(response, reverse("dashboard:exam_edit", args=[exam.id]))
        self.assertEqual(exam.total_marks, 70)
        self.assertFalse(exam.is_published)

    def test_rejects_closing_before_opening(self):
        now = timezone.localtime()
        response = self.client.post(reverse("dashboard:exam_create"), {
            "subject": self.subject.id, "klass": self.klass.id, "academic_year": self.year.id,
            "term": "first", "score_target": "ca1", "duration_minutes": 20,
            "opens_at": now.strftime("%Y-%m-%dT%H:%M"),
            "closes_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Closing time must be after opening time.")
        self.assertEqual(Exam.objects.count(), 1)


class QuestionTests(AuthoringTestBase):
    def test_add_question_saves_choices_and_correct_answer(self):
        self.add_question(marks=4, correct=2)
        question = self.exam.questions.get()
        self.assertEqual([c.text for c in question.choices.all()], ["A", "B", "C"])
        self.assertEqual(question.choices.get(is_correct=True).text, "B")

    def test_needs_two_options(self):
        response = self.client.post(reverse("dashboard:question_add", args=[self.exam.id]), {
            "text": "Q?", "marks": 4, "correct": 1, "choice_1": "Only one",
        })
        self.assertContains(response, "Enter at least 2 options.", status_code=400)
        self.assertFalse(self.exam.questions.exists())

    def test_correct_answer_must_be_a_filled_option(self):
        response = self.client.post(reverse("dashboard:question_add", args=[self.exam.id]), {
            "text": "Q?", "marks": 4, "correct": 5, "choice_1": "A", "choice_2": "B",
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.exam.questions.exists())

    def test_edit_replaces_choices(self):
        self.add_question(marks=4, correct=1)
        question = self.exam.questions.get()
        self.client.post(reverse("dashboard:question_edit", args=[self.exam.id, question.id]), {
            "text": "Edited", "marks": 6, "correct": 2, "choice_1": "X", "choice_2": "Y",
        })
        question.refresh_from_db()
        self.assertEqual((question.text, question.marks), ("Edited", 6))
        self.assertEqual(list(question.choices.values_list("text", "is_correct")), [("X", False), ("Y", True)])


class PublishTests(AuthoringTestBase):
    def test_cannot_publish_until_marks_add_up(self):
        self.add_question(marks=4)
        self.client.post(reverse("dashboard:exam_toggle_publish", args=[self.exam.id]))
        self.exam.refresh_from_db()
        self.assertFalse(self.exam.is_published)

        self.add_question(marks=6)
        self.client.post(reverse("dashboard:exam_toggle_publish", args=[self.exam.id]))
        self.exam.refresh_from_db()
        self.assertTrue(self.exam.is_published)

    def test_cannot_publish_empty_exam(self):
        self.assertIn("Add at least one question.", self.exam.publish_problems())


class LockTests(AuthoringTestBase):
    """Once a student has started, anything that changes grading must be frozen."""

    def setUp(self):
        super().setUp()
        self.add_question(marks=10)
        self.question = self.exam.questions.get()
        self.start_submission()

    def test_cannot_add_edit_or_delete_questions(self):
        self.add_question(marks=1)
        self.client.post(reverse("dashboard:question_delete", args=[self.exam.id, self.question.id]))
        self.client.post(reverse("dashboard:question_edit", args=[self.exam.id, self.question.id]), {
            "text": "Changed", "marks": 5, "correct": 1, "choice_1": "X", "choice_2": "Y",
        })
        self.assertEqual(self.exam.questions.count(), 1)
        self.question.refresh_from_db()
        self.assertEqual((self.question.text, self.question.marks), ("Q?", 10))

    def test_cannot_change_result_slot_but_can_extend_deadline(self):
        new_close = timezone.localtime(self.exam.closes_at + timedelta(days=1))
        self.client.post(reverse("dashboard:exam_edit", args=[self.exam.id]), {
            "subject": self.subject.id, "klass": self.klass.id, "academic_year": self.year.id,
            "term": "second", "score_target": "exam", "duration_minutes": 30,
            "opens_at": timezone.localtime(self.exam.opens_at).strftime("%Y-%m-%dT%H:%M"),
            "closes_at": new_close.strftime("%Y-%m-%dT%H:%M"),
        })
        self.exam.refresh_from_db()
        self.assertEqual((self.exam.score_target, self.exam.term, self.exam.total_marks), ("ca1", "first", 10))
        self.assertEqual(timezone.localtime(self.exam.closes_at).strftime("%Y-%m-%dT%H:%M"),
                         new_close.strftime("%Y-%m-%dT%H:%M"))

    def test_cannot_delete_exam(self):
        self.client.post(reverse("dashboard:exam_delete", args=[self.exam.id]))
        self.assertTrue(Exam.objects.filter(pk=self.exam.pk).exists())


class AccessTests(AuthoringTestBase):
    def test_students_cannot_reach_authoring(self):
        self.client.logout()
        student_user = User.objects.create_user(username="STU9", password="pw")
        SyncedStudent.objects.create(raddai_id=9, student_id="STU9", full_name="S", current_class=self.klass, user=student_user)
        self.client.login(username="STU9", password="pw")
        response = self.client.get(reverse("dashboard:exam_create"))
        self.assertRedirects(response, reverse("dashboard:staff_login"))
