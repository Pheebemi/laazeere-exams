from django.contrib import admin

from .models import SyncedAcademicYear, SyncedClass, SyncedStaff, SyncedStudent, SyncedSubject


@admin.register(SyncedAcademicYear)
class SyncedAcademicYearAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "start_date", "end_date")


@admin.register(SyncedClass)
class SyncedClassAdmin(admin.ModelAdmin):
    list_display = ("name", "grade", "section", "academic_year")
    list_filter = ("academic_year",)


@admin.register(SyncedSubject)
class SyncedSubjectAdmin(admin.ModelAdmin):
    list_display = ("name", "code")


@admin.register(SyncedStudent)
class SyncedStudentAdmin(admin.ModelAdmin):
    list_display = ("full_name", "student_id", "current_class", "is_active")
    list_filter = ("current_class", "is_active")
    search_fields = ("full_name", "student_id")


@admin.register(SyncedStaff)
class SyncedStaffAdmin(admin.ModelAdmin):
    list_display = ("full_name", "staff_id", "designation", "is_active")
    search_fields = ("full_name", "staff_id")
