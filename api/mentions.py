"""Menciones (@usuario) y hashtags (#tema) en lo que se escribe.

Quien sale mencionado recibe un aviso que dice dónde: en una publicación (o historia), en un
comentario o una respuesta, en un comentario de una publicación compartida o en el foro. Una
sola vez por cada texto, y solo si puede ver dónde se le mencionó: nada de lo que está oculto,
en revisión de un grupo o en un grupo privado del que no es parte, ni un post del foro al que
no tiene acceso (avisa cuando se publique). Una historia que comparte otra publicación no
avisa: copia el texto de la original.

Un hashtag se busca como palabra completa: #viaje no encuentra #viajes.
"""
import re
from collections import Counter

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.db.models.functions import Lower
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import models as ms
from .forum_access import can_view_post, forum_root
from .notifications import create_notification
from .serializers import SimpleUserSerializer

# Sin contar las de una URL (medium.com/@ana) ni un correo (ana@correo.com).
MENTION_RE = re.compile(r"(?<![\w@/])@([\w.-]{1,150})")
# Lleva al menos una letra (#1 o "puesto #3" no son hashtags) y no cuenta el de una URL
# (x.com/#seccion) ni el de una entidad HTML (&#39;).
HASHTAG_RE = re.compile(r"(?<![\w&#/])#(?=\w*[^\W\d_])(\w{1,50})(?!\w)")
HASHTAG_SEARCH_RE = re.compile(r"^#(\w{1,50})$")

DESCRIPTION_MENTION = "task_description_mention"
COMMENT_MENTION = "task_mention"
SHARED_COMMENT_MENTION = "shared_comment_mention"
FORUM_MENTION = "forum_mention"
HASHTAG_SUGGESTIONS = 8
# Publicaciones recientes de donde salen las sugerencias de hashtags.
HASHTAG_SAMPLE = 500


def mentioned_usernames(text):
    return {match.rstrip(".").casefold() for match in MENTION_RE.findall(text or "")} - {""}


def display_name(user):
    """Cómo se nombra a alguien en un aviso: su usuario o su nombre, nunca su correo."""
    full_name = " ".join(part for part in (user.first_name, user.last_name) if part)
    return user.username or full_name or "Alguien"


def _excerpt(value, limit=140):
    value = " ".join((value or "").split())
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def can_view_task(task, user):
    """¿`user` puede abrir esta publicación? Las mismas reglas que el feed."""
    from .views import exclude_hidden_tasks

    return exclude_hidden_tasks(ms.Task.objects.filter(pk=task.pk), viewer=user).exists()


def notify_mentions(text, *, author, notification_type, where, target_type, target_id, title,
                    dedupe_prefix, can_view, excerpt=None, secondary_target_type="", secondary_target_id=""):
    """Avisa a quienes `text` menciona y pueden ver dónde (`can_view(user)`).
    `where` completa el aviso: "te mencionó en un comentario", "...en el foro"."""
    usernames = mentioned_usernames(text)
    if not usernames or author is None:
        return
    users = (
        get_user_model().objects.annotate(normalized_username=Lower("username"))
        .filter(normalized_username__in=usernames, is_active=True)
        .exclude(pk=author.pk)
    )
    for user in users:
        if not can_view(user):
            continue
        create_notification(
            recipient=user,
            actor=author,
            notification_type=notification_type,
            target_type=target_type,
            target_id=target_id,
            secondary_target_type=secondary_target_type,
            secondary_target_id=secondary_target_id,
            data={
                "text": f"{display_name(author)} te mencionó {where}.",
                "title": title or "",
                "excerpt": _excerpt(text if excerpt is None else excerpt),
            },
            dedupe_key=f"{dedupe_prefix}:{user.pk}",
        )


def notify_description_mentions(task, texts):
    """La descripción de una publicación o de sus bloques (`texts`)."""
    if task.story_is_shared:
        return
    notify_mentions(
        " ".join(text or "" for text in texts),
        author=task.user,
        notification_type=DESCRIPTION_MENTION,
        where="en una historia" if task.is_story else "en una publicación",
        target_type="task",
        target_id=task.pk,
        title=task.title,
        excerpt=task.description,
        dedupe_prefix=f"task-description-mention:{task.pk}",
        can_view=lambda user: can_view_task(task, user),
    )


def notify_comment_mentions(comment):
    """Un comentario (o respuesta) de una publicación."""
    task = comment.post
    notify_mentions(
        comment.text,
        author=comment.created_by,
        notification_type=COMMENT_MENTION,
        where="en una respuesta a un comentario" if comment.parent_id else "en un comentario",
        target_type="task",
        target_id=comment.post_id,
        secondary_target_type="comment",
        secondary_target_id=comment.pk,
        title=task.title,
        dedupe_prefix=f"task-mention:{comment.pk}",
        can_view=lambda user: can_view_task(task, user),
    )


def notify_shared_comment_mentions(comment):
    """Un comentario (o respuesta) de una publicación compartida."""
    task = comment.shared_task.task
    notify_mentions(
        comment.text,
        author=comment.created_by,
        notification_type=SHARED_COMMENT_MENTION,
        where=(
            "en una respuesta a un comentario de una publicación compartida" if comment.parent_id
            else "en un comentario de una publicación compartida"
        ),
        target_type="shared_task",
        target_id=comment.shared_task_id,
        secondary_target_type="shared_comment",
        secondary_target_id=comment.pk,
        title=task.title,
        dedupe_prefix=f"shared-comment-mention:{comment.pk}",
        can_view=lambda user: can_view_task(task, user),
    )


def notify_forum_mentions(post):
    """Un post del foro (título y contenido) o una respuesta. Se llama después de guardar
    a los invitados: en un post solo para invitados, solo a ellos les llega."""
    root = forum_root(post)
    is_reply = bool(post.parent_id)
    notify_mentions(
        post.content if is_reply else f"{post.title} {post.content}",
        author=post.user,
        notification_type=FORUM_MENTION,
        where="en una respuesta del foro" if is_reply else "en una publicación del foro",
        target_type="forum_post",
        target_id=root.pk,
        secondary_target_type="forum_reply" if is_reply else "",
        secondary_target_id=post.pk if is_reply else "",
        title=root.title,
        excerpt=post.content,
        dedupe_prefix=f"forum-mention:{post.pk}",
        can_view=lambda user: can_view_post(root, user),
    )


def hashtag_in_search(raw_search):
    """El hashtag que se busca ("#viaje" -> "viaje"), o None si la búsqueda no es uno."""
    match = HASHTAG_SEARCH_RE.match(raw_search or "")
    return match.group(1) if match else None


def hashtag_q(tag, *fields):
    """Lo que usa #tag como palabra completa en alguno de `fields` (como HASHTAG_RE: no
    cuenta el de una URL ni el de una entidad HTML)."""
    pattern = rf"(^|[^[:alnum:]_/&#])#{re.escape(tag)}([^[:alnum:]_]|$)"
    query = Q()
    for field in fields:
        query |= Q(**{f"{field}__iregex": pattern})
    return query


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_by_username(request, username):
    """Para abrir el perfil de una mención (@usuario)."""
    user = get_user_model().objects.filter(username__iexact=username, is_active=True).first()
    if user is None:
        return Response({"detail": "No encontramos a esa persona."}, status=404)
    return Response(SimpleUserSerializer(user, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def hashtag_suggestions(request):
    """Los hashtags más usados en las publicaciones recientes que empiezan con `q`."""
    from .views import exclude_hidden_tasks

    prefix = (request.query_params.get("q") or "").strip().lstrip("#").casefold()
    tasks = exclude_hidden_tasks(
        ms.Task.objects.filter(description__icontains=f"#{prefix}"), viewer=request.user,
    )
    descriptions = tasks.order_by("-created_at").values_list("description", flat=True)[:HASHTAG_SAMPLE]
    counts = Counter()
    spelling = {}
    for text in descriptions:
        # Cada publicación cuenta una vez por hashtag.
        for tag in {tag.casefold(): tag for tag in HASHTAG_RE.findall(text or "")}.values():
            key = tag.casefold()
            if key.startswith(prefix):
                counts[key] += 1
                spelling.setdefault(key, tag)
    return Response([
        {"tag": spelling[key], "count": count}
        for key, count in counts.most_common(HASHTAG_SUGGESTIONS)
    ])
