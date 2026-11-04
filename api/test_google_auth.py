"""Entrar con Google: el id_token debe venir firmado por Google, ser para la app, estar
vigente y con el correo verificado. Entra a la cuenta con ese correo o crea una sin usuario
ni contraseña."""
import time
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from . import google_auth

User = get_user_model()
CLIENT_ID = "aportaciones-web.apps.googleusercontent.com"
GOOGLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def google_token(key=GOOGLE_KEY, **claims):
    now = int(time.time())
    payload = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "109876543210",
        "email": "ana.lopez@gmail.com",
        "email_verified": True,
        "given_name": "Ana",
        "family_name": "López",
        "iat": now,
        "exp": now + 3600,
        **claims,
    }
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "clave-de-prueba"})


@override_settings(GOOGLE_CLIENT_IDS=[CLIENT_ID])
class GoogleLoginTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        # Las claves públicas de Google, sin ir a internet.
        patcher = mock.patch.object(
            google_auth._google_keys, "get_signing_key_from_jwt",
            return_value=mock.Mock(key=GOOGLE_KEY.public_key()),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self, token):
        return self.client.post(reverse("google-login"), {"id_token": token}, format="json")

    def test_crea_la_cuenta_sin_usuario_ni_contrasena(self):
        response = self.login(google_token())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["created"])
        user = User.objects.get(email="ana.lopez@gmail.com")
        self.assertEqual((user.first_name, user.last_name, user.username), ("Ana", "López", None))
        self.assertFalse(user.has_usable_password())
        self.assertEqual(str(AccessToken(response.data["access"])["user_id"]), str(user.pk))
        self.assertIn("refresh", response.data)

    def test_entra_a_la_cuenta_que_ya_tiene_ese_correo(self):
        ana = User.objects.create_user(email="Ana.Lopez@gmail.com", password="clave-9281", username="ana")
        response = self.login(google_token())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["created"])
        self.assertEqual(str(AccessToken(response.data["access"])["user_id"]), str(ana.pk))
        self.assertEqual(User.objects.filter(email__iexact="ana.lopez@gmail.com").count(), 1)
        # Sigue entrando también con su contraseña.
        ana.refresh_from_db()
        self.assertTrue(ana.check_password("clave-9281"))

    def test_rechaza_lo_que_no_es_un_token_valido_de_google_para_la_app(self):
        now = int(time.time())
        invalid = {
            "para otra app": google_token(aud="otra-app.apps.googleusercontent.com"),
            "vencido": google_token(iat=now - 7200, exp=now - 3600),
            "correo sin verificar": google_token(email_verified=False),
            "otro emisor": google_token(iss="https://evil.example.com"),
            "firma falsa": google_token(key=OTHER_KEY),
            "basura": "no-es-un-token",
        }
        for case, token in invalid.items():
            with self.subTest(case):
                response = self.login(token)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["detail"], "No pudimos verificar tu cuenta de Google. Inténtalo de nuevo.")
        self.assertFalse(User.objects.filter(email__iexact="ana.lopez@gmail.com").exists())

    def test_una_cuenta_desactivada_no_entra(self):
        User.objects.create_user(email="ana.lopez@gmail.com", is_active=False)
        response = self.login(google_token())
        self.assertEqual(response.status_code, 403)

    @override_settings(GOOGLE_CLIENT_IDS=[])
    def test_sin_ids_de_cliente_esta_apagado(self):
        response = self.login(google_token())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email__iexact="ana.lopez@gmail.com").exists())
