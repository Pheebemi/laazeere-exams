from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from roster.models import SyncedManager, SyncedStudent

User = get_user_model()


def roster_payload(managers):
    return {
        "academic_years": [{"id": 1, "name": "2026/2027", "start_date": "2026-09-01", "end_date": "2027-07-31", "is_active": True}],
        "classes": [{"id": 1, "name": "JSS 1 A", "grade": 7, "section": "A", "academic_year_id": 1}],
        "subjects": [{"id": 1, "name": "Mathematics", "code": "MTH"}],
        "students": [{"id": 1, "student_id": "LAZ-JS-0001", "full_name": "Amina Bello", "current_class_id": 1, "is_active": True}],
        "staff": [],
        "managers": managers,
    }


@override_settings(EXAM_PORTAL_API_KEY="key", RADDAI_API_BASE_URL="https://raddai.test/api")
class SyncRosterTests(TestCase):
    def sync(self, payload):
        with mock.patch("roster.management.commands.sync_roster.requests.get") as get:
            get.return_value.json.return_value = payload
            output = StringIO()
            call_command("sync_roster", apply=True, stdout=output)
        return output.getvalue()

    def test_creates_managers_with_their_username_as_starter_password(self):
        self.sync(roster_payload([{"id": 7, "username": "bursar", "full_name": "Mrs Bursar", "role": "management", "is_active": True}]))
        manager = SyncedManager.objects.get()
        self.assertEqual((manager.username, manager.role), ("bursar", "management"))
        self.assertTrue(self.client.login(username="bursar", password="bursar"))

    def test_starter_password_is_upgraded_to_full_strength_on_first_login(self):
        self.sync(roster_payload([]))
        user = SyncedStudent.objects.get().user
        self.assertTrue(user.password.startswith("pbkdf2_sha256_starter$"))
        self.assertTrue(self.client.login(username="LAZ-JS-0001", password="LAZ-JS-0001"))
        user.refresh_from_db()
        self.assertTrue(user.password.startswith("pbkdf2_sha256$"))

    def test_manager_removed_on_main_portal_loses_access(self):
        row = {"id": 7, "username": "bursar", "full_name": "Mrs Bursar", "role": "management", "is_active": True}
        self.sync(roster_payload([row]))
        self.sync(roster_payload([]))
        manager = SyncedManager.objects.get()
        self.assertFalse(manager.is_active)
        self.assertFalse(manager.user.is_active)

    def test_never_turns_an_existing_account_into_a_manager(self):
        payload = roster_payload([{"id": 7, "username": "LAZ-JS-0001", "full_name": "Clash", "role": "admin", "is_active": True}])
        output = self.sync(payload)
        self.assertFalse(SyncedManager.objects.exists())
        self.assertIn("skip", output)

    def test_second_sync_changes_nothing(self):
        self.sync(roster_payload([]))
        output = self.sync(roster_payload([]))
        self.assertIn("academic_years: 0 to create, 0 to update", output)
        self.assertIn("students: 0 to create, 0 to update", output)
