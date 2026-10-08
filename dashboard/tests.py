import os
import shutil
import tempfile
from datetime import timedelta
from io import BytesIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from dashboard.forms import MAX_IMAGE_UPLOAD_BYTES
from exams.models import Answer, Choice, Exam, Question, Submission
from roster.models import (
    SyncedAcademicYear, SyncedClass, SyncedManager, SyncedStaff, SyncedStudent, SyncedSubject,
)

User = get_user_model()


class AuthoringTestBase(TestCase):
    def setUp(self):
        self.year = SyncedAcademicYear.objects.create(
            raddai_id=1, name="2025/2026", start_date="2025-09-01", end_date="2026-07-31", is_active=True
        )
        self.klass = SyncedClass.objects.create(raddai_id=1, name="JSS1 A", grade=7, section="A", academic_year=self.year)
        self.subject = SyncedSubject.objects.create(raddai_id=1, name="Mathematics")

        self.teacher = User.objects.create_user(username="STAFF1", password="pw", is_staff=True)
        SyncedStaff.objects.create(raddai_id=1, staff_id="STAFF1", full_name="Teacher", user=self.teacher)
        self.client.login(username="STAFF1", password="pw")

        now = timezone.now()
        self.exam = Exam.objects.create(
            subject=self.subject, academic_year=self.year, term="first",
            score_target="ca1", opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=2),
            created_by=self.teacher,
        )
        self.exam.classes.add(self.klass)

    def login_as_manager(self):
        manager = User.objects.create_user(username="bursar", password="pw")
        SyncedManager.objects.create(raddai_id=1, username="bursar", full_name="Mrs Bursar", role="management", user=manager)
        self.client.logout()
        self.client.login(username="bursar", password="pw")
        return manager

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
            "subject": self.subject.id, "classes": [self.klass.id], "academic_year": self.year.id,
            "term": "first", "score_target": "exam", "duration_minutes": 40,
            "opens_at": now.strftime("%Y-%m-%dT%H:%M"),
            "closes_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        })
        exam = Exam.objects.exclude(pk=self.exam.pk).get()
        self.assertRedirects(response, reverse("dashboard:exam_edit", args=[exam.id]))
        self.assertEqual(exam.total_marks, 70)
        self.assertFalse(exam.is_published)
        self.assertEqual(exam.created_by, self.teacher)

    def test_rejects_closing_before_opening(self):
        now = timezone.localtime()
        response = self.client.post(reverse("dashboard:exam_create"), {
            "subject": self.subject.id, "classes": [self.klass.id], "academic_year": self.year.id,
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
        self.login_as_manager()
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
            "subject": self.subject.id, "classes": [self.klass.id], "academic_year": self.year.id,
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


class TeacherPermissionTests(AuthoringTestBase):
    """Teachers write exams; management publishes them and pushes results."""

    def setUp(self):
        super().setUp()
        self.add_question(marks=10)

    def test_teacher_cannot_publish(self):
        response = self.client.post(reverse("dashboard:exam_toggle_publish", args=[self.exam.id]))
        self.assertRedirects(response, reverse("dashboard:home"))
        self.exam.refresh_from_db()
        self.assertFalse(self.exam.is_published)

    @mock.patch("dashboard.views.push_submission_to_raddai")
    def test_teacher_cannot_push(self, push):
        submission = self.start_submission()
        submission.status = Submission.Status.SUBMITTED
        submission.save()
        self.client.post(reverse("dashboard:push_results", args=[self.exam.id]))
        self.client.post(reverse("dashboard:push_all"))
        push.assert_not_called()

    def test_teacher_cannot_see_another_teachers_exam(self):
        other = User.objects.create_user(username="STAFF2", password="pw")
        SyncedStaff.objects.create(raddai_id=2, staff_id="STAFF2", full_name="Other", user=other)
        self.exam.created_by = other
        self.exam.save()
        self.assertEqual(self.client.get(reverse("dashboard:exam_edit", args=[self.exam.id])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("dashboard:exam_list")), "Mathematics")

    def test_teacher_cannot_change_a_published_exam(self):
        self.exam.is_published = True
        self.exam.save()
        self.add_question(marks=1)
        self.client.post(reverse("dashboard:exam_delete", args=[self.exam.id]))
        self.assertEqual(self.exam.questions.count(), 1)
        self.assertTrue(Exam.objects.filter(pk=self.exam.pk).exists())

    def test_teacher_cannot_open_management_pages(self):
        for name in ("results", "students", "staff"):
            self.assertRedirects(self.client.get(reverse(f"dashboard:{name}")), reverse("dashboard:home"))


class ManagementTests(AuthoringTestBase):
    def setUp(self):
        super().setUp()
        self.add_question(marks=10)
        self.login_as_manager()

    def test_manager_logs_in_to_the_management_dashboard(self):
        self.client.logout()
        response = self.client.post(reverse("dashboard:staff_login"), {"username": "bursar", "password": "pw"}, follow=True)
        self.assertContains(response, "Push results")
        self.assertContains(response, "Staff & management")

    def test_manager_sees_every_exam_and_can_publish(self):
        self.assertContains(self.client.get(reverse("dashboard:exam_list")), "Mathematics")
        self.client.post(reverse("dashboard:exam_toggle_publish", args=[self.exam.id]))
        self.exam.refresh_from_db()
        self.assertTrue(self.exam.is_published)

    @mock.patch("dashboard.views.push_submission_to_raddai", return_value=(True, None))
    def test_push_all_sends_only_submitted_unpushed_scripts(self, push):
        submission = self.start_submission()
        submission.status = Submission.Status.SUBMITTED
        submission.save()
        response = self.client.post(reverse("dashboard:push_all"), follow=True)
        push.assert_called_once_with(submission, timeout=10)
        self.assertContains(response, "Pushed 1 result to the main portal.")

    def test_management_pages_load(self):
        self.start_submission()
        for name in ("home", "results", "students", "staff", "exam_list"):
            self.assertEqual(self.client.get(reverse(f"dashboard:{name}")).status_code, 200, name)
        self.assertContains(self.client.get(reverse("dashboard:students")), "STU1")

    def test_disabled_manager_loses_access(self):
        SyncedManager.objects.update(is_active=False)
        self.assertRedirects(self.client.get(reverse("dashboard:home")), reverse("dashboard:staff_login"))


class ChangePasswordTests(AuthoringTestBase):
    def test_changing_password_clears_the_starter_flag(self):
        response = self.client.post(reverse("dashboard:change_password"), {
            "old_password": "pw", "new_password1": "a-much-better-pass-42", "new_password2": "a-much-better-pass-42",
        })
        self.assertRedirects(response, reverse("dashboard:home"))
        self.assertFalse(SyncedStaff.objects.get().must_change_password)
        self.client.logout()
        self.assertTrue(self.client.login(username="STAFF1", password="a-much-better-pass-42"))

    def test_student_side_does_not_advertise_staff_login(self):
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("exams:student_login")), reverse("dashboard:staff_login"))


class PushFailureTests(AuthoringTestBase):
    def setUp(self):
        super().setUp()
        self.first = self.start_submission()
        self.first.status = Submission.Status.SUBMITTED
        self.first.score = 10
        self.first.save()

    @mock.patch("exams.services.requests.post")
    def test_failure_is_recorded_and_cleared_on_success(self, post):
        from exams.services import push_submission_to_raddai

        post.return_value.status_code = 404
        post.return_value.json.return_value = {"error": "Student not found"}
        self.assertEqual(push_submission_to_raddai(self.first, timeout=5), (False, "Student not found"))
        self.assertEqual(post.call_args.kwargs["timeout"], 5)
        self.first.refresh_from_db()
        self.assertEqual(self.first.push_error, "Student not found")
        self.assertIsNotNone(self.first.push_failed_at)

        post.return_value.status_code = 200
        self.assertEqual(push_submission_to_raddai(self.first), (True, None))
        self.first.refresh_from_db()
        self.assertTrue(self.first.pushed_to_raddai)
        self.assertEqual((self.first.push_error, self.first.push_failed_at), ("", None))

    def test_earlier_failures_are_retried_after_everything_else(self):
        self.first.push_failed_at = timezone.now()
        self.first.save()
        other_user = User.objects.create_user(username="STU2", password="pw")
        other_student = SyncedStudent.objects.create(
            raddai_id=2, student_id="STU2", full_name="Second", current_class=self.klass, user=other_user
        )
        second = Submission.objects.create(student=other_student, exam=self.exam, status=Submission.Status.SUBMITTED, score=5)

        self.login_as_manager()
        with mock.patch("dashboard.views.push_submission_to_raddai", return_value=(True, None)) as push:
            self.client.post(reverse("dashboard:push_all"))
        self.assertEqual([c.args[0].pk for c in push.call_args_list], [second.pk, self.first.pk])
        self.assertEqual(push.call_args.kwargs["timeout"], 10)


def picture_upload(name="diagram.png", size=(2400, 1200), color=(20, 120, 60)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class QuestionPictureTests(AuthoringTestBase):
    def setUp(self):
        super().setUp()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = self.settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)

    def post_question(self, url, **extra):
        return self.client.post(url, {
            "text": "Name this shape", "marks": 4, "correct": 1, "choice_1": "Square", "choice_2": "Circle", **extra,
        })

    def add_url(self):
        return reverse("dashboard:question_add", args=[self.exam.id])

    def test_picture_is_shrunk_to_webp(self):
        self.post_question(self.add_url(), image=picture_upload())
        question = self.exam.questions.get()
        self.assertTrue(question.image.name.endswith(".webp"))
        with Image.open(question.image.path) as saved:
            self.assertEqual(saved.format, "WEBP")
            self.assertEqual(saved.size, (1600, 800))

    def test_non_picture_is_rejected(self):
        response = self.post_question(self.add_url(), image=SimpleUploadedFile("notes.png", b"not really a picture"))
        self.assertContains(response, "t a picture we can read", status_code=400)
        self.assertFalse(self.exam.questions.exists())

    def test_clear_message_when_blob_is_not_connected(self):
        with self.settings(PICTURE_UPLOADS_ENABLED=False):
            response = self.post_question(self.add_url(), image=picture_upload())
        self.assertContains(response, "switched on yet", status_code=400)
        self.assertFalse(self.exam.questions.exists())

    def test_oversized_upload_is_rejected(self):
        huge = SimpleUploadedFile("huge.jpg", b"x" * (MAX_IMAGE_UPLOAD_BYTES + 1), content_type="image/jpeg")
        response = self.post_question(self.add_url(), image=huge)
        self.assertContains(response, "too big", status_code=400)
        self.assertFalse(self.exam.questions.exists())

    def test_replacing_and_removing_delete_the_old_file(self):
        self.post_question(self.add_url(), image=picture_upload())
        question = self.exam.questions.get()
        first = question.image.path
        edit_url = reverse("dashboard:question_edit", args=[self.exam.id, question.id])

        with self.captureOnCommitCallbacks(execute=True):
            self.post_question(edit_url, image=picture_upload(color=(200, 0, 0)))
        question.refresh_from_db()
        self.assertFalse(os.path.exists(first))
        second = question.image.path
        self.assertTrue(os.path.exists(second))

        with self.captureOnCommitCallbacks(execute=True):
            self.post_question(edit_url, remove_image="on")
        question.refresh_from_db()
        self.assertFalse(question.image)
        self.assertFalse(os.path.exists(second))

    def test_editing_text_keeps_the_picture(self):
        self.post_question(self.add_url(), image=picture_upload())
        question = self.exam.questions.get()
        name = question.image.name
        self.post_question(reverse("dashboard:question_edit", args=[self.exam.id, question.id]), text="Renamed")
        question.refresh_from_db()
        self.assertEqual((question.text, question.image.name), ("Renamed", name))

    def test_deleting_question_or_exam_deletes_pictures(self):
        self.post_question(self.add_url(), image=picture_upload())
        self.post_question(self.add_url(), image=picture_upload())
        first, second = self.exam.questions.all()
        paths = [first.image.path, second.image.path]

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("dashboard:question_delete", args=[self.exam.id, first.id]))
        self.assertFalse(os.path.exists(paths[0]))
        self.assertTrue(os.path.exists(paths[1]))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("dashboard:exam_delete", args=[self.exam.id]))
        self.assertFalse(os.path.exists(paths[1]))

    def test_student_sees_the_picture(self):
        self.post_question(self.add_url(), image=picture_upload(), marks=10)
        question = self.exam.questions.get()
        self.login_as_manager()
        self.client.post(reverse("dashboard:exam_toggle_publish", args=[self.exam.id]))
        self.exam.refresh_from_db()
        self.assertTrue(self.exam.is_published)

        self.client.post(reverse("dashboard:exam_generate_code", args=[self.exam.id]))
        self.exam.refresh_from_db()
        submission = self.start_submission()
        self.client.force_login(submission.student.user)
        response = self.client.post(
            reverse("exams:start_exam", args=[self.exam.id]), {"access_code": self.exam.access_code}, follow=True
        )
        self.assertContains(response, question.image.url)


class ClearAnswersTests(AuthoringTestBase):
    """Management clears one term's saved answer choices; scores and everything else stay."""

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.student = self.start_submission().student  # STU1, in-progress script on self.exam
        Submission.objects.all().delete()

        self.first_term = self.exam  # first term, closed below
        self.second_term = Exam.objects.create(
            subject=self.subject, academic_year=self.year, term="second", score_target="ca1",
            opens_at=now - timedelta(days=3), closes_at=now - timedelta(days=2),
        )
        self.still_open = Exam.objects.create(
            subject=self.subject, academic_year=self.year, term="first", score_target="ca2",
            opens_at=now - timedelta(hours=1), closes_at=now + timedelta(hours=1),
        )
        self.second_term.classes.add(self.klass)
        self.still_open.classes.add(self.klass)
        Exam.objects.filter(pk=self.first_term.pk).update(
            opens_at=now - timedelta(days=3), closes_at=now - timedelta(days=2)
        )
        self.cleared = self.script(self.first_term)
        self.kept_other_term = self.script(self.second_term)
        self.kept_open_exam = self.script(self.still_open)
        self.url = reverse("dashboard:clear_answers")

    def script(self, exam):
        question = Question.objects.create(exam=exam, text="Q?", marks=10)
        right = Choice.objects.create(question=question, text="A", is_correct=True)
        submission = Submission.objects.create(
            student=self.student, exam=exam, status=Submission.Status.SUBMITTED, score=10
        )
        return Answer.objects.create(submission=submission, question=question, selected_choice=right)

    def clear(self, follow=False, **extra):
        return self.client.post(self.url, {"year": self.year.id, "term": "first", **extra}, follow=follow)

    def test_teachers_cannot_clear(self):
        self.assertRedirects(self.client.get(self.url), reverse("dashboard:home"))
        self.clear(confirm="yes")
        self.assertEqual(Answer.objects.count(), 3)

    def test_preview_counts_only_finished_exams_of_that_term(self):
        self.login_as_manager()
        preview = self.client.get(self.url, {"year": self.year.id, "term": "first"}).context["preview"]
        self.assertEqual(
            (preview["closed_exams"], preview["open_exams"], preview["scripts"], preview["answers"]), (1, 1, 1, 1)
        )

    def test_nothing_is_cleared_without_confirming(self):
        self.login_as_manager()
        self.clear()
        self.assertEqual(Answer.objects.count(), 3)

    def test_clears_only_that_terms_finished_exams_and_keeps_scores(self):
        self.login_as_manager()
        response = self.clear(confirm="yes", follow=True)
        self.assertContains(response, "Cleared 1 saved answers")
        self.assertFalse(Answer.objects.filter(pk=self.cleared.pk).exists())
        self.assertTrue(Answer.objects.filter(pk=self.kept_other_term.pk).exists())
        self.assertTrue(Answer.objects.filter(pk=self.kept_open_exam.pk).exists())
        self.assertEqual(Submission.objects.get(exam=self.first_term).score, 10)


class AccessCodeTests(AuthoringTestBase):
    """Only management issues access codes; teachers never see them."""

    def setUp(self):
        super().setUp()
        Exam.objects.filter(pk=self.exam.pk).update(is_published=True)
        self.generate = reverse("dashboard:exam_generate_code", args=[self.exam.id])

    def test_teachers_cannot_generate_or_see_codes(self):
        self.assertRedirects(self.client.post(self.generate), reverse("dashboard:home"))
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.access_code, "")
        Exam.objects.filter(pk=self.exam.pk).update(access_code="482731")
        self.assertNotContains(self.client.get(reverse("dashboard:exam_results", args=[self.exam.id])), "482731")
        self.assertNotContains(self.client.get(reverse("dashboard:exam_list")), "482731")

    def test_management_generates_from_the_exam_list(self):
        self.login_as_manager()
        listing = reverse("dashboard:exam_list")
        self.assertContains(self.client.get(listing), "Generate code")
        response = self.client.post(self.generate, {"next": listing}, follow=True)
        self.exam.refresh_from_db()
        self.assertRegex(self.exam.access_code, r"^\d{6}$")
        self.assertContains(response, self.exam.access_code)
        self.assertContains(response, "New code")

    def test_new_code_replaces_the_old_one(self):
        self.login_as_manager()
        self.client.post(self.generate)
        self.exam.refresh_from_db()
        old = self.exam.access_code
        for _ in range(5):  # a random 6-digit code could repeat by chance; five tries make that negligible
            self.client.post(self.generate)
            self.exam.refresh_from_db()
            if self.exam.access_code != old:
                break
        self.assertNotEqual(self.exam.access_code, old)


class SubjectForClassTests(AuthoringTestBase):
    """A new exam only accepts a subject the chosen class takes."""

    def post_exam(self, subject):
        now = timezone.localtime()
        return self.client.post(reverse("dashboard:exam_create"), {
            "classes": [self.klass.id], "subject": subject.id, "academic_year": self.year.id,
            "term": "first", "score_target": "ca1", "duration_minutes": 20,
            "opens_at": now.strftime("%Y-%m-%dT%H:%M"),
            "closes_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        })

    def test_subject_not_taken_by_the_class_is_refused(self):
        seniors_only = SyncedSubject.objects.create(raddai_id=50, name="Physics", grades=[10, 11, 12])
        response = self.post_exam(seniors_only)
        self.assertContains(response, "does not take Physics")
        self.assertEqual(Exam.objects.count(), 1)

    def test_subject_for_the_class_grade_or_for_every_class_is_accepted(self):
        juniors = SyncedSubject.objects.create(raddai_id=51, name="Basic Science", grades=[7, 8, 9])
        for subject in (juniors, self.subject):  # self.subject has no grades = every class
            self.assertEqual(self.post_exam(subject).status_code, 302, subject.name)
        self.assertEqual(Exam.objects.count(), 3)

    def test_page_carries_the_class_and_subject_data(self):
        SyncedSubject.objects.create(raddai_id=52, name="Physics", grades=[10, 11, 12])
        response = self.client.get(reverse("dashboard:exam_create"))
        self.assertContains(response, 'id="exam-picker-data"')
        self.assertContains(response, "[10, 11, 12]")


class BlankQuestionAuthoringTests(AuthoringTestBase):
    def test_teacher_adds_a_fill_in_the_blank_question(self):
        self.client.post(reverse("dashboard:question_add", args=[self.exam.id]), {
            "kind": "blank", "text": "2 + 2 = ____", "marks": 10, "accepted_answers": "4\nfour\n",
        })
        question = self.exam.questions.get()
        self.assertEqual((question.kind, question.accepted_answers), ("blank", ["4", "four"]))
        self.assertFalse(question.choices.exists())
        self.assertEqual(self.exam.publish_problems(), [])

    def test_blank_question_needs_an_answer(self):
        response = self.client.post(reverse("dashboard:question_add", args=[self.exam.id]), {
            "kind": "blank", "text": "2 + 2 = ____", "marks": 10, "accepted_answers": "  ",
        })
        self.assertContains(response, "Type the correct answer.", status_code=400)
        self.assertFalse(self.exam.questions.exists())

    def test_switching_a_question_between_types(self):
        self.add_question(marks=10)
        question = self.exam.questions.get()
        edit = reverse("dashboard:question_edit", args=[self.exam.id, question.id])
        self.client.post(edit, {"kind": "blank", "text": "Name it", "marks": 10, "accepted_answers": "B"})
        question.refresh_from_db()
        self.assertEqual(question.kind, "blank")
        self.assertFalse(question.choices.exists())
        self.client.post(edit, {"kind": "mcq", "text": "Pick", "marks": 10, "correct": 1, "choice_1": "X", "choice_2": "Y"})
        question.refresh_from_db()
        self.assertEqual((question.kind, question.accepted_answers, question.choices.count()), ("mcq", [], 2))


class MultiClassExamTests(AuthoringTestBase):
    """One exam can be sat by several classes: same questions, code and results page."""

    def setUp(self):
        super().setUp()
        self.jss1b = SyncedClass.objects.create(raddai_id=2, name="JSS1 B", grade=7, section="B", academic_year=self.year)

    def post_exam(self, classes, subject=None, url=None, year=None):
        now = timezone.localtime()
        return self.client.post(url or reverse("dashboard:exam_create"), {
            "classes": [c.id for c in classes], "subject": (subject or self.subject).id,
            "academic_year": (year or self.year).id, "term": "first", "score_target": "ca1", "duration_minutes": 20,
            "opens_at": now.strftime("%Y-%m-%dT%H:%M"),
            "closes_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        })

    def test_create_for_two_classes(self):
        response = self.post_exam([self.jss1b, self.klass])
        exam = Exam.objects.exclude(pk=self.exam.pk).get()
        self.assertRedirects(response, reverse("dashboard:exam_edit", args=[exam.id]))
        self.assertEqual(set(exam.classes.all()), {self.klass, self.jss1b})
        self.assertEqual(exam.class_names, "JSS1 A, JSS1 B")
        self.assertIn(exam.klass, (self.klass, self.jss1b))  # old column still filled for the previous code

    def test_needs_at_least_one_class(self):
        response = self.post_exam([])
        self.assertContains(response, "Pick at least one class.")
        self.assertEqual(Exam.objects.count(), 1)

    def test_subject_must_be_taken_by_every_class(self):
        ss1 = SyncedClass.objects.create(raddai_id=3, name="SS1 A", grade=10, section="A", academic_year=self.year)
        juniors = SyncedSubject.objects.create(raddai_id=60, name="Basic Science", grades=[7, 8, 9])
        response = self.post_exam([self.klass, ss1], subject=juniors)
        self.assertContains(response, "SS1 A does not take Basic Science")
        self.assertEqual(Exam.objects.count(), 1)

    def test_classes_must_be_in_the_chosen_session(self):
        old_year = SyncedAcademicYear.objects.create(
            raddai_id=2, name="2024/2025", start_date="2024-09-01", end_date="2025-07-31"
        )
        old_class = SyncedClass.objects.create(raddai_id=4, name="JSS1 C", grade=7, section="C", academic_year=old_year)
        response = self.post_exam([self.klass, old_class])
        self.assertContains(response, "JSS1 C is not in 2025/2026")
        self.assertEqual(Exam.objects.count(), 1)

    def test_edit_adds_a_class(self):
        response = self.post_exam([self.klass, self.jss1b], url=reverse("dashboard:exam_edit", args=[self.exam.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(self.exam.classes.all()), {self.klass, self.jss1b})

    def test_classes_locked_once_a_student_started(self):
        self.start_submission()
        self.post_exam([self.klass, self.jss1b], url=reverse("dashboard:exam_edit", args=[self.exam.id]))
        self.assertEqual(list(self.exam.classes.all()), [self.klass])

    def test_lists_filter_and_results_show_every_class(self):
        self.exam.classes.add(self.jss1b)
        student = self.start_submission()  # JSS1 A
        other = User.objects.create_user(username="STU2", password="pw")
        SyncedStudent.objects.create(raddai_id=2, student_id="STU2", full_name="Bola", current_class=self.jss1b, user=other)

        listing = self.client.get(reverse("dashboard:exam_list"), {"class": self.jss1b.id})
        self.assertEqual(list(listing.context["exams"]), [self.exam])
        self.assertContains(listing, "JSS1 A, JSS1 B")

        results = self.client.get(reverse("dashboard:exam_results", args=[self.exam.id]))
        self.assertEqual(results.context["class_size"], 2)
        self.assertContains(results, "Students in these classes")
        self.assertContains(results, f"<td class=\"px-4 py-3 whitespace-nowrap\">{student.student.current_class}</td>", html=False)

    def test_publish_needs_a_class(self):
        self.exam.classes.clear()
        self.assertIn("Pick at least one class.", self.exam.publish_problems())

    def test_old_single_class_exams_get_their_class(self):
        from exams.apps import link_single_class_exams

        legacy = Exam.objects.create(
            subject=self.subject, klass=self.jss1b, academic_year=self.year, term="first", score_target="ca2",
            opens_at=timezone.now(), closes_at=timezone.now() + timedelta(hours=1),
        )
        link_single_class_exams(sender=None)
        self.assertEqual(list(legacy.classes.all()), [self.jss1b])
        self.assertEqual(list(self.exam.classes.all()), [self.klass])  # exams that already have classes are untouched


class DoneButtonTests(AuthoringTestBase):
    """Done lets a teacher finish: it adds anything typed, then says whether the exam is ready."""

    def done(self, data=None):
        return self.client.post(
            reverse("dashboard:question_add", args=[self.exam.id]), {"done": "1", **(data or {})}, follow=True
        )

    def test_done_with_empty_form_lists_whats_missing(self):
        response = self.done({"text": "", "marks": "", "choice_1": " "})
        self.assertRedirects(response, reverse("dashboard:exam_list"))
        self.assertFalse(self.exam.questions.exists())
        self.assertContains(response, "Saved as a draft. Still to do: Add at least one question.")

    def test_done_adds_the_typed_question_then_finishes(self):
        response = self.done({"text": "2+2?", "marks": 10, "correct": 2, "choice_1": "3", "choice_2": "4"})
        self.assertRedirects(response, reverse("dashboard:exam_list"))
        self.assertEqual(self.exam.questions.count(), 1)
        self.assertContains(response, "Question added. Mathematics is ready. Management will review and publish it.")

    def test_done_with_a_half_filled_question_shows_the_errors(self):
        response = self.done({"text": "2+2?", "marks": 10, "choice_1": "4"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Question not added", status_code=400)
        self.assertFalse(self.exam.questions.exists())

    def test_marks_short_of_the_total_are_reported(self):
        self.add_question(marks=4)
        response = self.done()
        self.assertContains(response, "Question marks add up to 4")

    def test_manager_goes_back_to_the_publish_button(self):
        self.add_question(marks=10)
        self.login_as_manager()
        response = self.done()
        self.assertRedirects(response, reverse("dashboard:exam_edit", args=[self.exam.id]))
        self.assertContains(response, "is ready — click Publish")

    def test_page_has_done_and_add_buttons(self):
        response = self.client.get(reverse("dashboard:exam_edit", args=[self.exam.id]))
        self.assertContains(response, 'name="done"')
        self.assertContains(response, "Add question</button>")


class ResetSubmissionTests(AuthoringTestBase):
    """Management can wipe a student's attempt so they can take the exam again."""

    def setUp(self):
        super().setUp()
        self.add_question(marks=10)
        self.submission = self.start_submission()
        Submission.objects.filter(pk=self.submission.pk).update(status="submitted", submitted_at=timezone.now(), score=0)
        Answer.objects.create(submission=self.submission, question=self.exam.questions.get())
        self.url = reverse("dashboard:reset_submission", args=[self.exam.id, self.submission.id])

    def test_manager_resets_an_attempt(self):
        self.login_as_manager()
        response = self.client.post(self.url, follow=True)
        self.assertRedirects(response, reverse("dashboard:exam_results", args=[self.exam.id]))
        self.assertContains(response, "attempt was reset")
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(Answer.objects.exists())

    def test_student_can_start_again_after_reset(self):
        self.login_as_manager()
        self.client.post(self.url)
        self.exam.is_published = True
        self.exam.access_code = "123456"
        self.exam.save()
        student = Client()
        student.login(username="STU1", password="pw")
        student.post(reverse("exams:start_exam", args=[self.exam.id]), {"access_code": "123456"})
        self.assertEqual(Submission.objects.get().status, Submission.Status.IN_PROGRESS)

    def test_score_already_on_main_portal_is_not_reset(self):
        Submission.objects.filter(pk=self.submission.pk).update(pushed_to_raddai=True)
        self.login_as_manager()
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "already on the main portal")
        self.assertTrue(Submission.objects.exists())

    def test_teacher_cannot_reset(self):
        self.client.post(self.url)
        self.assertTrue(Submission.objects.exists())

    def test_submission_must_belong_to_the_exam(self):
        self.login_as_manager()
        response = self.client.post(reverse("dashboard:reset_submission", args=[self.exam.id + 1, self.submission.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Submission.objects.exists())

    def test_reset_button_only_for_management(self):
        results = reverse("dashboard:exam_results", args=[self.exam.id])
        self.assertNotContains(self.client.get(results), self.url)
        self.login_as_manager()
        self.assertContains(self.client.get(results), self.url)


class DeleteScoresTests(AuthoringTestBase):
    """Management can delete every score for an exam at once so everyone can retake it."""

    def setUp(self):
        super().setUp()
        self.add_question(marks=10)
        question = self.exam.questions.get()
        self.students = []
        for i, (status, pushed) in enumerate([("submitted", False), ("submitted", False), ("in_progress", False), ("submitted", True)]):
            user = User.objects.create_user(username=f"S{i}", password="pw")
            student = SyncedStudent.objects.create(raddai_id=100 + i, student_id=f"S{i}", full_name=f"Student {i}", current_class=self.klass, user=user)
            submission = Submission.objects.create(student=student, exam=self.exam, status=status, score=5, pushed_to_raddai=pushed)
            Answer.objects.create(submission=submission, question=question)
        self.other_exam = Exam.objects.create(
            subject=self.subject, academic_year=self.year, term="first", score_target="ca2",
            opens_at=timezone.now(), closes_at=timezone.now() + timedelta(hours=1),
        )
        Submission.objects.create(student=student, exam=self.other_exam, status="submitted", score=7)
        self.url = reverse("dashboard:delete_scores", args=[self.exam.id])

    def test_manager_deletes_every_score_not_on_the_main_portal(self):
        self.login_as_manager()
        response = self.client.post(self.url, follow=True)
        self.assertRedirects(response, reverse("dashboard:results"))
        self.assertContains(response, "Deleted 3 scores for Mathematics")
        self.assertContains(response, "1 score is already on the main portal")
        self.assertEqual(list(self.exam.submissions.values_list("pushed_to_raddai", flat=True)), [True])
        self.assertEqual(Answer.objects.filter(submission__exam=self.exam).count(), 1)
        self.assertEqual(self.other_exam.submissions.count(), 1)  # other exams untouched

    def test_teacher_cannot_delete(self):
        self.client.post(self.url)
        self.assertEqual(self.exam.submissions.count(), 4)

    def test_get_does_nothing(self):
        self.login_as_manager()
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.exam.submissions.count(), 4)

    def test_results_page_has_delete_next_to_push(self):
        self.login_as_manager()
        response = self.client.get(reverse("dashboard:results"))
        self.assertContains(response, self.url)
        self.assertContains(response, "Push 2")
