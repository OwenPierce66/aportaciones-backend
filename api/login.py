"""Iniciar sesión con el correo o con el nombre de usuario, sin distinguir mayúsculas.

La app manda `email` si lo escrito lleva @ y si no `username`; aquí valen los dos en
cualquiera de los campos. Si dos cuentas viejas solo se diferencian en mayúsculas, se
prueba primero la que coincide exacto y luego las demás: entra la que tenga esa contraseña.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

User = get_user_model()


def accounts_for(login):
    """Las cuentas que corresponden a lo escrito, primero la que coincide exacto."""
    field = "email" if "@" in login else "username"
    accounts = User._default_manager.filter(**{f"{field}__iexact": login})
    return sorted(accounts, key=lambda user: getattr(user, field) != login)


class EmailOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        login = str(username if username is not None else kwargs.get(User.USERNAME_FIELD) or "").strip()
        if not login or password is None:
            return None
        accounts = accounts_for(login)
        if not accounts:
            # Tarda lo mismo exista o no la cuenta: así no revela cuáles existen.
            User().set_password(password)
            return None
        for user in accounts:
            if user.check_password(password) and self.user_can_authenticate(user):
                return user
        return None


class LoginSerializer(TokenObtainPairSerializer):
    default_error_messages = {
        "no_active_account": "No encontramos una cuenta con esos datos. Revisa tu correo o usuario y tu contraseña.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = False
        self.fields["username"] = serializers.CharField(write_only=True, required=False)

    def validate(self, attrs):
        username = attrs.pop("username", "")
        attrs["email"] = attrs.get("email") or username
        if not attrs["email"]:
            raise serializers.ValidationError({"email": ["Escribe tu correo o tu nombre de usuario."]})
        return super().validate(attrs)
