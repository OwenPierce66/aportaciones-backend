"""Entrar con Google.

La app manda el id_token que Google le dio a la persona y aquí se verifica: firmado por
Google (con sus claves públicas), para uno de nuestros IDs de cliente (GOOGLE_CLIENT_IDS),
vigente y con el correo verificado. Se entra a la cuenta con ese correo o se crea una sin
contraseña ni nombre de usuario (el perfil pide elegirlo; para entrar también con correo,
"¿Olvidaste tu contraseña?" le pone una). Responde como auth/login/.
"""
import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from . import throttling as ts

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
# Margen para relojes un poco desfasados entre Google y el servidor.
CLOCK_LEEWAY_SECONDS = 60

# Las claves de Google se guardan unos minutos entre peticiones (Google las rota cada pocos días).
_google_keys = jwt.PyJWKClient(GOOGLE_CERTS_URL, timeout=10)


def verify_google_id_token(token):
    """Los datos (claims) del id_token si es válido; si no, None."""
    if not token or not settings.GOOGLE_CLIENT_IDS:
        return None
    try:
        key = _google_keys.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=settings.GOOGLE_CLIENT_IDS, leeway=CLOCK_LEEWAY_SECONDS,
        )
    except jwt.PyJWTError:
        return None
    if claims.get("iss") not in GOOGLE_ISSUERS or not claims.get("email"):
        return None
    if str(claims.get("email_verified")).lower() != "true":
        return None
    return claims


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ts.LoginThrottle])
def google_login(request):
    claims = verify_google_id_token(request.data.get("id_token"))
    if claims is None:
        return Response(
            {"detail": "No pudimos verificar tu cuenta de Google. Inténtalo de nuevo."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    User = get_user_model()
    user = User.objects.filter(email__iexact=claims["email"]).first()
    created = user is None
    if created:
        user = User.objects.create_user(
            email=claims["email"],
            first_name=claims.get("given_name") or "",
            last_name=claims.get("family_name") or "",
        )
    elif not user.is_active:
        return Response({"detail": "Esta cuenta está desactivada."}, status=status.HTTP_403_FORBIDDEN)
    refresh = RefreshToken.for_user(user)
    return Response({"refresh": str(refresh), "access": str(refresh.access_token), "created": created})
