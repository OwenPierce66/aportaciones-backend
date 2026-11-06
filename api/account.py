"""Recuperar la contraseña con un código por correo y eliminar la cuenta.

- Recuperar: se envía un código de 6 dígitos que vence en 15 minutos. La respuesta es la
  misma exista o no la cuenta, para no revelar qué correos están registrados.
- Eliminar: pide la contraseña o volver a elegir la cuenta de Google del mismo correo (las
  cuentas creadas con Google no tienen contraseña), cancela los cobros en Stripe y borra la
  cuenta con todo lo suyo, incluidos los archivos que subió. Si Stripe falla, la cuenta sigue intacta: borrarla
  dejaría cobros activos sin que nadie pueda cancelarlos.
"""
import logging
import secrets
import threading
from datetime import timedelta

import stripe
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models import FileField, Q
from django.db.models.deletion import Collector
from django.shortcuts import render
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from massaging.models import Group as ChatGroup, GroupMembership as ChatMembership

from . import models as ms
from . import throttling as ts
from .app_account import app_account_email
from .community_groups import hand_over_groups
from .google_auth import verify_google_id_token
from .subscriptions import SubscriptionsUnavailable, end_billing, refresh_profile_subscription

logger = logging.getLogger(__name__)
User = get_user_model()

NO_EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
CODE_LIFETIME = timedelta(minutes=15)
MAX_ATTEMPTS = 5
RESEND_GAP = timedelta(seconds=60)
MAX_CODES_PER_HOUR = 5
CODE_SENT = "Si existe una cuenta con ese correo, te enviamos un código."
BAD_CODE = "El código no es válido o ya venció. Pide uno nuevo."


def _in_background(fn, *args):
    """Sin esperar al proveedor: si tardara solo cuando la cuenta existe, la delataría."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def _user_by_email(email):
    users = User.objects.filter(email__iexact=email, is_active=True)
    return users.filter(email=email).first() or users.first()


def _hash_code(user, code):
    return salted_hmac("api.password_reset", f"{user.pk}:{code}", algorithm="sha256").hexdigest()


def _email_code(address, code):
    try:
        send_mail(
            "Tu código para recuperar tu contraseña de Aportaciones",
            f"Tu código para cambiar tu contraseña de Aportaciones es:\n\n{code}\n\n"
            "Vence en 15 minutos. Si no lo pediste, ignora este correo: tu contraseña no cambia.",
            settings.DEFAULT_FROM_EMAIL,
            [address],
        )
    except Exception:
        logger.exception("No se pudo enviar el código para recuperar la contraseña")


def _send_code(user):
    now = timezone.now()
    recent = ms.PasswordResetCode.objects.filter(user=user, created_at__gte=now - timedelta(hours=1))
    if recent.count() >= MAX_CODES_PER_HOUR or recent.filter(created_at__gte=now - RESEND_GAP).exists():
        return
    ms.PasswordResetCode.objects.filter(user=user, created_at__lt=now - timedelta(days=1)).delete()
    code = f"{secrets.randbelow(1_000_000):06d}"
    ms.PasswordResetCode.objects.create(
        user=user, code_hash=_hash_code(user, code), expires_at=now + CODE_LIFETIME,
    )
    _in_background(_email_code, user.email, code)


def _code_is_valid(user, code):
    """Solo vale el último código pedido, y cada intento fallido cuenta aunque luego se acierte."""
    with transaction.atomic():
        reset = (
            ms.PasswordResetCode.objects.select_for_update()
            .filter(user=user).order_by("-created_at").first()
        )
        if not reset or reset.expires_at <= timezone.now() or reset.attempts >= MAX_ATTEMPTS:
            return False
        if constant_time_compare(_hash_code(user, code), reset.code_hash):
            return True
        reset.attempts += 1
        reset.save(update_fields=["attempts"])
        return False


def revoke_sessions(user):
    BlacklistedToken.objects.bulk_create(
        [BlacklistedToken(token=token) for token in OutstandingToken.objects.filter(user=user)],
        ignore_conflicts=True,
    )
    user.push_tokens.all().delete()


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ts.PasswordResetThrottle])
def password_reset_request(request):
    email = str(request.data.get("email") or "").strip()
    try:
        validate_email(email)
    except DjangoValidationError:
        return Response({"email": ["Ingresa un correo válido."]}, status=status.HTTP_400_BAD_REQUEST)
    if settings.EMAIL_BACKEND == NO_EMAIL_BACKEND:
        return Response(
            {"detail": "Por ahora no podemos enviar correos. Intenta más tarde."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    user = _user_by_email(email)
    if user:
        _send_code(user)
    return Response({"detail": CODE_SENT})


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ts.PasswordResetConfirmThrottle])
def password_reset_confirm(request):
    email = str(request.data.get("email") or "").strip()
    code = str(request.data.get("code") or "").strip()
    new_password = str(request.data.get("new_password") or "")

    user = _user_by_email(email) if email else None
    if not user or not _code_is_valid(user, code):
        return Response({"code": [BAD_CODE]}, status=status.HTTP_400_BAD_REQUEST)
    try:
        validate_password(new_password, user=user)
    except DjangoValidationError as exc:
        # El código sigue vivo: solo hay que elegir otra contraseña.
        return Response({"new_password": exc.messages}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        user.set_password(new_password)
        user.save(update_fields=["password", "updated_at"])
        user.password_reset_codes.all().delete()
        revoke_sessions(user)
    # La app deja escrito este correo en Iniciar sesión.
    return Response({"detail": "Tu contraseña se cambió. Ya puedes iniciar sesión.", "email": user.email})


def _hand_over_chats(user):
    """Un chat de grupo sigue con su gente: lo hereda el admin o el integrante más antiguo."""
    chats = ChatGroup.objects.filter(
        Q(created_by=user) | Q(groupmembership__user=user, groupmembership__is_admin=True)
    ).distinct()
    for chat in chats:
        others = ChatMembership.objects.filter(group=chat).exclude(user=user)
        if others.filter(is_admin=True).exists() and chat.created_by_id != user.pk:
            continue
        heir = others.order_by("-is_admin", "joined_at").first()
        if not heir:
            continue
        heir.is_admin = True
        heir.save(update_fields=["is_admin"])
        if chat.created_by_id == user.pk:
            chat.created_by = heir.user
            chat.save(update_fields=["created_by"])


def _file_fields(model):
    return [field for field in model._meta.concrete_fields if isinstance(field, FileField)]


def _uploaded_files(collector):
    """Los archivos de las filas que la cascada va a borrar: {(storage, nombre)}."""
    sources = [
        (model, model._base_manager.filter(pk__in=[obj.pk for obj in objs]))
        for model, objs in collector.data.items()
    ] + [(queryset.model, queryset) for queryset in collector.fast_deletes]
    files = set()
    for model, queryset in sources:
        for field in _file_fields(model):
            files.update((field.storage, name) for name in queryset.values_list(field.name, flat=True) if name)
    return files


def _still_in_use(names):
    """Los que aún usa otra fila, como la copia de una publicación compartida."""
    used = set()
    for model in apps.get_models():
        for field in _file_fields(model):
            used.update(
                model._base_manager.filter(**{f"{field.name}__in": names}).values_list(field.name, flat=True)
            )
    return used


def _delete_files(files):
    for storage, name in files:
        try:
            storage.delete(name)
        except Exception:
            logger.exception("No se pudo borrar el archivo %s de una cuenta eliminada", name)


def erase_account(user):
    """Borra la cuenta y todo lo suyo: las filas juntas y los archivos al confirmarse."""
    paid_for = set(
        ms.Subscription.objects.filter(subscriber=user).exclude(recipient=user)
        .values_list("recipient_id", flat=True)
    )
    with transaction.atomic():
        empty_groups = hand_over_groups(user)
        _hand_over_chats(user)
        # Las notificaciones que causó guardan su nombre en el texto y sobrevivirían a la cuenta.
        ms.Notification.objects.filter(actor=user).delete()

        collector = Collector(using=DEFAULT_DB_ALIAS)
        collector.collect([user])
        if empty_groups:
            collector.collect(empty_groups)
        files = _uploaded_files(collector)

        for group in empty_groups:
            group.delete()
        user.delete()

        for recipient_id in paid_for:
            refresh_profile_subscription(recipient_id)
        used = _still_in_use({name for _, name in files})
        orphans = [(storage, name) for storage, name in files if name not in used]
        transaction.on_commit(lambda: _in_background(_delete_files, orphans))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ts.DeleteAccountThrottle])
def delete_account(request):
    user = request.user
    id_token = request.data.get("id_token")
    if id_token:
        claims = verify_google_id_token(str(id_token))
        if claims is None:
            return Response(
                {"id_token": ["No pudimos verificar tu cuenta de Google. Inténtalo de nuevo."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if claims["email"].lower() != user.email.lower():
            return Response(
                {"id_token": [f"Elige la cuenta de Google {user.email}."]}, status=status.HTTP_400_BAD_REQUEST,
            )
    elif not user.check_password(str(request.data.get("password") or "")):
        return Response({"password": ["La contraseña no es correcta."]}, status=status.HTTP_400_BAD_REQUEST)
    if user.is_staff or user.is_superuser or user.email.lower() == app_account_email().lower():
        return Response(
            {"detail": "Esta cuenta administra la app y no se puede eliminar desde aquí."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        end_billing(user)
    except (stripe.StripeError, SubscriptionsUnavailable):
        logger.exception("No se pudieron cancelar los cobros de la cuenta %s", user.pk)
        return Response(
            {"detail": "No pudimos cancelar tus suscripciones. Tu cuenta sigue activa: intenta de nuevo."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    erase_account(user)
    return Response(status=status.HTTP_204_NO_CONTENT)


def account_deletion_page(request):
    """Página pública que Google Play pide para eliminar la cuenta sin tener la app."""
    return render(request, "api/account_deletion.html", {"support_email": settings.SUPPORT_EMAIL})
