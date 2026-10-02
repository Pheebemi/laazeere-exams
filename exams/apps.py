from django.apps import AppConfig
from django.db.models.signals import post_migrate


def link_single_class_exams(sender=None, apps=None, **kwargs):
    """
    Give any exam that has only the old single class its `classes` entry.
    Migration 0010 does this once, but preview builds migrate the shared
    database before the new code is live, so an exam made in between by the
    old code would otherwise have no classes. Runs after every migrate.
    """
    if apps is None:
        from django.apps import apps
    try:
        Exam = apps.get_model("exams", "Exam")
    except LookupError:
        return
    if not any(field.name == "classes" for field in Exam._meta.get_fields()):
        return  # migrated to a point before the classes table existed
    for exam in Exam.objects.filter(klass__isnull=False, classes__isnull=True):
        exam.classes.add(exam.klass_id)


class ExamsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "exams"

    def ready(self):
        post_migrate.connect(link_single_class_exams, sender=self)
