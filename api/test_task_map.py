from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from api import models as ms

User = get_user_model()


class TaskLocationTests(TestCase):
    """Compartir una tarea desde la ubicación y verla en el mapa."""

    def setUp(self):
        self.author = User.objects.create_user(email="autor@example.com", password="x", username="autor")
        self.viewer = User.objects.create_user(email="ve@example.com", password="x", username="ve")
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def create(self, **extra):
        data = {"title": "Aquí", "pch": "consejos", **extra}
        return self.client.post(reverse("task-list-create"), data, format="multipart")

    def test_location_is_saved_rounded_to_about_100_m(self):
        response = self.create(latitude="28.635612", longitude="-106.088915")
        self.assertEqual(response.status_code, 201, response.data)
        task = ms.Task.objects.get(title="Aquí")
        self.assertEqual((task.latitude, task.longitude), (28.636, -106.089))

    def test_without_location_it_is_not_on_the_map(self):
        self.assertEqual(self.create().status_code, 201)
        self.assertIsNone(ms.Task.objects.get(title="Aquí").latitude)

    def test_location_needs_both_coordinates_in_range(self):
        self.assertEqual(self.create(latitude="28.6").status_code, 400)
        self.assertEqual(self.create(latitude="95", longitude="10").status_code, 400)

    def test_map_lists_only_public_tasks_with_location(self):
        shown = ms.Task.objects.create(user=self.author, title="En el mapa", latitude=28.6, longitude=-106.1)
        ms.Task.objects.create(user=self.author, title="Sin ubicación")
        ms.Task.objects.create(user=self.author, title="Oculta", is_hidden=True, latitude=28.6, longitude=-106.1)
        ms.Task.objects.create(user=self.author, title="Historia", is_story=True, latitude=28.6, longitude=-106.1)
        far = ms.Task.objects.create(user=self.author, title="Lejos", latitude=19.4, longitude=-99.1)

        self.client.force_authenticate(self.viewer)
        response = self.client.get(reverse("task-map"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["id"] for row in response.data}, {str(shown.pk), str(far.pk)})
        self.assertEqual(response.data[-1]["user"]["username"], "autor")

        response = self.client.get(reverse("task-map"), {"bbox": "-107,28,-105,29"})
        self.assertEqual([row["title"] for row in response.data], ["En el mapa"])
