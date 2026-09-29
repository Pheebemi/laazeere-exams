from django.db import models
from django.utils import timezone

from roster.models import SyncedAcademicYear, SyncedClass, SyncedStudent, SyncedSubject


class Exam(models.Model):
    class Term(models.TextChoices):
        FIRST = "first", "First Term"
        SECOND = "second", "Second Term"
        THIRD = "third", "Third Term"
        FINAL = "final", "Final Exam"

    class ScoreTarget(models.TextChoices):
        CA1 = "ca1", "CA 1"
        CA2 = "ca2", "CA 2"
        CA3 = "ca3", "CA 3"
        EXAM = "exam", "Exam"

    # Max marks per slot on raddai's Result model — CA slots are out of 10,
    # the exam slot is out of 70. Keeping this here (not hardcoded elsewhere)
    # means there's one place to change if that ever differs.
    SCORE_TARGET_MAX_MARKS = {
        ScoreTarget.CA1: 10,
        ScoreTarget.CA2: 10,
        ScoreTarget.CA3: 10,
        ScoreTarget.EXAM: 70,
    }

    RADDAI_FIELD_NAMES = {
        ScoreTarget.CA1: "ca1_score",
        ScoreTarget.CA2: "ca2_score",
        ScoreTarget.CA3: "ca3_score",
        ScoreTarget.EXAM: "exam_score",
    }

    subject = models.ForeignKey(SyncedSubject, on_delete=models.CASCADE, related_name="exams")
    klass = models.ForeignKey(SyncedClass, on_delete=models.CASCADE, related_name="exams")
    academic_year = models.ForeignKey(SyncedAcademicYear, on_delete=models.CASCADE, related_name="exams")
    term = models.CharField(max_length=10, choices=Term.choices)
    score_target = models.CharField(max_length=10, choices=ScoreTarget.choices)
    total_marks = models.PositiveIntegerField(editable=False, default=0)
    duration_minutes = models.PositiveIntegerField(default=30)
    opens_at = models.DateTimeField()
    closes_at = models.DateTimeField()
    is_published = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        self.total_marks = self.SCORE_TARGET_MAX_MARKS[self.score_target]
        super().save(*args, **kwargs)

    @property
    def raddai_score_field(self):
        return self.RADDAI_FIELD_NAMES[self.score_target]

    def is_open(self, now=None):
        now = now or timezone.now()
        return self.is_published and self.opens_at <= now <= self.closes_at

    @property
    def is_locked(self):
        """Once any student has started, questions/marks can't change without corrupting grading."""
        return self.submissions.exists()

    @property
    def allocated_marks(self):
        return sum(q.marks for q in self.questions.all())

    def publish_problems(self):
        problems = []
        questions = list(self.questions.prefetch_related("choices"))
        if not questions:
            problems.append("Add at least one question.")
        allocated = sum(q.marks for q in questions)
        if questions and allocated != self.total_marks:
            problems.append(
                f"Question marks add up to {allocated}, but this {self.get_score_target_display()} "
                f"must total exactly {self.total_marks}."
            )
        for i, q in enumerate(questions, start=1):
            choices = list(q.choices.all())
            if len(choices) < 2:
                problems.append(f"Question {i} needs at least 2 options.")
            if sum(1 for c in choices if c.is_correct) != 1:
                problems.append(f"Question {i} must have exactly one correct answer.")
        if self.closes_at <= self.opens_at:
            problems.append("Closing time must be after opening time.")
        return problems

    def __str__(self):
        return f"{self.subject} - {self.klass} - {self.get_score_target_display()}"


class Question(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="questions")
    text = models.TextField()
    order = models.PositiveIntegerField(default=0)
    marks = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.text[:60]


class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="choices")
    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.text[:60]


class Submission(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        SUBMITTED = "submitted", "Submitted"

    student = models.ForeignKey(SyncedStudent, on_delete=models.CASCADE, related_name="submissions")
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="submissions")
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.IN_PROGRESS)
    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    pushed_to_raddai = models.BooleanField(default=False)
    pushed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ["student", "exam"]

    def __str__(self):
        return f"{self.student} - {self.exam}"


class Answer(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="answers")
    selected_choice = models.ForeignKey(Choice, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        unique_together = ["submission", "question"]
