import logging
import re
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from django.utils import timezone

from massaging.models import GroupMessage, GroupMembership, Message, MessageLike

from . import app_like_requests

from .models import (
    CommentTag,
    Favorito,
    Follow,
    Like,
    LikeCommentPost,
    LikeP,
    LikePostt,
    LikeSharedTask,
    LikeSharedTaskComment,
    NewCategory,
    CategoryP,
    NewPeticionCommentPost,
    Postt,
    Profile,
    SharedTask,
    SharedTaskComment,
    Task,
    TaskSave,
    TaskTag,
    pFavorito,
    APPROVED_TAG,
    STATUS_TAGS,
    SUBTHEME_NAMES,
    Notification,   
    PushToken,      
)
from .notifications import create_notification, retire_notification

User = get_user_model()
logger = logging.getLogger(__name__)


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        if not hasattr(instance, "profile"):
            Profile.objects.create(user=instance)


@receiver(post_save, sender=Task)
def auto_fill_personal_category_filter(sender, instance, **kwargs):
    """Réplica automática del filtro: al crear/editar una tarea, sus categorías
    PRINCIPALES se guardan en el filtro personal (CategoryP) del autor.
    Se excluyen subtemas conocidos y etiquetas de estado (procesando/aprobada).
    Nunca quita nada: si el usuario borra una entrada manualmente, se respeta."""
    if not instance.user_id or not instance.categories:
        return

    principal_names = []
    seen = set()
    for raw in instance.categories.split(","):
        name = raw.strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        if key in SUBTHEME_NAMES or key in STATUS_TAGS:
            continue
        seen.add(key)
        principal_names.append(name)

    if not principal_names:
        return

    existing = list(CategoryP.objects.filter(user_id=instance.user_id))
    existing_casefold = {c.name.casefold() for c in existing}
    next_position = (max((c.position for c in existing), default=-1)) + 1

    for name in principal_names:
        if name.casefold() in existing_casefold:
            continue
        CategoryP.objects.create(user_id=instance.user_id, name=name, position=next_position)
        next_position += 1
        existing_casefold.add(name.casefold())


def _excerpt(value, limit=140):
    value = " ".join((value or "").split())
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def _actor_name(actor):
    full_name = " ".join(
        part for part in (actor.first_name, actor.last_name) if part
    )
    return actor.username or full_name or actor.email


def _notify(
    instance,
    *,
    recipient,
    actor,
    notification_type,
    target_type,
    target_id,
    text,
    data=None,
    secondary_target_type="",
    secondary_target_id="",
):
    payload = {"text": text}
    payload.update(data or {})
    return create_notification(
        recipient=recipient,
        actor=actor,
        notification_type=notification_type,
        target_type=target_type,
        target_id=target_id,
        secondary_target_type=secondary_target_type,
        secondary_target_id=secondary_target_id,
        data=payload,
        dedupe_key=f"{instance._meta.label_lower}:{instance.pk}",
    )


def _retire(instance, origin=None):
    if origin is not None:
        origin_model = getattr(origin, "model", origin.__class__)
        if origin_model is not instance.__class__:
            return
        if (
            isinstance(origin, instance.__class__)
            and origin.pk != instance.pk
        ):
            return
    retire_notification(dedupe_key=f"{instance._meta.label_lower}:{instance.pk}")


def _notify_comment_mentions(comment):
    usernames = {
        match.rstrip(".,;:!?)]}").casefold()
        for match in re.findall(r"(?<![\w@])@([\w.-]{1,150})", comment.text or "")
    }
    if not usernames:
        return

    users = list(
        User.objects.annotate(normalized_username=Lower("username"))
        .filter(normalized_username__in=usernames)
        .exclude(pk=comment.created_by_id)
    )

    excerpt = _excerpt(comment.text)
    for mentioned_user in users:
        create_notification(
            recipient=mentioned_user,
            actor=comment.created_by,
            notification_type="task_mention",
            target_type="task",
            target_id=comment.post_id,
            secondary_target_type="comment",
            secondary_target_id=comment.pk,
            data={
                "text": f"{_actor_name(comment.created_by)} te mencionó en un comentario.",
                "title": comment.post.title,
                "excerpt": excerpt,
            },
            dedupe_key=f"task-mention:{comment.pk}:{mentioned_user.pk}",
        )


def _notify_monthly_petition_top_five():
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    leaders = list(
        Like.objects.filter(
            task__pch__iexact="peticiones",
            created_at__gte=month_start,
            created_at__lt=next_month,
        )
        .values("task_id")
        .annotate(likes_count=Count("id"))
        .order_by("-likes_count", "task_id")[:5]
    )
    if not leaders:
        return

    tasks_by_id = Task.objects.in_bulk([leader["task_id"] for leader in leaders])
    admins = list(User.objects.filter(Q(is_staff=True) | Q(is_superuser=True)))
    month_key = month_start.strftime("%Y-%m")
    for leader in leaders:
        task = tasks_by_id.get(leader["task_id"])
        if not task:
            continue
        for admin in admins:
            create_notification(
                recipient=admin,
                actor=None,
                notification_type="petition_top_month",
                target_type="task",
                target_id=task.pk,
                data={
                    "text": f"Una petición entró al top 5 de aportaciones con más likes de {month_key}.",
                    "title": task.title[:80],
                    "likes_count": leader["likes_count"],
                    "month": month_key,
                },
                dedupe_key=f"petition-top-month:{month_key}:{task.pk}",
            )


@receiver(post_save, sender=TaskTag)
def notify_task_tag(sender, instance, created, **kwargs):
    """Notifica a la persona etiquetada en una publicación.
    Si la publicación es de categoría 'Grabar Podcast', la notificación
    usa el tipo especial 'podcast_invite' para destacarla como felicitación.
    Si la publicación está oculta, se avisa hasta que se publique."""
    if not created or instance.task.is_hidden or instance.task.group_status == "pending":
        return
    _notify_task_tag(instance)


def notify_task_tags_on_publish(task):
    """Una tarea oculta se publicó: avisa a quienes ya estaban etiquetados."""
    for tag in task.tagged_users.select_related("task", "user", "tagged_by"):
        _notify_task_tag(tag)


def _notify_task_tag(instance):
    task = instance.task
    is_podcast = "grabar podcast" in (task.categories or "").lower()
    if is_podcast:
        _notify(
            instance,
            recipient=instance.user,
            actor=instance.tagged_by,
            notification_type="podcast_invite",
            target_type="task",
            target_id=instance.task_id,
            text=f"🎉 ¡{_actor_name(instance.tagged_by)} te eligió para grabar un podcast!",
            data={"task_title": _excerpt(task.title, 80)},
        )
        return
    _notify(
        instance,
        recipient=instance.user,
        actor=instance.tagged_by,
        notification_type="task_tag",
        target_type="task",
        target_id=instance.task_id,
        text=f"{_actor_name(instance.tagged_by)} te etiquetó en una publicación",
        data={"task_title": _excerpt(task.title, 80)},
    )


@receiver(post_delete, sender=CommentTag)
def retire_comment_tag_notification(sender, instance, **kwargs):
    from .testimonials import retire_tag_notification

    retire_tag_notification(instance)


@receiver(post_delete, sender=TaskTag)
def retire_task_tag_notification(sender, instance, **kwargs):
    _retire(instance)


@receiver(post_save, sender=Like)
def notify_task_like(sender, instance, created, **kwargs):
    if not created:
        return
    if instance.task.user:
        is_story = instance.task.pch == "historias"
        noun = "historia" if is_story else "tarea"
        _notify(
            instance,
            recipient=instance.task.user,
            actor=instance.user,
            notification_type="story_like" if is_story else "task_like",
            target_type="story" if is_story else "task",
            target_id=instance.task_id,
            text=f"{_actor_name(instance.user)} indicó que le gusta tu {noun}.",
            data={
                "title": instance.task.title,
                "excerpt": _excerpt(instance.task.description),
            },
        )
    if (instance.task.pch or "").casefold() == "peticiones":
        _notify_monthly_petition_top_five()


@receiver(post_delete, sender=Like)
def retire_task_like(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))
    if (instance.task.pch or "").casefold() == "peticiones":
        _notify_monthly_petition_top_five()


@receiver(post_save, sender=Like)
def retire_app_like_request_for_liker(sender, instance, created, **kwargs):
    if created:
        app_like_requests.retire_for_liker(instance)


@receiver(post_delete, sender=Like)
def retire_app_like_requests_without_app_likes(sender, instance, **kwargs):
    app_like_requests.retire_if_no_app_likes(instance.task_id)


@receiver(post_save, sender=NewPeticionCommentPost)
def notify_task_comment(sender, instance, created, **kwargs):
    if not created:
        return
    _notify_comment_mentions(instance)
    recipient = instance.parent.created_by if instance.parent else instance.post.user
    if not recipient:
        return
    is_reply = bool(instance.parent_id)
    _notify(
        instance,
        recipient=recipient,
        actor=instance.created_by,
        notification_type="task_reply" if is_reply else "task_comment",
        target_type="task",
        target_id=instance.post_id,
        secondary_target_type="comment",
        secondary_target_id=instance.pk,
        text=(
            f"{_actor_name(instance.created_by)} respondió tu comentario."
            if is_reply
            else f"{_actor_name(instance.created_by)} comentó tu tarea."
        ),
        data={
            "title": instance.post.title,
            "excerpt": _excerpt(instance.text),
        },
    )


@receiver(post_delete, sender=NewPeticionCommentPost)
def retire_task_comment(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=LikeCommentPost)
def notify_task_comment_like(sender, instance, created, **kwargs):
    if not created:
        return
    comment = instance.comment
    _notify(
        instance,
        recipient=comment.created_by,
        actor=instance.user,
        notification_type="task_comment_like",
        target_type="task",
        target_id=comment.post_id,
        secondary_target_type="comment",
        secondary_target_id=comment.pk,
        text=f"{_actor_name(instance.user)} indicó que le gusta tu comentario.",
        data={
            "title": comment.post.title,
            "excerpt": _excerpt(comment.text),
        },
    )


@receiver(post_delete, sender=LikeCommentPost)
def retire_task_comment_like(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=SharedTask)
def notify_task_share(sender, instance, created, **kwargs):
    if not created or not instance.task.user:
        return
    _notify(
        instance,
        recipient=instance.task.user,
        actor=instance.shared_by,
        notification_type="task_shared",
        target_type="shared_task",
        target_id=instance.pk,
        secondary_target_type="task",
        secondary_target_id=instance.task_id,
        text=f"{_actor_name(instance.shared_by)} compartió tu tarea.",
        data={
            "title": instance.task.title,
            "excerpt": _excerpt(instance.description or instance.task.description),
        },
    )


@receiver(post_delete, sender=SharedTask)
def retire_task_share(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=SharedTaskComment)
def notify_shared_task_comment(sender, instance, created, **kwargs):
    if not created:
        return
    recipient = (
        instance.parent.created_by
        if instance.parent
        else instance.shared_task.shared_by
    )
    is_reply = bool(instance.parent_id)
    _notify(
        instance,
        recipient=recipient,
        actor=instance.created_by,
        notification_type=(
            "shared_task_reply" if is_reply else "shared_task_comment"
        ),
        target_type="shared_task",
        target_id=instance.shared_task_id,
        secondary_target_type="comment",
        secondary_target_id=instance.pk,
        text=(
            f"{_actor_name(instance.created_by)} respondió tu comentario."
            if is_reply
            else f"{_actor_name(instance.created_by)} comentó tu tarea compartida."
        ),
        data={
            "title": instance.shared_task.task.title,
            "excerpt": _excerpt(instance.text),
        },
    )


@receiver(post_delete, sender=SharedTaskComment)
def retire_shared_task_comment(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=LikeSharedTask)
def notify_shared_task_like(sender, instance, created, **kwargs):
    if not created:
        return
    shared = instance.shared_task
    _notify(
        instance,
        recipient=shared.shared_by,
        actor=instance.user,
        notification_type="shared_task_like",
        target_type="shared_task",
        target_id=shared.pk,
        secondary_target_type="task",
        secondary_target_id=shared.task_id,
        text=f"{_actor_name(instance.user)} indicó que le gusta tu tarea compartida.",
        data={"title": shared.task.title, "excerpt": _excerpt(shared.description)},
    )


@receiver(post_delete, sender=LikeSharedTask)
def retire_shared_task_like(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=LikeSharedTaskComment)
def notify_shared_comment_like(sender, instance, created, **kwargs):
    if not created:
        return
    comment = instance.comment
    _notify(
        instance,
        recipient=comment.created_by,
        actor=instance.user,
        notification_type="shared_task_comment_like",
        target_type="shared_task",
        target_id=comment.shared_task_id,
        secondary_target_type="comment",
        secondary_target_id=comment.pk,
        text=f"{_actor_name(instance.user)} indicó que le gusta tu comentario.",
        data={
            "title": comment.shared_task.task.title,
            "excerpt": _excerpt(comment.text),
        },
    )


@receiver(post_delete, sender=LikeSharedTaskComment)
def retire_shared_comment_like(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=Task)
def notify_story_share(sender, instance, created, **kwargs):
    if (
        not created
        or not instance.story_is_shared
        or not instance.story_source_task_id
        or not instance.story_source_task.user
    ):
        return
    source = instance.story_source_task
    _notify(
        instance,
        recipient=source.user,
        actor=instance.user,
        notification_type="task_shared_to_story",
        target_type="story",
        target_id=instance.pk,
        secondary_target_type="task",
        secondary_target_id=source.pk,
        text=f"{_actor_name(instance.user)} compartió tu tarea en una historia.",
        data={"title": source.title, "excerpt": _excerpt(instance.description)},
    )


@receiver(post_delete, sender=Task)
def retire_story_share(sender, instance, **kwargs):
    if instance.story_is_shared:
        _retire(instance, kwargs.get("origin"))


@receiver(post_delete, sender=Task)
def retire_written_testimonial(sender, instance, **kwargs):
    """Un testimonio borrado ya no espera respuesta de la persona del perfil."""
    if instance.testimonial_for_id:
        from .testimonials import retire_written_testimonial as retire
        retire(instance)


@receiver(post_save, sender=LikeP)
def notify_profile_like(sender, instance, created, **kwargs):
    if created:
        _notify(
            instance,
            recipient=instance.profile,
            actor=instance.user,
            notification_type="profile_like",
            target_type="profile",
            target_id=instance.profile_id,
            text=f"{_actor_name(instance.user)} indicó que le gusta tu perfil.",
        )


@receiver(post_delete, sender=LikeP)
def retire_profile_like(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=Favorito)
def notify_task_favorite(sender, instance, created, **kwargs):
    if created and instance.task.user:
        _notify(
            instance,
            recipient=instance.task.user,
            actor=instance.user,
            notification_type="task_favorite",
            target_type="task",
            target_id=instance.task_id,
            text=f"{_actor_name(instance.user)} agregó tu tarea a sus favoritos.",
            data={"title": instance.task.title},
        )


@receiver(post_delete, sender=Favorito)
def retire_task_favorite(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=Favorito)
def save_when_favorited(sender, instance, created, **kwargs):
    """Agregar a favoritos (o anclar) guarda la tarea: favoritos ⊂ guardados."""
    if created:
        TaskSave.objects.get_or_create(user_id=instance.user_id, task_id=instance.task_id)


@receiver(post_delete, sender=TaskSave)
def unfavorite_when_unsaved(sender, instance, **kwargs):
    """Quitar de guardados quita también el favorito (y con él el top 5 y sus anclajes por categoría)."""
    Favorito.objects.filter(user_id=instance.user_id, task_id=instance.task_id).delete()


@receiver(post_save, sender=TaskSave)
def notify_task_save(sender, instance, created, **kwargs):
    if not created or not instance.task.user_id:
        return
    if Favorito.objects.filter(user_id=instance.user_id, task_id=instance.task_id).exists():
        # Se guardó al agregarla a favoritos: ya se avisó con esa notificación.
        return
    _notify(
        instance,
        recipient=instance.task.user,
        actor=instance.user,
        notification_type="task_save",
        target_type="task",
        target_id=instance.task_id,
        text=f"{_actor_name(instance.user)} guardó tu tarea.",
        data={"title": instance.task.title},
    )


@receiver(post_delete, sender=TaskSave)
def retire_task_save(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=pFavorito)
def notify_profile_favorite(sender, instance, created, **kwargs):
    if created:
        _notify(
            instance,
            recipient=instance.perfil,
            actor=instance.user,
            notification_type="profile_favorite",
            target_type="profile",
            target_id=instance.perfil_id,
            text=f"{_actor_name(instance.user)} guardó tu perfil en favoritos.",
        )


@receiver(post_delete, sender=pFavorito)
def retire_profile_favorite(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=pFavorito)
def follow_when_favorited(sender, instance, created, **kwargs):
    """Favoritar (o anclar) a alguien lo sigue automaticamente."""
    if created and instance.user_id != instance.perfil_id:
        Follow.objects.get_or_create(
            follower_id=instance.user_id, following_id=instance.perfil_id
        )


@receiver(post_delete, sender=Follow)
def unfavorite_when_unfollowed(sender, instance, **kwargs):
    """Dejar de seguir quita tambien el favorito (y con el, el anclaje)."""
    pFavorito.objects.filter(
        user_id=instance.follower_id, perfil_id=instance.following_id
    ).delete()


@receiver(post_save, sender=Follow)
def notify_follow(sender, instance, created, **kwargs):
    if not created:
        return
    if pFavorito.objects.filter(
        user_id=instance.follower_id, perfil_id=instance.following_id
    ).exists():
        # Ya se aviso con la notificacion de favorito; no se duplica.
        return
    _notify(
        instance,
        recipient=instance.following,
        actor=instance.follower,
        notification_type="user_follow",
        target_type="profile",
        target_id=instance.following_id,
        text=f"{_actor_name(instance.follower)} empezó a seguirte.",
    )


@receiver(post_delete, sender=Follow)
def retire_follow(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=Postt)
def notify_forum_reply(sender, instance, created, **kwargs):
    if not created or not instance.parent_id:
        return
    parent = instance.parent
    _notify(
        instance,
        recipient=parent.user,
        actor=instance.user,
        notification_type="forum_reply",
        target_type="forum_post",
        target_id=parent.pk,
        secondary_target_type="forum_reply",
        secondary_target_id=instance.pk,
        text=f"{_actor_name(instance.user)} respondió tu publicación del foro.",
        data={
            "title": parent.title,
            "excerpt": _excerpt(instance.content),
        },
    )


@receiver(post_delete, sender=Postt)
def retire_forum_reply(sender, instance, **kwargs):
    if instance.parent_id:
        _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=LikePostt)
def notify_forum_like(sender, instance, created, **kwargs):
    if created:
        post = instance.post
        _notify(
            instance,
            recipient=post.user,
            actor=instance.user,
            notification_type="forum_post_like",
            target_type="forum_post",
            target_id=post.pk,
            text=f"{_actor_name(instance.user)} indicó que le gusta tu publicación.",
            data={"title": post.title, "excerpt": _excerpt(post.content)},
        )


@receiver(post_delete, sender=LikePostt)
def retire_forum_like(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=Message)
def notify_direct_message(sender, instance, created, **kwargs):
    if not created:
        return
    recipient = (
        instance.replied_to.sender
        if instance.replied_to and instance.replied_to.sender_id != instance.sender_id
        else instance.receiver
    )
    _notify(
        instance,
        recipient=recipient,
        actor=instance.sender,
        notification_type=(
            "direct_message_reply" if instance.replied_to_id else "direct_message"
        ),
        target_type="direct_chat",
        target_id=instance.sender_id,
        secondary_target_type="message",
        secondary_target_id=instance.pk,
        text=(
            f"{_actor_name(instance.sender)} respondió tu mensaje."
            if instance.replied_to_id
            else f"{_actor_name(instance.sender)} te envió un mensaje."
        ),
        data={"excerpt": _excerpt(instance.content) or "Archivo adjunto"},
    )


@receiver(post_delete, sender=Message)
def retire_direct_message(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=MessageLike)
def notify_message_like(sender, instance, created, **kwargs):
    if created:
        message = instance.message
        _notify(
            instance,
            recipient=message.sender,
            actor=instance.user,
            notification_type="direct_message_like",
            target_type="direct_chat",
            target_id=instance.user_id,
            secondary_target_type="message",
            secondary_target_id=message.pk,
            text=f"{_actor_name(instance.user)} indicó que le gusta tu mensaje.",
            data={"excerpt": _excerpt(message.content)},
        )


@receiver(post_delete, sender=MessageLike)
def retire_message_like(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))


@receiver(post_save, sender=GroupMessage)
def notify_group_message(sender, instance, created, **kwargs):
    if not created:
        return
    replied_user_id = (
        instance.replied_to.sender_id
        if instance.replied_to_id
        and instance.replied_to.sender_id != instance.sender_id
        else None
    )
    memberships = GroupMembership.objects.filter(group=instance.group).select_related(
        "user"
    )
    for membership in memberships:
        if membership.user_id == instance.sender_id:
            continue
        is_reply_recipient = membership.user_id == replied_user_id
        _notify(
            instance,
            recipient=membership.user,
            actor=instance.sender,
            notification_type=(
                "group_message_reply" if is_reply_recipient else "group_message"
            ),
            target_type="group_chat",
            target_id=instance.group_id,
            secondary_target_type="group_message",
            secondary_target_id=instance.pk,
            text=(
                f"{_actor_name(instance.sender)} respondió tu mensaje en "
                f"{instance.group.name}."
                if is_reply_recipient
                else f"{_actor_name(instance.sender)} envió un mensaje en "
                f"{instance.group.name}."
            ),
            data={
                "title": instance.group.name,
                "excerpt": _excerpt(instance.content) or "Archivo adjunto",
            },
        )


@receiver(post_delete, sender=GroupMessage)
def retire_group_message(sender, instance, **kwargs):
    _retire(instance, kwargs.get("origin"))
# ── Push Notifications ────────────────────────────────────────────────────────

@receiver(post_save, sender=Notification)
def send_push_on_notification(sender, instance, created, **kwargs):
    """
    Encola el envío de push en Celery cuando se crea una Notification.

    - La respuesta HTTP al usuario no espera a Expo: Celery procesa el envío.
    - El encolado se hace en `transaction.on_commit`: si la transacción se
      revierte no se envía un push de una notificación que no existe.
    - Falla silenciosamente: el push nunca debe romper el request principal
      (p. ej. si el broker Redis está caído).
    """
    if not created:
        return

    tokens = list(
        PushToken.objects.filter(user=instance.recipient, is_active=True)
        .values_list("token", flat=True)
    )
    if not tokens:
        return

    title = instance.data.get("title") or "Aportaciones"
    body = (
        instance.data.get("excerpt")
        or instance.data.get("text")
        or instance.notification_type
    )
    payload = {
        "notification_id": str(instance.id),
        "type": instance.notification_type,
        "target_type": instance.target_type,
        "target_id": instance.target_id,
    }

    def _enqueue():
        from .tasks import send_push_notification_task

        try:
            send_push_notification_task.delay(
                tokens=tokens,
                title=title,
                body=body,
                data=payload,
            )
        except Exception:
            logger.exception("[push] No se pudo encolar la notificación %s", instance.id)

    transaction.on_commit(_enqueue)