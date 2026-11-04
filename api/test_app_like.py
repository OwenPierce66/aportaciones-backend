from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from api import models as ms
from api.app_like_requests import APP_LIKE_REQUEST

User = get_user_model()


@override_settings(APP_ACCOUNT_EMAIL="owen@gmail.com")
class AppLikeTests(TestCase):
    """El "like de la App": cada persona del staff lo da a su nombre, marcado como de la App."""

    def setUp(self):
        self.app_account = User.objects.create_user(
            email="owen@gmail.com", password="test-password", username="owen"
        )
        self.admin = User.objects.create_superuser(
            email="admin@example.com", password="test-password", username="admin"
        )
        self.staff = User.objects.create_user(
            email="staff@example.com", password="test-password", username="staff", is_staff=True
        )
        self.author = User.objects.create_user(
            email="autor@example.com", password="test-password", username="autor"
        )
        self.task = ms.Task.objects.create(user=self.author, title="Aportación")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = reverse("admin-task-like", kwargs={"task_id": self.task.id})

    def requests_for(self, user):
        return ms.Notification.objects.filter(recipient=user, notification_type=APP_LIKE_REQUEST)

    def test_like_is_given_by_the_staff_member_as_the_app(self):
        response = self.client.post(self.url, {"liked": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["liked"])
        self.assertEqual(response.data["likes_count"], 1)
        like = ms.Like.objects.get(task=self.task)
        self.assertEqual((like.user, like.as_app), (self.admin, True))

    def test_liking_twice_keeps_one_like_and_false_removes_it(self):
        self.client.post(self.url, {}, format="json")
        self.client.post(self.url, {"liked": True}, format="json")
        self.assertEqual(ms.Like.objects.filter(task=self.task).count(), 1)

        response = self.client.post(self.url, {"liked": False}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["liked"])
        self.assertEqual(response.data["likes_count"], 0)

    def test_a_normal_like_becomes_an_app_like(self):
        ms.Like.objects.create(user=self.admin, task=self.task)
        self.client.post(self.url, {"liked": True}, format="json")
        self.assertTrue(ms.Like.objects.get(user=self.admin, task=self.task).as_app)

    def test_staff_can_like_but_regular_users_cannot(self):
        self.client.force_authenticate(self.author)
        response = self.client.post(self.url, {"liked": True}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ms.Like.objects.exists())

        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {"liked": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(ms.Like.objects.filter(user=self.staff, task=self.task, as_app=True).exists())

    def test_app_has_liked_is_about_my_own_app_like(self):
        detail = reverse("task-detail", kwargs={"id": self.task.id})
        self.assertIs(self.client.get(detail).data["app_has_liked"], False)

        ms.Like.objects.create(user=self.admin, task=self.task)
        self.assertIs(self.client.get(detail).data["app_has_liked"], False)

        self.client.post(self.url, {"liked": True}, format="json")
        self.assertIs(self.client.get(detail).data["app_has_liked"], True)

        self.client.force_authenticate(self.staff)
        self.assertIs(self.client.get(detail).data["app_has_liked"], False)

        self.client.force_authenticate(self.author)
        self.assertIsNone(self.client.get(detail).data["app_has_liked"])

    def test_likers_list_marks_app_likes_with_the_giver_name(self):
        self.client.post(self.url, {"liked": True}, format="json")
        ms.Like.objects.create(user=self.author, task=self.task)
        ms.Like.objects.create(user=self.app_account, task=self.task)  # like viejo de la cuenta de la App
        response = self.client.get(reverse("task-likers", kwargs={"task_id": self.task.id}))
        self.assertEqual(response.status_code, 200)
        rows = {row["username"]: row["is_app"] for row in response.data}
        self.assertEqual(rows, {"admin": True, "autor": False, "owen": True})

    def test_user_lists_mark_the_app_account(self):
        ms.SharedTask.objects.create(task=self.task, shared_by=self.app_account)
        ms.SharedTask.objects.create(task=self.task, shared_by=self.author)
        response = self.client.get(reverse("task-sharers", kwargs={"task_id": self.task.id}))
        self.assertEqual({row["email"]: row["is_app"] for row in response.data},
                         {"owen@gmail.com": True, "autor@example.com": False})

    def test_staff_normal_like_is_labeled_but_not_an_app_like(self):
        ms.Like.objects.create(user=self.staff, task=self.task)
        ms.Like.objects.create(user=self.author, task=self.task)
        response = self.client.get(reverse("task-likers", kwargs={"task_id": self.task.id}))
        rows = {row["username"]: (row["is_app"], row["is_app_member"]) for row in response.data}
        self.assertEqual(rows, {"staff": (False, True), "autor": (False, False)})

    def test_profile_like_is_given_by_the_app_account(self):
        url = reverse("admin-profile-like", kwargs={"profile_id": self.author.id})
        response = self.client.post(url, {"liked": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(ms.LikeP.objects.filter(user=self.app_account, profile=self.author).exists())

        response = self.client.get(reverse("profile-likers", kwargs={"profile_id": self.author.id}))
        self.assertEqual([row["is_app"] for row in response.data["results"]], [True])

        response = self.client.post(url, {"liked": False}, format="json")
        self.assertFalse(response.data["liked"])
        self.assertFalse(ms.LikeP.objects.filter(user=self.app_account, profile=self.author).exists())

    def test_missing_app_account_is_reported_for_profile_likes(self):
        self.app_account.delete()
        url = reverse("admin-profile-like", kwargs={"profile_id": self.author.id})
        response = self.client.post(url, {"liked": True}, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertIn("owen@gmail.com", response.data["detail"])

    def test_app_like_asks_the_rest_of_the_team(self):
        other_admin = User.objects.create_superuser(
            email="admin2@example.com", password="test-password", username="admin2"
        )
        ms.Like.objects.create(user=other_admin, task=self.task)  # ya le dio like: no se le pide

        self.client.post(self.url, {"liked": True}, format="json")
        request = self.requests_for(self.staff).get()
        self.assertEqual((request.actor, request.target_id), (self.admin, str(self.task.id)))
        self.assertFalse(self.requests_for(self.admin).exists())
        self.assertFalse(self.requests_for(other_admin).exists())
        self.assertFalse(self.requests_for(self.author).exists())

        # Al darle like (normal o como la App), el aviso desaparece.
        ms.Like.objects.create(user=self.staff, task=self.task)
        self.assertFalse(self.requests_for(self.staff).exists())

    def test_request_is_retired_when_no_app_likes_remain(self):
        self.client.post(self.url, {"liked": True}, format="json")
        self.assertTrue(self.requests_for(self.staff).exists())
        self.client.post(self.url, {"liked": False}, format="json")
        self.assertFalse(self.requests_for(self.staff).exists())

    def test_seen_request_does_not_come_back_until_unmarked(self):
        other_admin = User.objects.create_superuser(
            email="admin2@example.com", password="test-password", username="admin2"
        )
        self.client.post(self.url, {"liked": True}, format="json")
        request = self.requests_for(self.staff).get()

        self.client.force_authenticate(self.staff)
        seen_url = reverse("notification-app-like-seen", kwargs={"notification_id": request.id})
        response = self.client.post(seen_url, {"seen": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["seen"])
        self.assertTrue(response.data["is_read"])

        # Otra persona da like como la App: el aviso visto no vuelve a salir como nuevo.
        self.client.force_authenticate(other_admin)
        self.client.post(self.url, {"liked": True}, format="json")
        request.refresh_from_db()
        self.assertTrue(request.is_read)

        # Lo desmarca: el siguiente like como la App se lo vuelve a mostrar.
        self.client.force_authenticate(self.staff)
        self.client.post(seen_url, {"seen": False}, format="json")
        self.client.force_authenticate(self.admin)
        self.client.post(self.url, {"liked": False}, format="json")
        self.client.post(self.url, {"liked": True}, format="json")
        request.refresh_from_db()
        self.assertFalse(request.is_read)
        self.assertEqual(request.actor, self.admin)

    def test_filters_by_app_like_and_admin_authors(self):
        admin_task = ms.Task.objects.create(user=self.staff, title="Del staff")
        self.client.post(self.url, {"liked": True}, format="json")
        ms.Like.objects.create(user=self.author, task=admin_task)
        url = reverse("task-list-create")

        def titles(**params):
            cache.clear()
            response = self.client.get(url, params)
            return sorted(task["title"] for task in response.data["results"])

        self.assertEqual(titles(app_liked_only="true"), ["Aportación"])
        self.assertEqual(titles(admin_users_only="true"), ["Del staff"])
        self.assertEqual(titles(app_liked_only="true", sort_by="likes"), ["Aportación"])

    def test_task_list_for_superuser_does_not_grow_with_tasks(self):
        url = reverse("task-list-create")

        def add_tasks(start, end):
            for i in range(start, end):
                task = ms.Task.objects.create(user=self.author, title=f"t{i}")
                ms.Like.objects.create(user=self.admin, task=task, as_app=True)

        def count_queries():
            cache.clear()  # el listado usa caché por usuario
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            return len(ctx)

        add_tasks(0, 2)
        small = count_queries()
        add_tasks(2, 8)
        large = count_queries()
        self.assertLessEqual(large, small, f"{small} consultas con 2 tareas -> {large} con 8")
