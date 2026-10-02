from io import BytesIO
from uuid import uuid4

from django import forms
from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from exams.models import Exam, Question, normalize_answer
from roster.models import SyncedAcademicYear, SyncedClass, SyncedSubject

MAX_CHOICES = 6
DATETIME_FORMAT = "%Y-%m-%dT%H:%M"

# Vercel rejects request bodies over ~4.5 MB before Django sees them. The
# browser shrinks big photos before uploading (see _question_fields.html);
# this is the server-side backstop.
MAX_IMAGE_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_IMAGE_SIDE = 1600
IMAGE_TYPES = "image/jpeg,image/png,image/webp,image/gif"


def shrink_image(upload):
    """
    Re-encode an uploaded picture as a WebP at most MAX_IMAGE_SIDE pixels on
    its longest side — typically 100–200 KB instead of a multi-MB phone
    photo, so a whole class can load it at once on mobile data.
    """
    with Image.open(upload) as original:
        image = ImageOps.exif_transpose(original)  # phones record rotation in EXIF, not in the pixels
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        image = image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB")
        buffer = BytesIO()
        image.save(buffer, "WEBP", quality=85, method=4)
    return ContentFile(buffer.getvalue(), name=f"{uuid4().hex}.webp")


class ExamForm(forms.ModelForm):
    class Meta:
        model = Exam
        fields = [
            "klass", "subject", "academic_year", "term", "score_target",
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
        self.fields["subject"].queryset = SyncedSubject.objects.order_by("name")
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
        subject = cleaned.get("subject")
        if klass and subject and not subject.is_offered_to(klass):
            self.add_error("subject", f"{klass} does not take {subject}.")
        return cleaned

    def picker_data(self):
        """What the page needs to show only the chosen class's subjects and fill in its session."""
        return {
            "classes": {str(c.pk): {"grade": c.grade, "year": c.academic_year_id} for c in self.fields["klass"].queryset},
            "subjects": {str(s.pk): s.grades for s in self.fields["subject"].queryset},
        }


MAX_ACCEPTED_ANSWERS = 20


class QuestionForm(forms.Form):
    kind = forms.ChoiceField(
        label="Question type", choices=Question.Kind.choices, required=False, initial=Question.Kind.MULTIPLE_CHOICE
    )
    text = forms.CharField(label="Question", widget=forms.Textarea(attrs={"rows": 3}))
    marks = forms.IntegerField(min_value=1)
    image = forms.FileField(
        required=False, label="Picture (optional)", widget=forms.FileInput(attrs={"accept": IMAGE_TYPES})
    )
    remove_image = forms.BooleanField(required=False)
    correct = forms.IntegerField(min_value=1, max_value=MAX_CHOICES, required=False)
    accepted_answers = forms.CharField(
        label="Correct answer(s)", required=False, widget=forms.Textarea(attrs={"rows": 3}),
        help_text="One per line if more than one answer is right (e.g. Abuja, FCT Abuja). "
                  "Capital letters, extra spaces and a full stop at the end don't matter.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for i in range(1, MAX_CHOICES + 1):
            self.fields[f"choice_{i}"] = forms.CharField(required=False, max_length=500)

    def choice_rows(self):
        """(index, letter, bound field) for rendering the option inputs in the template."""
        return [(i, "ABCDEF"[i - 1], self[f"choice_{i}"]) for i in range(1, MAX_CHOICES + 1)]

    def clean_image(self):
        upload = self.cleaned_data.get("image")
        if not upload:
            return None
        if not settings.PICTURE_UPLOADS_ENABLED:
            raise forms.ValidationError(
                "Pictures aren't switched on yet — ask the admin to connect Vercel Blob storage."
            )
        if upload.size > MAX_IMAGE_UPLOAD_BYTES:
            raise forms.ValidationError("That picture is too big (max 4 MB). Try a smaller photo or a screenshot.")
        try:
            return shrink_image(upload)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise forms.ValidationError("That file isn't a picture we can read. Use a JPG, PNG or WebP image.")

    def clean(self):
        cleaned = super().clean()
        cleaned["kind"] = cleaned.get("kind") or Question.Kind.MULTIPLE_CHOICE
        if cleaned["kind"] == Question.Kind.BLANK:
            answers = [line.strip() for line in cleaned.get("accepted_answers", "").splitlines() if line.strip()]
            if not any(normalize_answer(a) for a in answers):
                self.add_error("accepted_answers", "Type the correct answer.")
            elif len(answers) > MAX_ACCEPTED_ANSWERS:
                self.add_error("accepted_answers", f"Keep it to {MAX_ACCEPTED_ANSWERS} answers or fewer.")
            elif any(len(a) > 200 for a in answers):
                self.add_error("accepted_answers", "Each answer must be 200 characters or fewer.")
            cleaned["answers_list"] = answers
            cleaned["filled_choices"] = {}
            return cleaned

        filled = {
            i: cleaned.get(f"choice_{i}", "").strip()
            for i in range(1, MAX_CHOICES + 1)
            if cleaned.get(f"choice_{i}", "").strip()
        }
        if len(filled) < 2:
            raise forms.ValidationError("Enter at least 2 options.")
        correct = cleaned.get("correct")
        if not correct:
            self.add_error("correct", "Pick the correct answer.")
        elif correct not in filled:
            raise forms.ValidationError("The correct answer must be one of the options you filled in.")
        cleaned["filled_choices"] = filled
        return cleaned

    @classmethod
    def initial_for(cls, question):
        initial = {
            "kind": question.kind,
            "text": question.text,
            "marks": question.marks,
            "accepted_answers": "\n".join(question.accepted_answers),
        }
        for i, choice in enumerate(question.choices.all(), start=1):
            initial[f"choice_{i}"] = choice.text
            if choice.is_correct:
                initial["correct"] = i
        return initial
