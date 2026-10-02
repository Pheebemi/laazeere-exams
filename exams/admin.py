from django.contrib import admin

from .models import Answer, Choice, Exam, Question, Submission


class ChoiceInline(admin.TabularInline):
    model = Choice
    extra = 4


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("text", "exam", "marks", "order")
    list_filter = ("exam",)
    inlines = [ChoiceInline]


class QuestionInline(admin.StackedInline):
    model = Question
    extra = 1
    show_change_link = True


@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ("subject", "class_names", "term", "score_target", "total_marks", "opens_at", "closes_at", "is_published")
    list_filter = ("classes", "term", "score_target", "is_published")
    exclude = ("klass",)
    filter_horizontal = ("classes",)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("classes")
    inlines = [QuestionInline]


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ("student", "exam", "status", "score", "pushed_to_raddai")
    list_filter = ("exam", "status", "pushed_to_raddai")
    readonly_fields = [f.name for f in Submission._meta.fields]


admin.site.register(Answer)
