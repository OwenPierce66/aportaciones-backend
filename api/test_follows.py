from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from api.models import Profile, pFavorito

User = get_user_model()


class FavoriteProfileSocialStatsTests(TestCase):
    """"Seguir" es la función de perfiles favoritos (pFavorito)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="viewer", email="viewer@example.com", password="test-password"
        )
        self.target = User.objects.create_user(
            username="target", email="target@example.com", password="test-password"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.stats_url = reverse("user-social-stats", kwargs={"user_id": self.target.id})

    def test_toggle_favorite_updates_stats(self):
        response = self.client.get(self.stats_url)
        self.assertEqual(response.data["favorited_by_count"], 0)
        self.assertFalse(response.data["is_favorited"])

        self.assertEqual(
            self.client.post(reverse("pfavorito-add"), {"perfil_id": str(self.target.id)}).status_code,
            201,
        )
        response = self.client.get(self.stats_url)
        self.assertEqual(response.data["favorited_by_count"], 1)
        self.assertTrue(response.data["is_favorited"])

        self.assertEqual(
            self.client.post(reverse("pfavorito-add"), {"perfil_id": str(self.target.id)}).status_code,
            204,
        )
        response = self.client.get(self.stats_url)
        self.assertEqual(response.data["favorited_by_count"], 0)
        self.assertFalse(response.data["is_favorited"])

    def test_given_count_is_hidden_unless_public_or_owner(self):
        pFavorito.objects.create(user=self.target, perfil=self.user)
        profile = self.target.profile
        profile.favorite_profiles_public = False
        profile.save()

        response = self.client.get(self.stats_url)
        self.assertIsNone(response.data["favorites_count"])

        profile.favorite_profiles_public = True
        profile.save()
        response = self.client.get(self.stats_url)
        self.assertEqual(response.data["favorites_count"], 1)

    def test_owner_sees_own_given_count_and_is_not_favorited(self):
        other = User.objects.create_user(
            username="other", email="other@example.com", password="test-password"
        )
        pFavorito.objects.create(user=self.user, perfil=other)
        profile = self.user.profile
        profile.favorite_profiles_public = False
        profile.save()

        response = self.client.get(reverse("user-social-stats", kwargs={"user_id": self.user.id}))
        self.assertEqual(response.data["favorites_count"], 1)
        self.assertFalse(response.data["is_favorited"])

    def test_admin_state_only_for_staff_and_superuser(self):
        response = self.client.get(self.stats_url)
        self.assertNotIn("admin_state", response.data)

        staff = User.objects.create_user(
            username="staffer", email="staffer@example.com", password="test-password", is_staff=True
        )
        self.client.force_authenticate(staff)
        self.assertIn("admin_state", self.client.get(self.stats_url).data)

        admin = User.objects.create_superuser(
            username="boss", email="boss@example.com", password="test-password"
        )
        self.client.force_authenticate(admin)
        self.target.is_active = False
        self.target.save()
        response = self.client.get(self.stats_url)
        self.assertEqual(
            response.data["admin_state"],
            {
                "is_active": False,
                "is_staff": False,
                "is_superuser": False,
                "is_verified": False,
                "is_recommended": False,
            },
        )


class PinImpliesFavoriteTests(TestCase):
    """Anclar sin tenerlo en favoritos lo agrega primero."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="pinner", email="pinner@example.com", password="test-password"
        )
        self.target = User.objects.create_user(
            username="pinned", email="pinned@example.com", password="test-password"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_pin_profile_not_in_favorites_adds_it(self):
        self.assertFalse(pFavorito.objects.filter(user=self.user, perfil=self.target).exists())
        response = self.client.post(
            reverse("favorite-pin"),
            {"type": "profile", "target_id": str(self.target.id), "is_pinned": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        favorite = pFavorito.objects.get(user=self.user, perfil=self.target)
        self.assertTrue(favorite.is_pinned)

    def test_pin_task_not_in_favorites_adds_it(self):
        from api.models import Favorito, Task

        task = Task.objects.create(user=self.target, title="x", pch="peticiones")
        response = self.client.post(
            reverse("favorite-pin"),
            {"type": "task", "target_id": str(task.id), "is_pinned": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Favorito.objects.get(user=self.user, task=task).is_pinned)

    def test_unpin_unknown_favorite_is_still_404(self):
        response = self.client.post(
            reverse("favorite-pin"),
            {"type": "profile", "target_id": str(self.target.id), "is_pinned": False},
            format="json",
        )
        self.assertEqual(response.status_code, 404)


class FollowHierarchyTests(TestCase):
    """Jerarquía: Seguir -> Favorito -> Anclado."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="me", email="me@example.com", password="test-password"
        )
        self.target = User.objects.create_user(
            username="them", email="them@example.com", password="test-password"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.follow_url = reverse("user-follow-toggle", kwargs={"user_id": self.target.id})
        self.stats_url = reverse("user-social-stats", kwargs={"user_id": self.target.id})

    def test_follow_and_unfollow_update_stats(self):
        from api.models import Follow

        self.assertEqual(self.client.post(self.follow_url).status_code, 200)
        self.assertTrue(Follow.objects.filter(follower=self.user, following=self.target).exists())
        stats = self.client.get(self.stats_url).data
        self.assertEqual(stats["followers_count"], 1)
        self.assertTrue(stats["is_following"])
        # Seguir NO es favorito.
        self.assertFalse(stats["is_favorited"])
        self.assertFalse(pFavorito.objects.filter(user=self.user, perfil=self.target).exists())

        # Repetir POST es idempotente.
        self.client.post(self.follow_url)
        self.assertEqual(Follow.objects.count(), 1)

        self.assertEqual(self.client.delete(self.follow_url).status_code, 200)
        self.assertFalse(self.client.get(self.stats_url).data["is_following"])

    def test_cannot_follow_yourself(self):
        url = reverse("user-follow-toggle", kwargs={"user_id": self.user.id})
        self.assertEqual(self.client.post(url).status_code, 400)

    def test_favoriting_follows_automatically(self):
        from api.models import Follow

        self.client.post(reverse("pfavorito-add"), {"perfil_id": str(self.target.id)})
        self.assertTrue(Follow.objects.filter(follower=self.user, following=self.target).exists())
        self.assertTrue(self.client.get(self.stats_url).data["is_following"])

    def test_pinning_favorites_and_follows(self):
        from api.models import Follow

        self.client.post(
            reverse("favorite-pin"),
            {"type": "profile", "target_id": str(self.target.id), "is_pinned": True},
            format="json",
        )
        self.assertTrue(pFavorito.objects.get(user=self.user, perfil=self.target).is_pinned)
        self.assertTrue(Follow.objects.filter(follower=self.user, following=self.target).exists())

    def test_followers_list_says_if_viewer_follows(self):
        url = reverse("user-followers", kwargs={"user_id": self.target.id})
        self.assertFalse(self.client.get(url).data["viewer_follows"])
        self.client.post(self.follow_url)
        data = self.client.get(url).data
        self.assertTrue(data["viewer_follows"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["users"][0]["id"], str(self.user.id))

    def test_task_author_includes_followers_count(self):
        from api.models import Task

        task = Task.objects.create(user=self.target, title="Reel", pch="consejos")
        detail_url = reverse("task-detail", kwargs={"id": task.id})
        self.assertEqual(self.client.get(detail_url).data["user"]["followers_count"], 0)
        self.client.post(self.follow_url)
        author = self.client.get(detail_url).data["user"]
        self.assertEqual(author["followers_count"], 1)
        self.assertTrue(author["is_following"])

    def test_unpinning_moves_profile_to_sixth_place(self):
        others = [
            User.objects.create_user(email=f"fav{i}@example.com", password="pass", username=f"fav{i}")
            for i in range(5)
        ]
        pin_url = reverse("favorite-pin")
        for profile in [self.target, *others[:4]]:
            self.client.post(pin_url, {"type": "profile", "target_id": str(profile.id)}, format="json")
        self.client.post(reverse("pfavorito-add"), {"perfil_id": str(others[4].id)})

        self.client.post(
            pin_url, {"type": "profile", "target_id": str(self.target.id), "is_pinned": False}, format="json",
        )
        target_fav = pFavorito.objects.get(user=self.user, perfil=self.target)
        self.assertFalse(target_fav.is_pinned)
        self.assertEqual(target_fav.position, 5)
        # El que estaba sexto sube al top 5.
        self.assertTrue(pFavorito.objects.get(user=self.user, perfil=others[4]).is_pinned)
        self.assertEqual(pFavorito.objects.filter(user=self.user, is_pinned=True).count(), 5)

    def test_unpinning_with_few_favorites_stays_unpinned(self):
        pin_url = reverse("favorite-pin")
        self.client.post(pin_url, {"type": "profile", "target_id": str(self.target.id)}, format="json")
        self.client.post(
            pin_url, {"type": "profile", "target_id": str(self.target.id), "is_pinned": False}, format="json",
        )
        self.assertFalse(pFavorito.objects.get(user=self.user, perfil=self.target).is_pinned)
        self.assertTrue(self.client.get(self.stats_url).data["is_favorited"])

    def test_unfollow_removes_favorite_and_pin(self):
        self.client.post(
            reverse("favorite-pin"),
            {"type": "profile", "target_id": str(self.target.id), "is_pinned": True},
            format="json",
        )
        response = self.client.delete(self.follow_url)
        self.assertFalse(response.data["is_favorited"])
        self.assertFalse(pFavorito.objects.filter(user=self.user, perfil=self.target).exists())

    def test_unfavoriting_keeps_following(self):
        from api.models import Follow

        self.client.post(reverse("pfavorito-add"), {"perfil_id": str(self.target.id)})
        self.client.post(reverse("pfavorito-add"), {"perfil_id": str(self.target.id)})  # quita
        self.assertFalse(pFavorito.objects.filter(user=self.user, perfil=self.target).exists())
        self.assertTrue(Follow.objects.filter(follower=self.user, following=self.target).exists())

    def test_follow_notification_is_not_duplicated_by_favorite(self):
        from api.models import Notification

        self.client.post(self.follow_url)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.target, notification_type="user_follow"
            ).count(),
            1,
        )
        other = User.objects.create_user(
            username="o", email="o@example.com", password="test-password"
        )
        other_client = APIClient()
        other_client.force_authenticate(other)
        other_client.post(reverse("pfavorito-add"), {"perfil_id": str(self.target.id)})
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.target, actor=other, notification_type="user_follow"
            ).count(),
            0,
        )
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.target, actor=other, notification_type="profile_favorite"
            ).count(),
            1,
        )

    def test_followed_users_only_filter_and_lists(self):
        from api.models import Task

        mine = Task.objects.create(user=self.target, title="de seguido", pch="peticiones")
        stranger = User.objects.create_user(
            username="s", email="s@example.com", password="test-password"
        )
        other = Task.objects.create(user=stranger, title="de otro", pch="peticiones")
        self.client.post(self.follow_url)

        response = self.client.get(reverse("task-list-create"), {"followed_users_only": "true"})
        data = response.data["results"] if "results" in response.data else response.data
        found = {str(item["id"]) for item in data}
        self.assertIn(str(mine.id), found)
        self.assertNotIn(str(other.id), found)

        following = self.client.get(reverse("user-following", kwargs={"user_id": self.user.id}))
        self.assertEqual(following.data["count"], 1)
        self.assertTrue(following.data["users"][0]["is_following"])
        followers = self.client.get(reverse("user-followers", kwargs={"user_id": self.target.id}))
        self.assertEqual(followers.data["count"], 1)


class MyRelationsTests(TestCase):
    def test_returns_following_favorites_and_pinned(self):
        me = User.objects.create_user(username="me", email="me@example.com", password="x")
        a = User.objects.create_user(username="a", email="a@example.com", password="x")
        b = User.objects.create_user(username="b", email="b@example.com", password="x")
        c = User.objects.create_user(username="c", email="c@example.com", password="x")
        client = APIClient()
        client.force_authenticate(me)
        client.post(reverse("user-follow-toggle", kwargs={"user_id": a.id}))
        pFavorito.objects.create(user=me, perfil=b, is_pinned=False)
        pFavorito.objects.create(user=me, perfil=c, is_pinned=True)

        data = client.get(reverse("user-my-relations")).data
        self.assertEqual(data["me"], str(me.id))
        # Favoritear sigue automaticamente (senal), asi que b y c tambien se siguen.
        self.assertEqual(set(data["following"]), {str(a.id), str(b.id), str(c.id)})
        self.assertEqual(set(data["favorites"]), {str(b.id), str(c.id)})
        self.assertEqual(data["pinned"], [str(c.id)])

    def test_requires_authentication(self):
        self.assertEqual(APIClient().get(reverse("user-my-relations")).status_code, 401)


class FavoritedByTests(TestCase):
    def setUp(self):
        self.target = User.objects.create_user(username="t", email="t@example.com", password="x")
        self.pub = User.objects.create_user(username="pub", email="pub@example.com", password="x")
        self.priv = User.objects.create_user(username="priv", email="priv@example.com", password="x")
        self.viewer = User.objects.create_user(username="v", email="v@example.com", password="x")
        for user, public in ((self.pub, True), (self.priv, False)):
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.favorite_profiles_public = public
            profile.save()
            pFavorito.objects.create(user=user, perfil=self.target)
        self.url = reverse("user-favorited-by", kwargs={"user_id": self.target.id})

    def test_owner_sees_everyone(self):
        client = APIClient()
        client.force_authenticate(self.target)
        data = client.get(self.url).data
        self.assertEqual(data["count"], 2)
        self.assertEqual({u["username"] for u in data["users"]}, {"pub", "priv"})

    def test_others_only_see_public_favorites(self):
        client = APIClient()
        client.force_authenticate(self.viewer)
        data = client.get(self.url).data
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["visible_count"], 1)
        self.assertEqual([u["username"] for u in data["users"]], ["pub"])
