from django.conf import settings
from django.db import models


class SyncedAcademicYear(models.Model):
    """Mirrors AcademicYear on raddai-backend. Source of truth is raddai — never edited here directly."""

    raddai_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=50)
    start_date = models.DateField()
    end_date = models.DateField()
    is_active = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class SyncedClass(models.Model):
    raddai_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=50)
    grade = models.IntegerField()
    section = models.CharField(max_length=10, blank=True)
    academic_year = models.ForeignKey(SyncedAcademicYear, on_delete=models.CASCADE, related_name="classes")

    def __str__(self):
        return self.name


class SyncedSubject(models.Model):
    raddai_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20, blank=True, default="")

    def __str__(self):
        return self.name


class SyncedStudent(models.Model):
    raddai_id = models.PositiveIntegerField(unique=True)
    student_id = models.CharField(max_length=20, unique=True)
    full_name = models.CharField(max_length=150)
    current_class = models.ForeignKey(
        SyncedClass, on_delete=models.SET_NULL, null=True, blank=True, related_name="students"
    )
    is_active = models.BooleanField(default=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="syncedstudent")
    must_change_password = models.BooleanField(default=True)
    # The one browser session allowed to use this account. Logging in anywhere
    # else replaces it, which signs the older device out (exams.decorators).
    current_session_key = models.CharField(max_length=40, blank=True, default="")

    def __str__(self):
        return f"{self.full_name} ({self.student_id})"


class SyncedStaff(models.Model):
    raddai_id = models.PositiveIntegerField(unique=True)
    staff_id = models.CharField(max_length=20, unique=True)
    full_name = models.CharField(max_length=150)
    designation = models.CharField(max_length=50, blank=True, default="")
    is_active = models.BooleanField(default=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="syncedstaff")
    must_change_password = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.full_name} ({self.staff_id})"


class SyncedManager(models.Model):
    """
    A Management/Admin account on raddai-backend. Gets the exam portal's admin
    dashboard: publishing exams, pushing results, viewing the whole roster.
    Logs in with the same username as the main portal.
    """

    raddai_id = models.PositiveIntegerField(unique=True)
    username = models.CharField(max_length=150, unique=True)
    full_name = models.CharField(max_length=150)
    role = models.CharField(max_length=20, blank=True, default="")
    is_active = models.BooleanField(default=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="syncedmanager")
    must_change_password = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.full_name} ({self.username})"
