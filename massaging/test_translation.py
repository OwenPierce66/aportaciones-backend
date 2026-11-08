"""Tests del traductor de mensajes (directos y de grupo)."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from api.models import Profile

from .models import Group, GroupMembership, GroupMessage, Message, MessageTranslation
from .translation import (
    DeepLProvider,
    GoogleProvider,
    TranslationError,
    TranslationNotConfigured,
    normalize_language,
)

User = get_user_model()
PATCH_TARGET = "massaging.translation_views.translate_text"


def make_user(name):
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", password="test-password"
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


class DirectMessageTranslationTests(TestCase):
    def setUp(self):
        self.sender = make_user("t_sender")
        self.receiver = make_user("t_receiver")
        self.outsider = make_user("t_outsider")
        self.message = Message.objects.create(
            sender=self.sender, receiver=self.receiver, content="Hello world"
        )
        self.url = reverse("translate-message", kwargs={"message_id": self.message.id})

    def test_translates_to_the_users_language_and_caches(self):
        with mock.patch(PATCH_TARGET, return_value=("Hola mundo", "en", "deepl")) as provider:
            first = client_for(self.receiver).post(self.url)
            second = client_for(self.receiver).post(self.url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["text"], "Hola mundo")
        self.assertEqual(first.data["source_lang"], "en")
        self.assertEqual(first.data["target_lang"], "es")
        self.assertFalse(first.data["cached"])
        self.assertTrue(second.data["cached"])
        # El proveedor (y su costo) solo se usa una vez por mensaje e idioma.
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(MessageTranslation.objects.count(), 1)
        # El original no se toca.
        self.message.refresh_from_db()
        self.assertEqual(self.message.content, "Hello world")

    def test_uses_profile_language_and_explicit_target(self):
        profile, _ = Profile.objects.get_or_create(user=self.receiver)
        profile.language = "fr"
        profile.save()
        with mock.patch(PATCH_TARGET, return_value=("Bonjour le monde", "en", "deepl")) as provider:
            res = client_for(self.receiver).post(self.url)
            self.assertEqual(res.data["target_lang"], "fr")
            provider.assert_called_once_with("Hello world", "fr")
            # Pedir otro idioma explícitamente crea otra traducción.
            other = client_for(self.receiver).post(self.url, {"target": "de"}, format="json")
        self.assertEqual(other.data["target_lang"], "de")
        self.assertEqual(MessageTranslation.objects.count(), 2)

    def test_same_language_is_flagged(self):
        with mock.patch(PATCH_TARGET, return_value=("Hello world", "es", "deepl")):
            res = client_for(self.receiver).post(self.url, {"target": "es"}, format="json")
        self.assertTrue(res.data["same_language"])

    def test_sender_can_translate_own_message(self):
        with mock.patch(PATCH_TARGET, return_value=("Hola mundo", "en", "deepl")):
            self.assertEqual(client_for(self.sender).post(self.url).status_code, 200)

    def test_editing_the_message_discards_stored_translations(self):
        with mock.patch(PATCH_TARGET, return_value=("Hola mundo", "en", "deepl")):
            client_for(self.receiver).post(self.url)
        self.assertEqual(MessageTranslation.objects.count(), 1)
        edit_url = reverse("update-message", kwargs={"message_id": self.message.id})
        res = client_for(self.sender).patch(edit_url, {"content": "Good morning"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(MessageTranslation.objects.count(), 0)
        with mock.patch(PATCH_TARGET, return_value=("Buenos días", "en", "deepl")):
            again = client_for(self.receiver).post(self.url)
        self.assertEqual(again.data["text"], "Buenos días")
        self.assertFalse(again.data["cached"])

    def test_outsider_gets_404(self):
        with mock.patch(PATCH_TARGET) as provider:
            res = client_for(self.outsider).post(self.url)
        self.assertEqual(res.status_code, 404)
        provider.assert_not_called()

    def test_requires_authentication(self):
        self.assertEqual(APIClient().post(self.url).status_code, 401)

    def test_message_without_text_is_rejected(self):
        empty = Message.objects.create(sender=self.sender, receiver=self.receiver, content="  ")
        url = reverse("translate-message", kwargs={"message_id": empty.id})
        with mock.patch(PATCH_TARGET) as provider:
            res = client_for(self.receiver).post(url)
        self.assertEqual(res.status_code, 400)
        provider.assert_not_called()

    def test_shared_content_prefix_is_not_translated(self):
        shared = Message.objects.create(
            sender=self.sender,
            receiver=self.receiver,
            content="↪ Publicación compartida: Mi tarea [task:7]\nHello friend",
        )
        url = reverse("translate-message", kwargs={"message_id": shared.id})
        with mock.patch(PATCH_TARGET, return_value=("Hola amigo", "en", "deepl")) as provider:
            res = client_for(self.receiver).post(url)
        self.assertEqual(res.status_code, 200)
        provider.assert_called_once_with("Hello friend", "es")

    def test_shared_content_without_body_is_rejected(self):
        shared = Message.objects.create(
            sender=self.sender,
            receiver=self.receiver,
            content="↪ Respuesta a tu historia: [story:3]",
        )
        url = reverse("translate-message", kwargs={"message_id": shared.id})
        with mock.patch(PATCH_TARGET) as provider:
            res = client_for(self.receiver).post(url)
        self.assertEqual(res.status_code, 400)
        provider.assert_not_called()

    @override_settings(TRANSLATION_MAX_CHARS=5)
    def test_too_long_message_is_rejected(self):
        with mock.patch(PATCH_TARGET) as provider:
            res = client_for(self.receiver).post(self.url)
        self.assertEqual(res.status_code, 400)
        provider.assert_not_called()

    def test_provider_not_configured_returns_503(self):
        with mock.patch(PATCH_TARGET, side_effect=TranslationNotConfigured("x")):
            res = client_for(self.receiver).post(self.url)
        self.assertEqual(res.status_code, 503)
        self.assertEqual(MessageTranslation.objects.count(), 0)

    def test_provider_failure_returns_502_and_stores_nothing(self):
        with mock.patch(PATCH_TARGET, side_effect=TranslationError("boom")):
            res = client_for(self.receiver).post(self.url)
        self.assertEqual(res.status_code, 502)
        self.assertEqual(MessageTranslation.objects.count(), 0)


class GroupMessageTranslationTests(TestCase):
    def setUp(self):
        self.member = make_user("g_member")
        self.outsider = make_user("g_outsider")
        self.group = Group.objects.create(name="grupo", created_by=self.member)
        GroupMembership.objects.create(group=self.group, user=self.member)
        self.message = GroupMessage.objects.create(
            group=self.group, sender=self.member, content="Good morning"
        )
        self.url = reverse("translate-group-message", kwargs={"message_id": self.message.id})

    def test_member_can_translate(self):
        with mock.patch(PATCH_TARGET, return_value=("Buenos días", "en", "deepl")):
            res = client_for(self.member).post(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["text"], "Buenos días")
        self.assertEqual(MessageTranslation.objects.filter(group_message=self.message).count(), 1)

    def test_non_member_gets_404(self):
        with mock.patch(PATCH_TARGET) as provider:
            res = client_for(self.outsider).post(self.url)
        self.assertEqual(res.status_code, 404)
        provider.assert_not_called()


class TranslationLanguageTests(TestCase):
    def setUp(self):
        self.user = make_user("l_user")
        self.client = client_for(self.user)
        self.url = reverse("translation-language")

    def test_defaults_to_spanish_and_lists_languages(self):
        data = self.client.get(self.url).data
        self.assertEqual(data["language"], "es")
        self.assertIn("en", {item["code"] for item in data["supported"]})

    def test_can_change_language(self):
        res = self.client.patch(self.url, {"language": "pt"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["language"], "pt")
        self.assertEqual(Profile.objects.get(user=self.user).language, "pt")

    def test_rejects_unknown_language(self):
        res = self.client.patch(self.url, {"language": "xx"}, format="json")
        self.assertEqual(res.status_code, 400)

    @override_settings(TRANSLATION_PROVIDER="deepl", DEEPL_API_KEY="")
    def test_reports_when_translator_is_not_configured(self):
        self.assertFalse(self.client.get(self.url).data["configured"])

    @override_settings(TRANSLATION_PROVIDER="deepl", DEEPL_API_KEY="k:fx")
    def test_reports_when_translator_is_configured(self):
        self.assertTrue(self.client.get(self.url).data["configured"])


def fake_response(status_code=200, payload=None):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = payload or {}
    return response


class ProviderTests(SimpleTestCase):
    def test_normalize_language(self):
        self.assertEqual(normalize_language("EN-us"), "en")
        self.assertEqual(normalize_language("pt_BR"), "pt")
        self.assertEqual(normalize_language("xx"), "es")
        self.assertEqual(normalize_language(None, default=""), "")

    def test_deepl_requires_key(self):
        with self.assertRaises(TranslationNotConfigured):
            DeepLProvider("")

    def test_deepl_free_key_uses_free_host_and_regional_target(self):
        payload = {"translations": [{"text": "Hello", "detected_source_language": "ES"}]}
        with mock.patch("massaging.translation.requests.post", return_value=fake_response(200, payload)) as post:
            text, detected = DeepLProvider("abc:fx").translate("Hola", "en")
        self.assertEqual((text, detected), ("Hello", "es"))
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api-free.deepl.com/v2/translate")
        self.assertEqual(kwargs["json"]["target_lang"], "EN-US")
        self.assertEqual(kwargs["headers"]["Authorization"], "DeepL-Auth-Key abc:fx")

    def test_deepl_paid_key_uses_paid_host(self):
        self.assertTrue(DeepLProvider("abc").url.startswith("https://api.deepl.com/"))

    def test_deepl_quota_exhausted(self):
        with mock.patch("massaging.translation.requests.post", return_value=fake_response(456)):
            with self.assertRaises(TranslationError):
                DeepLProvider("abc:fx").translate("Hola", "en")

    def test_deepl_unexpected_payload(self):
        with mock.patch("massaging.translation.requests.post", return_value=fake_response(200, {})):
            with self.assertRaises(TranslationError):
                DeepLProvider("abc:fx").translate("Hola", "en")

    def test_google_parses_response(self):
        payload = {"data": {"translations": [{"translatedText": "Hello", "detectedSourceLanguage": "es"}]}}
        with mock.patch("massaging.translation.requests.post", return_value=fake_response(200, payload)):
            self.assertEqual(GoogleProvider("key").translate("Hola", "en"), ("Hello", "es"))

    def test_google_requires_key(self):
        with self.assertRaises(TranslationNotConfigured):
            GoogleProvider("")

    @staticmethod
    def gemini_payload(translation="Hello", source_lang="es"):
        import json as _json
        body = _json.dumps({"translation": translation, "source_lang": source_lang})
        return {"candidates": [{"content": {"parts": [{"text": body}]}}]}

    def test_gemini_parses_response_and_sends_key_in_header(self):
        from .translation import GeminiProvider
        with mock.patch("massaging.translation.requests.post", return_value=fake_response(200, self.gemini_payload())) as post:
            result = GeminiProvider("key").translate("Hola", "en")
        self.assertEqual(result, ("Hello", "es"))
        args, kwargs = post.call_args
        self.assertIn("gemini-3.1-flash-lite:generateContent", args[0])
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "key")
        self.assertNotIn("key=", args[0])
        # El mensaje va como dato del usuario, no dentro de las instrucciones.
        self.assertEqual(kwargs["json"]["contents"][0]["parts"][0]["text"], "Hola")
        self.assertNotIn("Hola", kwargs["json"]["systemInstruction"]["parts"][0]["text"])

    def test_gemini_requires_key(self):
        from .translation import GeminiProvider
        with self.assertRaises(TranslationNotConfigured):
            GeminiProvider("")

    def test_gemini_rate_limit_and_bad_payloads(self):
        from .translation import GeminiProvider
        bad_json = {"candidates": [{"content": {"parts": [{"text": "no es json"}]}}]}
        for response in (fake_response(429), fake_response(500), fake_response(200, {}), fake_response(200, bad_json),
                         fake_response(200, self.gemini_payload(translation="  "))):
            with mock.patch("massaging.translation.requests.post", return_value=response):
                with self.assertRaises(TranslationError):
                    GeminiProvider("key").translate("Hola", "en")

    def test_network_failures_become_translation_errors_without_logging_the_key(self):
        import requests
        from .translation import GeminiProvider
        url_with_key = "https://translation.googleapis.com/language/translate/v2?key=secreta"
        for provider in (DeepLProvider("abc:fx"), GoogleProvider("secreta"), GeminiProvider("secreta")):
            for failure in (requests.Timeout(f"Read timed out: {url_with_key}"),
                            requests.ConnectionError(f"Max retries exceeded with url: {url_with_key}")):
                with self.subTest(provider=provider.name, failure=type(failure).__name__):
                    with mock.patch("massaging.translation.requests.post", side_effect=failure):
                        with self.assertLogs("massaging.translation", level="WARNING") as logs:
                            with self.assertRaises(TranslationError):
                                provider.translate("Hola", "en")
                    self.assertNotIn("secreta", " ".join(logs.output))

    @override_settings(TRANSLATION_PROVIDER="gemini", GEMINI_API_KEY="k", GEMINI_TRANSLATE_MODEL="gemini-3.5-flash-lite")
    def test_get_provider_gemini_uses_configured_model(self):
        from .translation import get_provider
        provider = get_provider()
        self.assertEqual((provider.name, provider.model), ("gemini", "gemini-3.5-flash-lite"))

    @override_settings(TRANSLATION_PROVIDER="gemini", GEMINI_API_KEY="")
    def test_get_provider_gemini_not_configured(self):
        from .translation import get_provider
        with self.assertRaises(TranslationNotConfigured):
            get_provider()

    @override_settings(TRANSLATION_PROVIDER="nope")
    def test_unknown_provider(self):
        from .translation import get_provider
        with self.assertRaises(TranslationNotConfigured):
            get_provider()


class TranslationPreviewTests(TestCase):
    """Traducción en vivo al escribir (sin guardar nada)."""

    def setUp(self):
        self.user = make_user("p_user")
        self.url = reverse("translation-preview")

    def test_requires_authentication(self):
        self.assertEqual(APIClient().post(self.url, {"text": "Hola"}, format="json").status_code, 401)

    def test_translates_to_english_by_default_and_passes_source(self):
        with mock.patch(PATCH_TARGET, return_value=("Hello preview one", "es", "deepl")) as provider:
            res = client_for(self.user).post(
                self.url, {"text": "Hola vista previa uno", "source": "es"}, format="json"
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["text"], "Hello preview one")
        self.assertEqual(res.data["target_lang"], "en")
        self.assertEqual(res.data["source_lang"], "es")
        provider.assert_called_once_with("Hola vista previa uno", "en", "es")
        # No guarda nada: es solo un borrador.
        self.assertEqual(MessageTranslation.objects.count(), 0)

    def test_same_draft_is_served_from_cache(self):
        body = {"text": "Texto repetido de prueba", "target": "en", "source": "es"}
        with mock.patch(PATCH_TARGET, return_value=("Repeated test text", "es", "deepl")) as provider:
            client_for(self.user).post(self.url, body, format="json")
            again = client_for(self.user).post(self.url, body, format="json")
        self.assertEqual(again.data["text"], "Repeated test text")
        self.assertEqual(provider.call_count, 1)

    def test_empty_and_too_long_text_are_rejected(self):
        with mock.patch(PATCH_TARGET) as provider:
            empty = client_for(self.user).post(self.url, {"text": "   "}, format="json")
            with override_settings(TRANSLATION_MAX_CHARS=3):
                long = client_for(self.user).post(self.url, {"text": "demasiado largo"}, format="json")
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(long.status_code, 400)
        provider.assert_not_called()

    def test_unknown_source_is_ignored(self):
        with mock.patch(PATCH_TARGET, return_value=("Hello unknown", "es", "deepl")) as provider:
            client_for(self.user).post(
                self.url, {"text": "Hola desconocido", "source": "zz"}, format="json"
            )
        provider.assert_called_once_with("Hola desconocido", "en", None)

    def test_provider_errors(self):
        with mock.patch(PATCH_TARGET, side_effect=TranslationNotConfigured("x")):
            self.assertEqual(
                client_for(self.user).post(self.url, {"text": "Hola error uno"}, format="json").status_code, 503
            )
        with mock.patch(PATCH_TARGET, side_effect=TranslationError("x")):
            self.assertEqual(
                client_for(self.user).post(self.url, {"text": "Hola error dos"}, format="json").status_code, 502
            )


class SentTranslationTests(TestCase):
    """La traducción que el autor ve al escribir viaja con su mensaje."""

    def setUp(self):
        self.sender = make_user("s_sender")
        self.receiver = make_user("s_receiver")
        self.outsider = make_user("s_outsider")

    def _send_direct(self, extra=None):
        data = {"receiver": self.receiver.id, "content": "Hola amigo"}
        data.update(extra or {})
        return client_for(self.sender).post(reverse("message-list"), data, format="multipart")

    def test_direct_message_carries_the_translation(self):
        res = self._send_direct(
            {"translation_text": "Hello friend", "translation_lang": "en", "translation_source": "es"}
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(
            res.data["sent_translation"],
            {"text": "Hello friend", "lang": "en", "source_lang": "es"},
        )
        stored = MessageTranslation.objects.get()
        self.assertEqual(stored.provider, "sender")

        # El receptor la ve al listar la conversación, sin consultar al proveedor.
        with mock.patch(PATCH_TARGET) as provider:
            listing = client_for(self.receiver).get(
                reverse("message-list"), {"user_id": self.sender.id}
            )
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data[0]["sent_translation"]["text"], "Hello friend")
        provider.assert_not_called()

    def test_message_without_translation_has_none(self):
        res = self._send_direct()
        self.assertEqual(res.status_code, 201)
        self.assertIsNone(res.data["sent_translation"])
        self.assertEqual(MessageTranslation.objects.count(), 0)

    def test_invalid_translation_data_is_ignored_but_message_is_sent(self):
        res = self._send_direct({"translation_text": "Hello", "translation_lang": "zz"})
        self.assertEqual(res.status_code, 201)
        self.assertIsNone(res.data["sent_translation"])
        res = self._send_direct({"translation_text": "   ", "translation_lang": "en"})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(MessageTranslation.objects.count(), 0)

    @override_settings(TRANSLATION_MAX_CHARS=5)
    def test_too_long_translation_is_ignored(self):
        res = self._send_direct({"translation_text": "Hello friend", "translation_lang": "en"})
        self.assertEqual(res.status_code, 201)
        self.assertIsNone(res.data["sent_translation"])

    def test_tapping_the_message_reuses_the_sent_translation(self):
        sent = self._send_direct({"translation_text": "Hello friend", "translation_lang": "en"})
        url = reverse("translate-message", kwargs={"message_id": sent.data["id"]})
        with mock.patch(PATCH_TARGET) as provider:
            res = client_for(self.receiver).post(url, {"target": "en"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["text"], "Hello friend")
        self.assertTrue(res.data["cached"])
        provider.assert_not_called()

    def test_group_message_carries_the_translation(self):
        group = Group.objects.create(name="Grupo", created_by=self.sender)
        GroupMembership.objects.create(group=group, user=self.sender)
        GroupMembership.objects.create(group=group, user=self.receiver)
        res = client_for(self.sender).post(
            reverse("send-group-message", kwargs={"group_id": group.id}),
            {"content": "Hola grupo", "translation_text": "Hello group", "translation_lang": "en"},
            format="multipart",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["sent_translation"]["text"], "Hello group")
        listing = client_for(self.receiver).get(
            reverse("group-messages", kwargs={"group_id": group.id})
        )
        self.assertEqual(listing.data[0]["sent_translation"]["lang"], "en")
