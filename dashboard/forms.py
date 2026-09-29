from django import forms

from exams.models import Exam
from roster.models import SyncedAcademicYear, SyncedClass

MAX_CHOICES = 6
DATETIME_FORMAT = "%Y-%m-%dT%H:%M"


class ExamForm(forms.ModelForm):
    class Meta:
        model = Exam
        fields = [
            "subject", "klass", "academic_year", "term", "score_target",
            "duration_minutes", "opens_at", "closes_at",
        ]
        labels = {
            "klass": "Class",
            "score_target": "Result slot",
            "duration_minutes": "Duration (minutes)",
            "opens_at": "Opens at",
            "closes_at": "Closes at",
        }
        help_texts = {
            "score_target": "Which score on the main portal this test fills: First/Second/Third CA are out of 10, Examination is out of 70.",
        }
        widgets = {
            "opens_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DATETIME_FORMAT),
            "closes_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DATETIME_FORMAT),
        }

    # Fields that change what the grade means — frozen once any student has started.
    LOCKED_FIELDS = ["subject", "klass", "academic_year", "term", "score_target"]

    def __init__(self, *args, locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        active_year = SyncedAcademicYear.objects.filter(is_active=True).first()
        if active_year and not self.instance.pk:
            self.fields["academic_year"].initial = active_year
        self.fields["klass"].queryset = SyncedClass.objects.select_related("academic_year").order_by("grade", "section")
        for name in ("opens_at", "closes_at"):
            self.fields[name].input_formats = [DATETIME_FORMAT]
        if locked:
            for name in self.LOCKED_FIELDS:
                self.fields[name].disabled = True

    def clean(self):
        cleaned = super().clean()
        opens_at, closes_at = cleaned.get("opens_at"), cleaned.get("closes_at")
        if opens_at and closes_at and closes_at <= opens_at:
            self.add_error("closes_at", "Closing time must be after opening time.")
        klass, year = cleaned.get("klass"), cleaned.get("academic_year")
        if klass and year and klass.academic_year_id != year.id:
            self.add_error("klass", f"{klass} belongs to {klass.academic_year}, not {year}.")
        return cleaned


class QuestionForm(forms.Form):
    text = forms.CharField(label="Question", widget=forms.Textarea(attrs={"rows": 3}))
    marks = forms.IntegerField(min_value=1)
    correct = forms.IntegerField(
        min_value=1, max_value=MAX_CHOICES,
        error_messages={"required": "Pick the correct answer."},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for i in range(1, MAX_CHOICES + 1):
            self.fields[f"choice_{i}"] = forms.CharField(required=False, max_length=500)

    def choice_rows(self):
        """(index, letter, bound field) for rendering the option inputs in the template."""
        return [(i, "ABCDEF"[i - 1], self[f"choice_{i}"]) for i in range(1, MAX_CHOICES + 1)]

    def clean(self):
        cleaned = super().clean()
        filled = {
            i: cleaned.get(f"choice_{i}", "").strip()
            for i in range(1, MAX_CHOICES + 1)
            if cleaned.get(f"choice_{i}", "").strip()
        }
        if len(filled) < 2:
            raise forms.ValidationError("Enter at least 2 options.")
        correct = cleaned.get("correct")
        if correct and correct not in filled:
            raise forms.ValidationError("The correct answer must be one of the options you filled in.")
        cleaned["filled_choices"] = filled
        return cleaned

    @classmethod
    def initial_for(cls, question):
        initial = {"text": question.text, "marks": question.marks}
        for i, choice in enumerate(question.choices.all(), start=1):
            initial[f"choice_{i}"] = choice.text
            if choice.is_correct:
                initial["correct"] = i
        return initial
