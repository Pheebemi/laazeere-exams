"""
Pull academic years, classes, subjects, students, staff, and management
accounts from raddai-backend and mirror them locally.

Dry-run by default — pass --apply to actually write changes. Mirrors
repair_fee_payments.py's shape on raddai-backend: build a diff first,
print it, only write inside a transaction when --apply is passed.
"""

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.hashers import StarterPasswordHasher
from roster.models import (
    SyncedAcademicYear, SyncedClass, SyncedManager, SyncedStaff, SyncedStudent, SyncedSubject,
)

User = get_user_model()


def _new_user(username, is_active, **extra):
    """New account whose starter password is its own username — see core/hashers.py for why the fast hasher."""
    return User.objects.create(
        username=username,
        password=make_password(username, hasher=StarterPasswordHasher.algorithm),
        is_active=is_active,
        **extra,
    )


def _username_taken(username, by_other_than=None):
    accounts = User.objects.filter(username=username)
    if by_other_than is not None:
        accounts = accounts.exclude(pk=by_other_than.pk)
    return accounts.exists()


def _save_person(existing, fields, username):
    """Apply synced fields to a student/staff/manager row and keep its login account in step with it."""
    for k, v in fields.items():
        setattr(existing, k, v)
    existing.save()
    existing.user.username = username
    existing.user.is_active = fields["is_active"]
    existing.user.save(update_fields=["username", "is_active"])


class Command(BaseCommand):
    help = "Sync roster data (academic years, classes, subjects, students, staff, managers) from raddai-backend."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the sync. Without this, only show a dry-run.")
        parser.add_argument("--academic-year", type=int, help="Only sync data for one raddai academic year id.")

    def handle(self, *args, **options):
        if not settings.EXAM_PORTAL_API_KEY:
            raise CommandError("EXAM_PORTAL_API_KEY is not set — cannot authenticate to raddai-backend.")

        url = f"{settings.RADDAI_API_BASE_URL}/exam-portal/roster/"
        params = {}
        if options.get("academic_year"):
            params["academic_year"] = options["academic_year"]

        response = requests.get(
            url, params=params, headers={"X-Exam-Portal-Key": settings.EXAM_PORTAL_API_KEY}, timeout=30
        )
        response.raise_for_status()
        payload = response.json()

        apply = options["apply"]
        mode = "Applying" if apply else "Dry run"
        self.stdout.write(f"{mode} roster sync from {url}\n")

        with transaction.atomic():
            year_map = self._sync_academic_years(payload.get("academic_years", []), apply)
            class_map = self._sync_classes(payload.get("classes", []), year_map, apply)
            self._sync_subjects(payload.get("subjects", []), apply)
            self._sync_students(payload.get("students", []), class_map, apply)
            self._sync_staff(payload.get("staff", []), apply)
            if "managers" in payload:
                # Older raddai-backend builds don't send this key — leave managers alone then,
                # rather than reading its absence as "everyone lost management access".
                self._sync_managers(payload["managers"], apply)

            if not apply:
                # Roll back everything — a dry run must never persist writes.
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS("Done." if apply else "Dry run complete — nothing was written."))

    def _sync_academic_years(self, rows, apply):
        id_map = {}
        created = updated = 0
        for row in rows:
            existing = SyncedAcademicYear.objects.filter(raddai_id=row["id"]).first()
            fields = {
                "name": row["name"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "is_active": row["is_active"],
            }
            if existing:
                # start/end dates arrive as ISO strings but are stored as dates — compare like with like.
                changed = any(
                    (getattr(existing, k).isoformat() if k in ("start_date", "end_date") else getattr(existing, k)) != v
                    for k, v in fields.items()
                )
                if changed:
                    updated += 1
                    self.stdout.write(f"  [academic_year] update: {row['name']}")
                    if apply:
                        for k, v in fields.items():
                            setattr(existing, k, v)
                        existing.save()
                id_map[row["id"]] = existing.pk
            else:
                created += 1
                self.stdout.write(f"  [academic_year] create: {row['name']}")
                if apply:
                    obj = SyncedAcademicYear.objects.create(raddai_id=row["id"], **fields)
                    id_map[row["id"]] = obj.pk
        self.stdout.write(f"academic_years: {created} to create, {updated} to update\n")
        return id_map

    def _sync_classes(self, rows, year_map, apply):
        id_map = {}
        created = updated = 0
        for row in rows:
            year_pk = year_map.get(row["academic_year_id"])
            if year_pk is None and apply:
                # Year wasn't in this run's payload (e.g. filtered out) — look it up directly.
                year = SyncedAcademicYear.objects.filter(raddai_id=row["academic_year_id"]).first()
                year_pk = year.pk if year else None
            existing = SyncedClass.objects.filter(raddai_id=row["id"]).first()
            fields = {"name": row["name"], "grade": row["grade"], "section": row.get("section", "")}
            if existing:
                changed = any(getattr(existing, k) != v for k, v in fields.items())
                if changed:
                    updated += 1
                    self.stdout.write(f"  [class] update: {row['name']}")
                    if apply:
                        for k, v in fields.items():
                            setattr(existing, k, v)
                        existing.save()
                id_map[row["id"]] = existing.pk
            else:
                created += 1
                self.stdout.write(f"  [class] create: {row['name']}")
                if apply and year_pk:
                    obj = SyncedClass.objects.create(raddai_id=row["id"], academic_year_id=year_pk, **fields)
                    id_map[row["id"]] = obj.pk
        self.stdout.write(f"classes: {created} to create, {updated} to update\n")
        return id_map

    def _sync_subjects(self, rows, apply):
        created = updated = 0
        for row in rows:
            existing = SyncedSubject.objects.filter(raddai_id=row["id"]).first()
            fields = {"name": row["name"], "code": row.get("code") or ""}
            if existing:
                changed = any(getattr(existing, k) != v for k, v in fields.items())
                if changed:
                    updated += 1
                    self.stdout.write(f"  [subject] update: {row['name']}")
                    if apply:
                        for k, v in fields.items():
                            setattr(existing, k, v)
                        existing.save()
            else:
                created += 1
                self.stdout.write(f"  [subject] create: {row['name']}")
                if apply:
                    SyncedSubject.objects.create(raddai_id=row["id"], **fields)
        self.stdout.write(f"subjects: {created} to create, {updated} to update\n")

    def _skip_clash(self, kind, username, full_name):
        """
        Two people can't share a login name. Skip the newcomer (never merge them
        into, or rename them onto, someone else's account) and carry on — one
        clash must not abort the whole sync.
        """
        self.stdout.write(
            f"  [{kind}] skip: {full_name} — login name {username} is already used by another account"
        )

    def _sync_students(self, rows, class_map, apply):
        created = updated = skipped = 0
        for row in rows:
            class_pk = class_map.get(row.get("current_class_id"))
            if class_pk is None and row.get("current_class_id") and apply:
                cls = SyncedClass.objects.filter(raddai_id=row["current_class_id"]).first()
                class_pk = cls.pk if cls else None

            existing = SyncedStudent.objects.select_related("user").filter(raddai_id=row["id"]).first()
            username = row["student_id"]
            fields = {
                "student_id": username,
                "full_name": row["full_name"],
                "current_class_id": class_pk,
                "is_active": row.get("is_active", True),
            }
            if existing:
                if not any(getattr(existing, k) != v for k, v in fields.items()):
                    continue
                if _username_taken(username, by_other_than=existing.user):
                    skipped += 1
                    self._skip_clash("student", username, row["full_name"])
                    continue
                updated += 1
                self.stdout.write(f"  [student] update: {row['full_name']} ({username})")
                if apply:
                    _save_person(existing, fields, username)
            elif _username_taken(username):
                skipped += 1
                self._skip_clash("student", username, row["full_name"])
            else:
                created += 1
                self.stdout.write(f"  [student] create: {row['full_name']} ({username})")
                if apply:
                    user = _new_user(username, fields["is_active"])
                    SyncedStudent.objects.create(raddai_id=row["id"], user=user, **fields)
        self.stdout.write(f"students: {created} to create, {updated} to update, {skipped} skipped\n")

    def _sync_staff(self, rows, apply):
        created = updated = skipped = 0
        for row in rows:
            existing = SyncedStaff.objects.select_related("user").filter(raddai_id=row["id"]).first()
            username = row["staff_id"]
            fields = {
                "staff_id": username,
                "full_name": row["full_name"],
                "designation": row.get("designation") or "",
                "is_active": row.get("is_active", True),
            }
            if existing:
                if not any(getattr(existing, k) != v for k, v in fields.items()):
                    continue
                if _username_taken(username, by_other_than=existing.user):
                    skipped += 1
                    self._skip_clash("staff", username, row["full_name"])
                    continue
                updated += 1
                self.stdout.write(f"  [staff] update: {row['full_name']} ({username})")
                if apply:
                    _save_person(existing, fields, username)
            elif _username_taken(username):
                skipped += 1
                self._skip_clash("staff", username, row["full_name"])
            else:
                created += 1
                self.stdout.write(f"  [staff] create: {row['full_name']} ({username})")
                if apply:
                    user = _new_user(username, fields["is_active"], is_staff=True)
                    SyncedStaff.objects.create(raddai_id=row["id"], user=user, **fields)
        self.stdout.write(f"staff: {created} to create, {updated} to update, {skipped} skipped\n")

    def _sync_managers(self, rows, apply):
        created = updated = removed = skipped = 0
        seen = set()
        for row in rows:
            seen.add(row["id"])
            existing = SyncedManager.objects.select_related("user").filter(raddai_id=row["id"]).first()
            username = row["username"]
            fields = {
                "username": username,
                "full_name": row["full_name"],
                "role": row.get("role") or "",
                "is_active": row.get("is_active", True),
            }
            if existing:
                if not any(getattr(existing, k) != v for k, v in fields.items()):
                    continue
                if _username_taken(username, by_other_than=existing.user):
                    skipped += 1
                    self._skip_clash("manager", username, row["full_name"])
                    continue
                updated += 1
                self.stdout.write(f"  [manager] update: {row['full_name']} ({username})")
                if apply:
                    _save_person(existing, fields, username)
            elif _username_taken(username):
                # e.g. a student/staff ID equal to a manager's username — never hand an
                # existing account management rights by accident.
                skipped += 1
                self._skip_clash("manager", username, row["full_name"])
            else:
                created += 1
                self.stdout.write(f"  [manager] create: {row['full_name']} ({username})")
                if apply:
                    user = _new_user(username, fields["is_active"])
                    SyncedManager.objects.create(raddai_id=row["id"], user=user, **fields)

        # Someone who is no longer Management/Admin on the main portal must lose
        # dashboard access here too, so disable accounts missing from the payload.
        for gone in SyncedManager.objects.filter(is_active=True).exclude(raddai_id__in=seen).select_related("user"):
            removed += 1
            self.stdout.write(f"  [manager] disable: {gone.full_name} ({gone.username}) is no longer management")
            if apply:
                gone.is_active = False
                gone.save(update_fields=["is_active"])
                gone.user.is_active = False
                gone.user.save(update_fields=["is_active"])
        self.stdout.write(
            f"managers: {created} to create, {updated} to update, {removed} to disable, {skipped} skipped\n"
        )
