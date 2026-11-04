from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from api import models as ms
from api.mentions import DESCRIPTION_MENTION

User = get_user_model()


class DescriptionMentionTests(TestCase):
    """@usuario en la descripción de una aportación o de sus bloques."""

    def setUp(self):
        self.author = User.objects.create_user(email="autor@example.com", password="x", username="autor")
        self.ana = User.objects.create_user(email="ana@example.com", password="x", username="Ana.Lu")
        self.beto = User.objects.create_user(email="beto@example.com", password="x", username="beto")

    def mentions_for(self, user):
        return ms.Notification.objects.filter(recipient=user, notification_type=DESCRIPTION_MENTION)

    def test_mention_notifies_once_and_not_the_author(self):
        task = ms.Task.objects.create(user=self.author, title="Ruta", description="Gracias @ana.lu. y @autor @nadie")
        notification = self.mentions_for(self.ana).get()
        self.assertEqual(notification.target_id, str(task.pk))
        self.assertEqual(notification.actor, self.author)
        self.assertEqual(notification.data["text"], "autor te mencionó en una publicación.")
        self.assertFalse(self.mentions_for(self.author).exists())

        task.description += " @beto"
        task.save()
        self.assertEqual(self.mentions_for(self.ana).count(), 1)
        self.assertEqual(self.mentions_for(self.beto).count(), 1)

    def test_hidden_task_notifies_when_published_including_its_blocks(self):
        task = ms.Task.objects.create(user=self.author, title="Borrador", description="Con @ana.lu", is_hidden=True)
        ms.SubTask.objects.create(parent_task=task, title="Paso", description="Y @beto")
        self.assertFalse(self.mentions_for(self.ana).exists())
        self.assertFalse(self.mentions_for(self.beto).exists())

        task.is_hidden = False
        task.save(update_fields=["is_hidden"])
        self.assertTrue(self.mentions_for(self.ana).exists())
        self.assertTrue(self.mentions_for(self.beto).exists())

    def test_block_mention_notifies(self):
        task = ms.Task.objects.create(user=self.author, title="Guía")
        ms.SubFuentes.objects.create(parent_task=task, title="Fuente", description="Dato de @beto")
        self.assertEqual(self.mentions_for(self.beto).get().target_id, str(task.pk))

    def test_links_emails_and_shared_stories_do_not_notify(self):
        ms.Task.objects.create(user=self.author, title="Links", description="medium.com/@beto y beto@example.com")
        source = ms.Task.objects.create(user=self.ana, title="Original", description="Hola @beto")
        self.assertEqual(self.mentions_for(self.beto).count(), 1)
        ms.Task.objects.create(
            user=self.author, title="Original", description="Hola @beto",
            story_is_shared=True, story_source_task=source,
        )
        self.assertEqual(self.mentions_for(self.beto).count(), 1)

    def test_unrelated_saves_do_not_check_mentions_again(self):
        task = ms.Task.objects.create(user=self.author, title="Ruta", description="@beto")
        self.mentions_for(self.beto).delete()
        task.save(update_fields=["share_count"])
        self.assertFalse(self.mentions_for(self.beto).exists())

    def test_open_profile_by_username(self):
        client = APIClient()
        client.force_authenticate(self.author)
        response = client.get(reverse("user-by-username", kwargs={"username": "ana.lu"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], str(self.ana.pk))
        response = client.get(reverse("user-by-username", kwargs={"username": "nadie"}))
        self.assertEqual(response.status_code, 404)


class HashtagTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(email="autor@example.com", password="x", username="autor")
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def task(self, description, **extra):
        return ms.Task.objects.create(user=self.author, title="T", pch="consejos", description=description, **extra)

    def test_search_matches_the_whole_hashtag(self):
        exact = self.task("Mi #Viaje a la playa")
        self.task("Puros #viajes")
        self.task("Un viaje sin hashtag")
        self.task("x.com/#viaje")
        response = self.client.get(reverse("task-list-create"), {"search": "#viaje"})
        self.assertEqual([row["id"] for row in response.data["results"]], [str(exact.pk)])

    def test_shared_tasks_search_hashtags_too(self):
        task = self.task("Sin hashtag")
        ms.SharedTask.objects.create(task=task, shared_by=self.author, description="Miren #ruta")
        ms.SharedTask.objects.create(task=self.task("Otra"), shared_by=self.author, description="#rutas")
        response = self.client.get(reverse("shared-task-list-create"), {"search": "#ruta"})
        self.assertEqual([row["task"]["id"] for row in response.data["results"]], [str(task.pk)])

    def test_suggestions_by_prefix_most_used_first(self):
        self.task("#viaje #Viaje #viajero")
        self.task("#viaje y #comida")
        self.task("#viajeoculto", is_hidden=True)
        self.task("Puesto #3 y #1")
        response = self.client.get(reverse("hashtag-suggestions"), {"q": "#via"})
        self.assertEqual(response.data, [{"tag": "viaje", "count": 2}, {"tag": "viajero", "count": 1}])
        response = self.client.get(reverse("hashtag-suggestions"))
        self.assertEqual({row["tag"] for row in response.data}, {"viaje", "viajero", "comida"})


class MentionPlacesTests(TestCase):
    """Cada aviso dice dónde te mencionaron o etiquetaron: comentario, respuesta, compartida, foro."""

    def setUp(self):
        self.author = User.objects.create_user(email="autor@example.com", password="x", username="autor")
        self.ana = User.objects.create_user(email="ana@example.com", password="x", username="ana")
        self.beto = User.objects.create_user(email="beto@example.com", password="x", username="beto")
        self.task = ms.Task.objects.create(user=self.author, title="Ruta", pch="consejos")
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def last_for(self, user, notification_type):
        return ms.Notification.objects.filter(recipient=user, notification_type=notification_type).latest("created_at")

    def test_comment_and_reply_say_where(self):
        url = reverse("task-comments", kwargs={"task_id": self.task.id})
        comment = self.client.post(url, {"text": "Mira @ana"}, format="json").data
        notification = self.last_for(self.ana, "task_mention")
        self.assertEqual(notification.data["text"], "autor te mencionó en un comentario.")
        self.assertEqual((notification.data["title"], notification.data["excerpt"]), ("Ruta", "Mira @ana"))

        self.client.post(url, {"text": "Y tú @beto", "parent": comment["id"]}, format="json")
        self.assertEqual(self.last_for(self.beto, "task_mention").data["text"], "autor te mencionó en una respuesta a un comentario.")

    def test_editing_a_comment_notifies_who_was_added(self):
        comment = ms.NewPeticionCommentPost.objects.create(created_by=self.author, post=self.task, text="Hola @ana")
        comment.text += " y @beto"
        comment.save()
        self.assertEqual(ms.Notification.objects.filter(notification_type="task_mention", recipient=self.ana).count(), 1)
        self.assertTrue(ms.Notification.objects.filter(notification_type="task_mention", recipient=self.beto).exists())

    def test_comment_on_a_hidden_task_does_not_notify(self):
        hidden = ms.Task.objects.create(user=self.author, title="Oculta", is_hidden=True)
        ms.NewPeticionCommentPost.objects.create(created_by=self.author, post=hidden, text="@ana")
        self.assertFalse(ms.Notification.objects.filter(recipient=self.ana).exists())

    def test_shared_task_comments_say_where(self):
        shared = ms.SharedTask.objects.create(task=self.task, shared_by=self.beto)
        url = reverse("shared-task-comments", kwargs={"shared_task_id": shared.id})
        response = self.client.post(url, {"text": "Gracias @ana", "tagged_user_ids": [str(self.beto.id)]}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            self.last_for(self.ana, "shared_comment_mention").data["text"],
            "autor te mencionó en un comentario de una publicación compartida.",
        )
        tag = self.last_for(self.beto, "shared_comment_tag")
        self.assertEqual(tag.data["text"], "autor te etiquetó en un comentario de una publicación compartida.")
        self.assertEqual(tag.data["title"], "Ruta")

    def test_forum_posts_and_replies_say_where(self):
        url = reverse("post-list-create")
        post = self.client.post(url, {"title": "Dudas", "content": "¿Qué opinas @ana?"}, format="json").data
        notification = self.last_for(self.ana, "forum_mention")
        self.assertEqual(notification.data["text"], "autor te mencionó en una publicación del foro.")
        self.assertEqual((notification.target_type, notification.target_id), ("forum_post", str(post["id"])))

        self.client.post(url, {"content": "Te etiqueto", "parent": post["id"], "tagged_user_ids": [str(self.beto.id)]}, format="json")
        self.assertEqual(self.last_for(self.beto, "forum_tag").data["text"], "autor te etiquetó en una respuesta del foro.")
        self.client.force_authenticate(self.beto)
        self.client.post(url, {"content": "Respondo @ana", "parent": post["id"]}, format="json")
        self.assertEqual(self.last_for(self.ana, "forum_mention").data["text"], "beto te mencionó en una respuesta del foro.")

    def test_forum_invited_only_post_notifies_only_who_can_see_it(self):
        response = self.client.post(reverse("post-list-create"), {
            "title": "Privado", "content": "@ana y @beto",
            "visibility": "invited", "invited_user_ids": [str(self.ana.id)],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(ms.Notification.objects.filter(recipient=self.ana, notification_type="forum_mention").exists())
        self.assertFalse(ms.Notification.objects.filter(recipient=self.beto, notification_type="forum_mention").exists())

    def test_forum_hashtag_filter(self):
        with_tag = ms.Postt.objects.create(user=self.author, title="Viaje", content="Fotos #playa")
        in_reply = ms.Postt.objects.create(user=self.author, title="Otro", content="Nada")
        ms.Postt.objects.create(user=self.ana, parent=in_reply, content="Yo voy a la #Playa")
        ms.Postt.objects.create(user=self.author, title="No", content="#playas")
        response = self.client.get(reverse("post-list-create"), {"hashtag": "playa"})
        self.assertEqual({row["id"] for row in response.data["results"]}, {with_tag.id, in_reply.id})

    def test_notifications_never_show_an_email(self):
        nameless = User.objects.create_user(email="sin.usuario@example.com", password="x")
        ms.Task.objects.create(user=nameless, title="Hola", description="@ana")
        notification = self.last_for(self.ana, "task_description_mention")
        self.assertEqual(notification.data["text"], "Alguien te mencionó en una publicación.")
        self.client.force_authenticate(self.ana)
        item = self.client.get(reverse("notification-list")).data["results"][0]
        self.assertNotIn("sin.usuario", str(item))

