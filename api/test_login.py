from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()
PASSWORD = "clave-vieja-9281"
NEW_PASSWORD = "clave-nueva-7316"


class LoginTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.ana = User.objects.create_user(email="Ana.Lopez@example.com", password=PASSWORD, username="AnaLopez")


class LoginTests(LoginTestCase):
    def login(self, **data):
        return self.client.post(reverse("token_obtain_pair"), {"password": PASSWORD, **data}, format="json")

    def logged_in_as(self, response):
        self.assertEqual(response.status_code, 200, response.data)
        return RefreshToken(response.data["refresh"])["user_id"]

    def test_enters_with_the_email_in_any_case(self):
        for email in ("Ana.Lopez@example.com", "ana.lopez@example.com", " ANA.LOPEZ@EXAMPLE.COM "):
            self.assertEqual(self.logged_in_as(self.login(email=email)), str(self.ana.pk))

    def test_enters_with_the_username_in_any_case(self):
        for username in ("AnaLopez", "analopez"):
            self.assertEqual(self.logged_in_as(self.login(username=username)), str(self.ana.pk))

    def test_the_username_also_works_in_the_email_field(self):
        self.assertEqual(self.logged_in_as(self.login(email="analopez")), str(self.ana.pk))

    def test_a_wrong_password_and_an_unknown_account_answer_the_same(self):
        wrong = self.login(email="ana.lopez@example.com", password="otra-clave-5150")
        unknown = self.login(email="nadie@example.com")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.data["detail"], unknown.data["detail"])

    def test_needs_an_email_or_a_username(self):
        response = self.login()
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)

    def test_an_inactive_account_cannot_enter(self):
        self.ana.is_active = False
        self.ana.save(update_fields=["is_active"])
        self.assertEqual(self.login(email="ana.lopez@example.com").status_code, 401)

    def test_old_accounts_that_differ_only_in_case_each_enter_with_their_password(self):
        twin = User.objects.create_user(email="ana.lopez@example.com", password=NEW_PASSWORD, username="ana2")
        self.assertEqual(self.logged_in_as(self.login(email="ANA.LOPEZ@example.com")), str(self.ana.pk))
        self.assertEqual(
            self.logged_in_as(self.login(email="ANA.LOPEZ@example.com", password=NEW_PASSWORD)), str(twin.pk),
        )


class RegisterTests(LoginTestCase):
    def register(self, **data):
        body = {"email": "luis@example.com", "username": "luis", "password": NEW_PASSWORD, **data}
        return self.client.post(reverse("register"), body, format="json")

    def test_registers_and_can_enter_right_away(self):
        response = self.register()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(User.objects.get(email="luis@example.com").check_password(NEW_PASSWORD))

    def test_an_email_or_username_in_use_with_other_case_is_rejected(self):
        response = self.register(email="ANA.lopez@example.com", username="analopez")
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)
        self.assertIn("username", response.data)

    def test_needs_a_password_that_is_not_weak(self):
        missing = self.client.post(reverse("register"), {"email": "luis@example.com", "username": "luis"}, format="json")
        self.assertEqual(missing.status_code, 400)
        self.assertIn("password", missing.data)
        self.assertIn("password", self.register(password="12345678").data)
        self.assertFalse(User.objects.filter(email="luis@example.com").exists())


class ChangeCredentialsTests(LoginTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.ana)

    def patch(self, **data):
        return self.client.patch(reverse("user-me"), data, format="json")

    def test_changing_the_password_needs_the_current_one(self):
        missing = self.patch(password=NEW_PASSWORD)
        wrong = self.patch(password=NEW_PASSWORD, current_password="otra-clave-5150")
        for response in (missing, wrong):
            self.assertEqual(response.status_code, 400)
            self.assertIn("current_password", response.data)
        self.ana.refresh_from_db()
        self.assertTrue(self.ana.check_password(PASSWORD))

    def test_with_the_current_one_it_changes_and_closes_the_other_sessions(self):
        other_device = RefreshToken.for_user(self.ana)
        response = self.patch(password=NEW_PASSWORD, current_password=PASSWORD)
        self.assertEqual(response.status_code, 200, response.data)
        self.ana.refresh_from_db()
        self.assertTrue(self.ana.check_password(NEW_PASSWORD))

        refresh = lambda token: self.client.post(reverse("token_refresh"), {"refresh": token}, format="json")
        self.assertEqual(refresh(str(other_device)).status_code, 401)
        self.assertEqual(refresh(response.data["refresh"]).status_code, 200)

    def test_the_new_password_cannot_be_weak(self):
        response = self.patch(password="12345678", current_password=PASSWORD)
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data)

    def test_changing_the_email_needs_the_current_password(self):
        self.assertEqual(self.patch(email="ana.nueva@example.com").status_code, 400)
        response = self.patch(email="ana.nueva@example.com", current_password=PASSWORD)
        self.assertEqual(response.status_code, 200, response.data)
        self.ana.refresh_from_db()
        self.assertEqual(self.ana.email, "ana.nueva@example.com")

    def test_cannot_take_an_email_or_username_in_use_with_other_case(self):
        User.objects.create_user(email="luis@example.com", password=PASSWORD, username="luis")
        self.assertIn("email", self.patch(email="LUIS@example.com", current_password=PASSWORD).data)
        self.assertIn("username", self.patch(username="Luis").data)

    def test_the_rest_of_the_profile_needs_no_password(self):
        response = self.patch(first_name="Ana María", email="Ana.Lopez@example.com", username="analopez")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("access", response.data)

    def test_an_account_made_with_google_puts_its_first_password_without_one(self):
        google = User.objects.create_user(email="ana.google@gmail.com")
        self.client.force_authenticate(user=google)
        self.assertEqual(self.patch(password=NEW_PASSWORD).status_code, 200)
        google.refresh_from_db()
        self.assertTrue(google.check_password(NEW_PASSWORD))
