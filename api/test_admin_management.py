from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from api import models as ms

User = get_user_model()


class AdminManagementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner",
            email="owner-admin-test@example.com",
            password="test-password",
        )
        self.staff = User.objects.create_user(
            username="staff",
            email="staff-admin-test@example.com",
            password="test-password",
            is_staff=True,
        )
        self.superadmin = User.objects.create_superuser(
            username="root",
            email="root-admin-test@example.com",
            password="test-password",
        )
        self.task = ms.Task.objects.create(
            user=self.owner,
            title="Publicación administrable",
            description="Contenido de prueba",
        )
        self.client = APIClient()

    def test_staff_can_search_and_edit_or_delete_any_task(self):
        self.client.force_authenticate(self.staff)
        listing = self.client.get(reverse("admin-tasks"), {"search": "administrable"})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 1)

        detail_url = reverse("task-detail", kwargs={"id": self.task.id})
        updated = self.client.patch(detail_url, {"title": "Editada por staff"}, format="json")
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["title"], "Editada por staff")

        deleted = self.client.delete(detail_url)
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(ms.Task.objects.filter(id=self.task.id).exists())

    def test_non_admin_cannot_use_admin_listing_or_delete_another_users_task(self):
        self.client.force_authenticate(self.owner)
        listing = self.client.get(reverse("admin-users"))
        self.assertEqual(listing.status_code, 403)

        other_user = User.objects.create_user(
            username="other",
            email="other-admin-test@example.com",
            password="test-password",
        )
        other_task = ms.Task.objects.create(user=other_user, title="No autorizado")
        response = self.client.delete(reverse("task-detail", kwargs={"id": other_task.id}))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ms.Task.objects.filter(id=other_task.id).exists())

    def test_only_superadmin_can_suspend_and_manage_staff_role(self):
        self.client.force_authenticate(self.staff)
        forbidden = self.client.post(
            reverse("admin-user-active", kwargs={"user_id": self.owner.id}),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_authenticate(self.superadmin)
        suspended = self.client.post(
            reverse("admin-user-active", kwargs={"user_id": self.owner.id}),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(suspended.status_code, 200)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)

        promoted = self.client.post(
            reverse("admin-user-role", kwargs={"user_id": self.owner.id}),
            {"is_staff": True},
            format="json",
        )
        self.assertEqual(promoted.status_code, 200)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_staff)

    def test_staff_can_verify_but_cannot_change_roles(self):
        self.client.force_authenticate(self.staff)
        verified = self.client.post(
            reverse("admin-verify-user", kwargs={"user_id": self.owner.id}),
            {"is_verified": True},
            format="json",
        )
        self.assertEqual(verified.status_code, 200)
        self.owner.profile.refresh_from_db()
        self.assertTrue(self.owner.profile.is_verified)

        role_url = reverse("admin-user-role", kwargs={"user_id": self.owner.id})
        for payload in ({"is_staff": True}, {"is_superuser": True}):
            self.assertEqual(self.client.post(role_url, payload, format="json").status_code, 403)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_staff or self.owner.is_superuser)

    def test_superadmin_can_make_and_remove_admins(self):
        self.client.force_authenticate(self.superadmin)
        role_url = reverse("admin-user-role", kwargs={"user_id": self.owner.id})

        promoted = self.client.post(role_url, {"is_superuser": True}, format="json")
        self.assertEqual(promoted.status_code, 200, promoted.data)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_superuser and self.owner.is_staff)

        # Un admin no pierde staff sin antes dejar de ser admin.
        self.assertEqual(self.client.post(role_url, {"is_staff": False}, format="json").status_code, 400)

        demoted = self.client.post(role_url, {"is_superuser": False}, format="json")
        self.assertEqual(demoted.status_code, 200, demoted.data)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_superuser)
        self.assertTrue(self.owner.is_staff)

    def test_superadmin_cannot_change_own_role(self):
        self.client.force_authenticate(self.superadmin)
        response = self.client.post(
            reverse("admin-user-role", kwargs={"user_id": self.superadmin.id}),
            {"is_superuser": False},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.superadmin.refresh_from_db()
        self.assertTrue(self.superadmin.is_superuser)

    def test_superadmin_cannot_suspend_self_or_another_superadmin(self):
        self.client.force_authenticate(self.superadmin)
        response = self.client.post(
            reverse("admin-user-active", kwargs={"user_id": self.superadmin.id}),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_user_can_report_task_and_staff_can_resolve_report(self):
        self.client.force_authenticate(self.owner)
        report = self.client.post(
            reverse("user-report-create"),
            {
                "target_type": "task",
                "target_id": str(self.task.id),
                "reason": "Contenido que requiere revisión.",
            },
            format="json",
        )
        self.assertEqual(report.status_code, 201, report.data)
        self.assertEqual(report.data["status"], ms.UserReport.STATUS_PENDING)

        self.client.force_authenticate(self.staff)
        listing = self.client.get(reverse("user-reports"), {"status": "pending"})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 1)

        resolution = self.client.patch(
            reverse("admin-report-update", kwargs={"report_id": str(report.data["id"])}),
            {"status": "resolved", "resolution_note": "Revisado por moderación."},
            format="json",
        )
        self.assertEqual(resolution.status_code, 200, resolution.data)
        self.assertEqual(resolution.data["status"], "resolved")
        self.assertEqual(resolution.data["resolution_note"], "Revisado por moderación.")

    def test_report_rejects_invalid_targets_and_duplicate_pending_reports(self):
        self.client.force_authenticate(self.owner)
        payload = {
            "target_type": "task",
            "target_id": str(self.task.id),
            "reason": "Motivo",
        }
        first = self.client.post(reverse("user-report-create"), payload, format="json")
        second = self.client.post(reverse("user-report-create"), payload, format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ms.UserReport.objects.count(), 1)

        payload["target_id"] = "not-a-uuid"
        invalid = self.client.post(reverse("user-report-create"), payload, format="json")
        self.assertEqual(invalid.status_code, 400)

    def test_staff_can_remove_reported_comment_and_resolve_it(self):
        comment = ms.NewPeticionCommentPost.objects.create(
            created_by=self.owner,
            post=self.task,
            text="Comentario moderable",
        )
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            reverse("user-report-create"),
            {
                "target_type": "comment",
                "target_id": str(comment.id),
                "reason": "Comentario abusivo.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

        self.client.force_authenticate(self.staff)
        removed = self.client.delete(
            reverse("admin-report-delete-content", kwargs={"report_id": response.data["id"]})
        )
        self.assertEqual(removed.status_code, 200, removed.data)
        self.assertEqual(removed.data["status"], "resolved")
        self.assertFalse(ms.NewPeticionCommentPost.objects.filter(id=comment.id).exists())

    def test_superadmin_can_suspend_reported_account_and_resolve_report(self):
        self.client.force_authenticate(self.owner)
        report = self.client.post(
            reverse("user-report-create"),
            {
                "target_type": "user",
                "target_id": str(self.staff.id),
                "reason": "Cuenta que requiere revisión.",
            },
            format="json",
        )
        self.assertEqual(report.status_code, 201, report.data)

        self.client.force_authenticate(self.superadmin)
        response = self.client.post(
            reverse("admin-report-suspend-user", kwargs={"report_id": report.data["id"]})
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_active)
        self.assertEqual(response.data["status"], "resolved")
