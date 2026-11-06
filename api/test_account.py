import re
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

import requests
import stripe as real_stripe
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.mail import EmailMessage
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from api import models as ms
from api.account import NO_EMAIL_BACKEND
from massaging.models import Group as ChatGroup, GroupMembership as ChatMembership, GroupMessage, Message

User = get_user_model()
PASSWORD = "clave-vieja-9281"
NEW_PASSWORD = "clave-nueva-7316"


def run_in_foreground(fn, *args):
    fn(*args)


class AccountTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch("api.account._in_background", side_effect=run_in_foreground)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ana = User.objects.create_user(email="ana@example.com", password=PASSWORD, username="ana")
        self.luis = User.objects.create_user(email="luis@example.com", password=PASSWORD, username="luis")


class PasswordResetTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def ask(self, email="ana@example.com"):
        return self.client.post(reverse("password-reset"), {"email": email}, format="json")

    def confirm(self, code, new_password=NEW_PASSWORD, email="ana@example.com"):
        return self.client.post(
            reverse("password-reset-confirm"),
            {"email": email, "code": code, "new_password": new_password},
            format="json",
        )

    def sent_code(self):
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def wrong_code(self):
        return "000000" if self.sent_code() != "000000" else "000001"

    def age_codes(self, **delta):
        ms.PasswordResetCode.objects.update(created_at=timezone.now() - timedelta(**delta))

    def test_emails_a_code_and_only_stores_its_hash(self):
        self.assertEqual(self.ask().status_code, 200)
        self.assertEqual(mail.outbox[-1].to, ["ana@example.com"])
        self.assertIn("Aportaciones", mail.outbox[-1].subject)
        stored = ms.PasswordResetCode.objects.get(user=self.ana)
        self.assertNotIn(self.sent_code(), stored.code_hash)

    def test_answers_the_same_when_the_account_does_not_exist(self):
        known, unknown = self.ask(), self.ask("nadie@example.com")
        self.assertEqual((unknown.status_code, unknown.data), (known.status_code, known.data))
        self.assertEqual(len(mail.outbox), 1)

    def test_finds_the_account_whatever_the_capitals(self):
        self.ask("ANA@Example.com")
        self.assertEqual(mail.outbox[-1].to, ["ana@example.com"])
        response = self.confirm(self.sent_code(), email="ANA@Example.com")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["email"], "ana@example.com")

    def test_rejects_a_malformed_email(self):
        self.assertEqual(self.ask("no-es-correo").status_code, 400)

    @override_settings(EMAIL_BACKEND=NO_EMAIL_BACKEND)
    def test_says_so_when_no_email_provider_is_configured(self):
        self.assertEqual(self.ask().status_code, 503)
        self.assertFalse(ms.PasswordResetCode.objects.exists())

    def test_a_failed_send_does_not_break_the_answer(self):
        with mock.patch("api.account.send_mail", side_effect=OSError("sin red")), self.assertLogs("api.account", "ERROR"):
            self.assertEqual(self.ask().status_code, 200)

    def test_limits_how_often_an_account_gets_emails(self):
        self.ask()
        self.ask()
        self.assertEqual(len(mail.outbox), 1)
        self.age_codes(seconds=61)
        self.ask()
        self.assertEqual(len(mail.outbox), 2)
        for _ in range(3):
            self.age_codes(seconds=61)
            self.ask()
        self.assertEqual(len(mail.outbox), 5)
        self.age_codes(seconds=61)
        self.ask()
        self.assertEqual(len(mail.outbox), 5)

    def test_limits_requests_per_ip(self):
        for _ in range(10):
            self.assertEqual(self.ask("nadie@example.com").status_code, 200)
        self.assertEqual(self.ask("nadie@example.com").status_code, 429)

    def test_right_code_changes_the_password_and_closes_every_session(self):
        first = RefreshToken.for_user(self.ana)
        rotated = self.client.post(reverse("token_refresh"), {"refresh": str(RefreshToken.for_user(self.ana))}, format="json")
        self.assertEqual(rotated.status_code, 200, rotated.data)
        ms.PushToken.objects.create(user=self.ana, token="ExponentPushToken[ana]")

        self.ask()
        response = self.confirm(self.sent_code())
        self.assertEqual(response.status_code, 200, response.data)

        self.ana.refresh_from_db()
        self.assertTrue(self.ana.check_password(NEW_PASSWORD))
        self.assertFalse(ms.PasswordResetCode.objects.filter(user=self.ana).exists())
        self.assertFalse(self.ana.push_tokens.exists())
        for token in (str(first), rotated.data["refresh"]):
            self.assertEqual(self.client.post(reverse("token_refresh"), {"refresh": token}, format="json").status_code, 401)
        login = self.client.post(reverse("token_obtain_pair"), {"email": "ana@example.com", "password": NEW_PASSWORD}, format="json")
        self.assertEqual(login.status_code, 200)

    def test_a_used_code_cannot_be_used_again(self):
        self.ask()
        code = self.sent_code()
        self.assertEqual(self.confirm(code).status_code, 200)
        self.assertEqual(self.confirm(code, new_password="otra-clave-5150").status_code, 400)

    def test_locks_the_code_after_five_wrong_attempts(self):
        self.ask()
        code = self.sent_code()
        for _ in range(5):
            self.assertIn("code", self.confirm(self.wrong_code()).data)
        self.assertEqual(self.confirm(code).status_code, 400)
        self.ana.refresh_from_db()
        self.assertTrue(self.ana.check_password(PASSWORD))

    def test_an_expired_code_does_not_work(self):
        self.ask()
        ms.PasswordResetCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.confirm(self.sent_code()).status_code, 400)

    def test_only_the_latest_code_works(self):
        self.ask()
        old = self.sent_code()
        self.age_codes(seconds=61)
        self.ask()
        if old != self.sent_code():
            self.assertEqual(self.confirm(old).status_code, 400)
        self.assertEqual(self.confirm(self.sent_code()).status_code, 200)

    def test_a_weak_password_keeps_the_code_alive(self):
        self.ask()
        response = self.confirm(self.sent_code(), new_password="123")
        self.assertEqual(response.status_code, 400)
        self.assertIn("new_password", response.data)
        self.assertEqual(self.confirm(self.sent_code()).status_code, 200)

    def test_an_unknown_email_gets_the_same_error_as_a_wrong_code(self):
        self.ask()
        wrong = self.confirm(self.wrong_code())
        unknown = self.confirm("123456", email="nadie@example.com")
        self.assertEqual((unknown.status_code, unknown.data), (wrong.status_code, wrong.data))


class BrevoBackendTests(TestCase):
    @override_settings(EMAIL_BACKEND="api.email_backend.BrevoEmailBackend", BREVO_API_KEY="clave-brevo")
    def test_posts_the_message_to_brevo(self):
        with mock.patch("api.email_backend.requests.post") as post:
            sent = EmailMessage("Asunto", "Cuerpo", "Aportaciones <hola@example.com>", ["ana@example.com"]).send()
        self.assertEqual(sent, 1)
        self.assertEqual(post.call_args.kwargs["headers"]["api-key"], "clave-brevo")
        self.assertEqual(post.call_args.kwargs["json"], {
            "sender": {"name": "Aportaciones", "email": "hola@example.com"},
            "to": [{"email": "ana@example.com"}],
            "subject": "Asunto",
            "textContent": "Cuerpo",
        })

    @override_settings(EMAIL_BACKEND="api.email_backend.BrevoEmailBackend", BREVO_API_KEY="clave-brevo")
    def test_raises_when_brevo_rejects_it(self):
        with mock.patch("api.email_backend.requests.post") as post:
            post.return_value.raise_for_status.side_effect = requests.HTTPError("401")
            with self.assertRaises(requests.HTTPError):
                EmailMessage("Asunto", "Cuerpo", "hola@example.com", ["ana@example.com"]).send()


class DeleteAccountTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.ana)

    def delete(self, password=PASSWORD):
        return self.client.post(reverse("user-delete"), {"password": password}, format="json")

    def test_a_wrong_password_deletes_nothing(self):
        response = self.delete("no-es-esta")
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data)
        self.assertTrue(User.objects.filter(email="ana@example.com").exists())

    def test_needs_a_session(self):
        self.assertEqual(APIClient().post(reverse("user-delete"), {"password": PASSWORD}, format="json").status_code, 401)

    def delete_with_google(self, claims):
        with mock.patch("api.account.verify_google_id_token", return_value=claims) as verify:
            response = self.client.post(reverse("user-delete"), {"id_token": "token-de-google"}, format="json")
        verify.assert_called_once_with("token-de-google")
        return response

    def test_an_account_made_with_google_confirms_with_google(self):
        google = User.objects.create_user(email="ana.lopez@gmail.com", username="analopez")
        self.client.force_authenticate(google)
        self.assertFalse(self.client.get(reverse("user-me")).data["has_password"])
        self.assertEqual(self.delete_with_google({"email": "Ana.Lopez@gmail.com"}).status_code, 204)
        self.assertFalse(User.objects.filter(pk=google.pk).exists())

    def test_an_account_with_password_can_also_confirm_with_google(self):
        self.assertTrue(self.client.get(reverse("user-me")).data["has_password"])
        self.assertEqual(self.delete_with_google({"email": "ana@example.com"}).status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.ana.pk).exists())

    def test_another_google_account_or_a_bad_token_deletes_nothing(self):
        for claims in ({"email": "luis@example.com"}, None):
            response = self.delete_with_google(claims)
            self.assertEqual(response.status_code, 400)
            self.assertIn("id_token", response.data)
        self.assertTrue(User.objects.filter(pk=self.ana.pk).exists())

    def test_deletes_everything_of_hers_and_nothing_of_anyone_else(self):
        hers = ms.Task.objects.create(user=self.ana, title="Suya", pch="consejos")
        his = ms.Task.objects.create(user=self.luis, title="De Luis", pch="consejos")
        ms.Like.objects.create(user=self.ana, task=his)
        ms.Like.objects.create(user=self.luis, task=hers)
        her_comment = ms.NewPeticionCommentPost.objects.create(created_by=self.ana, post=his, text="de ana")
        ms.LikeCommentPost.objects.create(user=self.luis, comment=her_comment)
        his_comment = ms.NewPeticionCommentPost.objects.create(created_by=self.luis, post=his, text="de luis")
        ms.Follow.objects.create(follower=self.ana, following=self.luis)
        ms.Follow.objects.create(follower=self.luis, following=self.ana)
        ms.Favorito.objects.create(user=self.ana, task=his)
        ms.SharedTask.objects.create(task=his, shared_by=self.ana, description="compartida")
        ms.ProfileVisit.objects.create(owner=self.ana, visitor=self.luis, day=timezone.localdate(), last_visited_at=timezone.now())
        ms.PushToken.objects.create(user=self.ana, token="ExponentPushToken[ana]")
        ms.PasswordResetCode.objects.create(user=self.ana, code_hash="x", expires_at=timezone.now())
        ms.Notification.objects.create(
            recipient=self.luis, actor=self.ana, notification_type="task_like",
            target_type="task", target_id=str(his.pk), data={"text": "ana indicó que le gusta"},
        )
        Message.objects.create(sender=self.ana, receiver=self.luis, content="hola")
        Message.objects.create(sender=self.luis, receiver=self.ana, content="hola ana")

        self.assertEqual(self.delete().status_code, 204)

        self.assertFalse(User.objects.filter(email="ana@example.com").exists())
        self.assertFalse(ms.Task.objects.filter(pk=hers.pk).exists())
        self.assertFalse(ms.NewPeticionCommentPost.objects.filter(pk=her_comment.pk).exists())
        self.assertFalse(Message.objects.exists())
        self.assertFalse(ms.Notification.objects.filter(data__text__contains="ana").exists())
        self.assertTrue(ms.Task.objects.filter(pk=his.pk).exists())
        self.assertTrue(ms.NewPeticionCommentPost.objects.filter(pk=his_comment.pk).exists())
        self.assertTrue(User.objects.filter(email="luis@example.com").exists())

    def test_the_old_session_stops_working(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.ana).access_token}")
        self.assertEqual(client.post(reverse("user-delete"), {"password": PASSWORD}, format="json").status_code, 204)
        self.assertEqual(client.get(reverse("user-me")).status_code, 401)

    def test_admins_cannot_delete_themselves_here(self):
        User.objects.filter(pk=self.ana.pk).update(is_staff=True)
        self.client.force_authenticate(User.objects.get(pk=self.ana.pk))
        self.assertEqual(self.delete().status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.ana.pk).exists())

    @override_settings(APP_ACCOUNT_EMAIL="ANA@example.com")
    def test_the_app_account_cannot_be_deleted(self):
        self.assertEqual(self.delete().status_code, 403)

    def test_community_group_passes_to_the_oldest_admin(self):
        group = ms.CommunityGroup.objects.create(name="Grupo", created_by=self.ana)
        eva = User.objects.create_user(email="eva@example.com", password="x", username="eva")
        ms.CommunityGroupMember.objects.create(group=group, user=self.ana, role="owner")
        ms.CommunityGroupMember.objects.create(group=group, user=self.luis, role="member")
        ms.CommunityGroupMember.objects.create(group=group, user=eva, role="admin")
        self.assertEqual(self.delete().status_code, 204)
        self.assertEqual(ms.CommunityGroupMember.objects.get(group=group, user=eva).role, "owner")
        self.assertEqual(ms.CommunityGroupMember.objects.get(group=group, user=self.luis).role, "member")

    def test_community_group_without_admins_passes_to_the_oldest_member(self):
        group = ms.CommunityGroup.objects.create(name="Grupo", created_by=self.ana)
        ms.CommunityGroupMember.objects.create(group=group, user=self.ana, role="owner")
        ms.CommunityGroupMember.objects.create(group=group, user=self.luis, role="member")
        self.assertEqual(self.delete().status_code, 204)
        self.assertEqual(ms.CommunityGroupMember.objects.get(group=group, user=self.luis).role, "owner")

    def test_community_group_with_nobody_left_goes_away(self):
        group = ms.CommunityGroup.objects.create(name="Solo", created_by=self.ana)
        ms.CommunityGroupMember.objects.create(group=group, user=self.ana, role="owner")
        ms.CommunityGroupMember.objects.create(group=group, user=self.luis, role="member", status="pending")
        self.assertEqual(self.delete().status_code, 204)
        self.assertFalse(ms.CommunityGroup.objects.filter(pk=group.pk).exists())

    def test_chat_group_survives_with_a_new_admin(self):
        chat = ChatGroup.objects.create(name="Chat", created_by=self.ana)
        ChatMembership.objects.create(group=chat, user=self.ana, is_admin=True)
        ChatMembership.objects.create(group=chat, user=self.luis)
        GroupMessage.objects.create(group=chat, sender=self.ana, content="de ana")
        keep = GroupMessage.objects.create(group=chat, sender=self.luis, content="de luis")
        self.assertEqual(self.delete().status_code, 204)
        chat.refresh_from_db()
        self.assertEqual(chat.created_by, self.luis)
        self.assertTrue(ChatMembership.objects.get(group=chat, user=self.luis).is_admin)
        self.assertEqual(list(GroupMessage.objects.filter(group=chat)), [keep])

    def test_chat_group_keeps_its_other_admin_when_an_admin_leaves(self):
        chat = ChatGroup.objects.create(name="Chat", created_by=self.luis)
        eva = User.objects.create_user(email="eva@example.com", password="x", username="eva")
        ChatMembership.objects.create(group=chat, user=self.luis, is_admin=True)
        ChatMembership.objects.create(group=chat, user=self.ana, is_admin=True)
        ChatMembership.objects.create(group=chat, user=eva)
        self.assertEqual(self.delete().status_code, 204)
        chat.refresh_from_db()
        self.assertEqual(chat.created_by, self.luis)
        self.assertFalse(ChatMembership.objects.get(group=chat, user=eva).is_admin)

    def test_chat_group_with_nobody_left_goes_away(self):
        chat = ChatGroup.objects.create(name="Solo", created_by=self.ana)
        ChatMembership.objects.create(group=chat, user=self.ana, is_admin=True)
        self.assertEqual(self.delete().status_code, 204)
        self.assertFalse(ChatGroup.objects.filter(pk=chat.pk).exists())

    def test_removes_her_files_but_not_one_someone_else_still_uses(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            only_hers = ms.Task.objects.create(user=self.ana, title="Foto", pch="consejos")
            only_hers.image.save("solo.png", ContentFile(b"png"))
            shared = ms.Task.objects.create(user=self.ana, title="Original", pch="consejos")
            shared.image.save("compartida.png", ContentFile(b"png"))
            ms.Task.objects.create(user=self.luis, title="Copia", pch="consejos", image=shared.image.name)
            cover = ms.Portada.objects.create(user=self.ana, title="Portada")
            cover.image.save("portada.png", ContentFile(b"png"))
            storage = only_hers.image.storage
            names = {"only": only_hers.image.name, "shared": shared.image.name, "cover": cover.image.name}

            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.delete().status_code, 204)

            self.assertFalse(storage.exists(names["only"]))
            self.assertFalse(storage.exists(names["cover"]))
            self.assertTrue(storage.exists(names["shared"]))

    def test_a_file_that_cannot_be_deleted_does_not_undo_the_deletion(self):
        task = ms.Task.objects.create(user=self.ana, title="Foto", pch="consejos", image="tasks/no-existe.png")
        with mock.patch("django.core.files.storage.FileSystemStorage.delete", side_effect=OSError("disco")):
            with self.assertLogs("api.account", "ERROR"), self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.delete().status_code, 204)
        self.assertFalse(ms.Task.objects.filter(pk=task.pk).exists())

    def test_public_page_explains_how_to_delete(self):
        with override_settings(SUPPORT_EMAIL="ayuda@example.com"):
            response = APIClient().get(reverse("account-deletion-page"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aportaciones")
        self.assertContains(response, "ayuda@example.com")
        with override_settings(SUPPORT_EMAIL=""):
            self.assertNotContains(APIClient().get(reverse("account-deletion-page")), "Sin la app")


@override_settings(STRIPE_SECRET_KEY="sk_test")
class DeleteAccountBillingTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.ana)
        patcher = mock.patch("api.subscriptions.stripe")
        self.stripe = patcher.start()
        self.addCleanup(patcher.stop)
        self.stripe.StripeError = real_stripe.StripeError
        self.stripe.InvalidRequestError = real_stripe.InvalidRequestError

    def subscription(self, subscriber, recipient, stripe_id, status="active"):
        return ms.Subscription.objects.create(
            subscriber=subscriber, recipient=recipient, amount_cents=1600, currency="mxn",
            status=status, stripe_subscription_id=stripe_id,
            current_period_end=timezone.now() + timedelta(days=20),
        )

    def delete(self):
        return self.client.post(reverse("user-delete"), {"password": PASSWORD}, format="json")

    def cancelled_ids(self):
        return {call.args[0] for call in self.stripe.Subscription.cancel.call_args_list}

    def test_cancels_what_she_pays_and_what_she_receives(self):
        self.subscription(self.ana, self.luis, "sub_paid")
        self.subscription(self.luis, self.ana, "sub_received")
        self.subscription(self.ana, self.ana, "sub_own")
        self.subscription(self.ana, self.luis, "sub_old", status="canceled")
        self.subscription(self.ana, self.luis, "")
        self.assertEqual(self.delete().status_code, 204)
        self.assertEqual(self.cancelled_ids(), {"sub_paid", "sub_received", "sub_own"})
        self.assertFalse(ms.Subscription.objects.exists())

    def test_the_person_she_paid_for_loses_the_badge(self):
        self.subscription(self.ana, self.luis, "sub_paid")
        ms.Profile.objects.filter(user=self.luis).update(subscriptionActive=True, subscription_amount=1)
        self.assertEqual(self.delete().status_code, 204)
        profile = ms.Profile.objects.get(user=self.luis)
        self.assertFalse(profile.subscriptionActive)
        self.assertEqual(profile.subscription_amount, 0)

    def test_keeps_the_account_if_stripe_fails_and_retry_continues(self):
        self.subscription(self.ana, self.luis, "sub_first")
        self.subscription(self.ana, self.luis, "sub_second")
        self.stripe.Subscription.cancel.side_effect = [None, real_stripe.APIConnectionError("sin red")]
        with self.assertLogs("api.account", "ERROR"):
            self.assertEqual(self.delete().status_code, 503)
        self.assertTrue(User.objects.filter(pk=self.ana.pk).exists())
        self.assertEqual(ms.Subscription.objects.filter(status="canceled").count(), 1)

        self.stripe.Subscription.cancel.reset_mock(side_effect=True)
        self.assertEqual(self.delete().status_code, 204)
        self.assertEqual(self.stripe.Subscription.cancel.call_count, 1)

    @override_settings(STRIPE_SECRET_KEY="")
    def test_keeps_the_account_if_it_has_live_charges_and_stripe_is_not_configured(self):
        self.subscription(self.ana, self.luis, "sub_live")
        with self.assertLogs("api.account", "ERROR"):
            self.assertEqual(self.delete().status_code, 503)
        self.assertTrue(User.objects.filter(pk=self.ana.pk).exists())

    def test_a_subscription_stripe_no_longer_knows_does_not_block_it(self):
        self.subscription(self.ana, self.luis, "sub_gone")
        self.stripe.Subscription.cancel.side_effect = real_stripe.InvalidRequestError(
            "No such subscription", "id", code="resource_missing",
        )
        self.assertEqual(self.delete().status_code, 204)

    def test_a_subscription_already_cancelled_in_stripe_does_not_block_it(self):
        self.subscription(self.ana, self.luis, "sub_ended")
        self.stripe.Subscription.cancel.side_effect = real_stripe.InvalidRequestError("Ya cancelada", "id")
        self.stripe.Subscription.retrieve.return_value = {"status": "canceled"}
        self.assertEqual(self.delete().status_code, 204)

    def test_another_stripe_refusal_keeps_the_account(self):
        self.subscription(self.ana, self.luis, "sub_live")
        self.stripe.Subscription.cancel.side_effect = real_stripe.InvalidRequestError("No", "id")
        self.stripe.Subscription.retrieve.return_value = {"status": "active"}
        with self.assertLogs("api.account", "ERROR"):
            self.assertEqual(self.delete().status_code, 503)
        self.assertTrue(User.objects.filter(pk=self.ana.pk).exists())

    def test_deletes_her_from_stripe_without_depending_on_it(self):
        ms.Profile.objects.filter(user=self.ana).update(stripe_customer_id="cus_ana")
        self.stripe.Customer.delete.side_effect = real_stripe.APIConnectionError("sin red")
        with self.assertLogs("api.subscriptions", "ERROR"):
            self.assertEqual(self.delete().status_code, 204)
        self.stripe.Customer.delete.assert_called_once_with("cus_ana", api_key="sk_test")
