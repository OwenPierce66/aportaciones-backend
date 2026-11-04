"""Aviso al staff y a los admins para que den el like de la App a una tarea.

Cuando alguien del equipo da like como la App, a los demás les llega un aviso (uno por tarea).
Deja de salirle a quien ya le dio like a la tarea o lo marca como visto (vuelve a salir si lo
desmarca); si la tarea se queda sin likes de la App, se retira para todos.
"""
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from .app_account import app_account_email
from .models import Like, Notification
from .notifications import create_notification

APP_LIKE_REQUEST = "app_like_request"


def request_key(task_id):
    return f"app-like-request:{task_id}"


def app_like_q(prefix=""):
    """Likes que cuentan como de la App: los marcados `as_app` y los de la cuenta de la App."""
    query = Q(**{f"{prefix}as_app": True})
    email = app_account_email()
    if email:
        query |= Q(**{f"{prefix}user__email__iexact": email})
    return query


def notify_app_like_request(task, actor):
    """`actor` dio like como la App a `task`: avisa al resto del equipo que aún no le da like."""
    team = (
        get_user_model().objects.filter(Q(is_staff=True) | Q(is_superuser=True), is_active=True)
        .exclude(pk=actor.pk)
        .exclude(task_likes__task=task)
    )
    key = request_key(task.pk)
    actor_name = actor.username or actor.first_name or "Alguien del equipo"
    data = {
        "title": (task.title or "")[:80],
        # Texto del push (en la app se arma con el nombre de quien lo dio).
        "excerpt": f"{actor_name} le dio like como la App. ¿Le das like tú también?",
    }
    existing = {n.recipient_id: n for n in Notification.objects.filter(dedupe_key=key, recipient__in=team)}
    for member in team:
        notification = existing.get(member.pk)
        if notification is None:
            create_notification(
                recipient=member, actor=actor, notification_type=APP_LIKE_REQUEST,
                target_type="task", target_id=task.pk, data=data, dedupe_key=key,
            )
        elif not notification.data.get("seen"):
            # Otra persona del equipo dio like como la App: vuelve a salir como nueva.
            Notification.objects.filter(pk=notification.pk).update(
                actor=actor, data={**notification.data, **data},
                is_read=False, read_at=None, created_at=timezone.now(),
            )


def retire_for_liker(like):
    """Quien ya le dio like a la tarea no necesita el aviso."""
    Notification.objects.filter(dedupe_key=request_key(like.task_id), recipient_id=like.user_id).delete()


def retire_if_no_app_likes(task_id):
    if not Like.objects.filter(task_id=task_id).filter(app_like_q()).exists():
        Notification.objects.filter(dedupe_key=request_key(task_id)).delete()


def set_seen(notification, seen):
    """Marca (o desmarca) el aviso como visto: mientras esté visto no vuelve a salir como nuevo."""
    notification.data = {**notification.data, "seen": seen}
    fields = ["data", "updated_at"]
    if seen and not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        fields += ["is_read", "read_at"]
    notification.save(update_fields=fields)
