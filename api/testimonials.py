"""Etiquetas en comentarios y testimonios en el perfil.

Un testimonio llega de dos formas:
- Quien comenta etiqueta a otras personas (CommentTag).
- Alguien lo escribe directamente en el perfil de otra persona: una publicación
  (tarea con pch "testimonios" y `testimonial_for`) con likes, comentarios, impulsar...
En los dos casos la persona del perfil recibe una notificación y decide si se
muestra. Los testimonios aprobados los ve todo el mundo.
"""
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import CommentTag, Task
from .notifications import create_notification, retire_notification

User = get_user_model()

MAX_TAGS_PER_COMMENT = 10
MAX_TESTIMONIALS = 100
TAG_NOTIFICATION = "comment_tag"
PROFILE_NOTIFICATION = "profile_testimonial"
APPROVED_NOTIFICATION = "testimonial_approved"


def _dedupe_key(tag):
    return f"{TAG_NOTIFICATION}:{tag.pk}"


def _notify_tag(tag):
    from .mentions import display_name

    comment = tag.comment
    task = comment.post
    if task.is_hidden:
        # Nadie más que el dueño puede abrir una tarea oculta.
        return
    author = comment.created_by
    create_notification(
        recipient=tag.user,
        actor=author,
        notification_type=TAG_NOTIFICATION,
        target_type="task",
        target_id=comment.post_id,
        secondary_target_type="comment",
        secondary_target_id=comment.pk,
        data={
            "text": (
                f"{display_name(author)} te etiquetó en "
                f"{'una respuesta a un comentario' if comment.parent_id else 'un comentario'}. "
                "¿Lo muestras en tu perfil como testimonio?"
            ),
            "testimonial_id": str(tag.pk),
            "title": task.title,
            "excerpt": (comment.text or "")[:140],
        },
        dedupe_key=_dedupe_key(tag),
    )


def retire_tag_notification(tag):
    retire_notification(dedupe_key=_dedupe_key(tag))


def sync_comment_tags(comment, user_ids):
    """Deja etiquetadas exactamente a `user_ids` (sin el autor), avisando a las nuevas."""
    wanted = {
        pk for pk in User.objects.filter(pk__in=list(user_ids), is_active=True)
        .exclude(pk=comment.created_by_id).values_list("pk", flat=True)
    }
    existing = {tag.user_id: tag for tag in comment.tags.all()}

    for user_id, tag in existing.items():
        if user_id not in wanted:
            tag.delete()
    for user_id in wanted - set(existing):
        tag = CommentTag.objects.create(comment=comment, user_id=user_id)
        _notify_tag(tag)


def _profile_dedupe_key(task):
    return f"{PROFILE_NOTIFICATION}:{task.pk}"


def submit_written_testimonial(task):
    """Un testimonio nuevo (o editado por quien lo escribió) queda pendiente y se le
    avisa a la persona del perfil para que decida si lo muestra."""
    if task.testimonial_status != CommentTag.STATUS_PENDING:
        task.testimonial_status = CommentTag.STATUS_PENDING
        task.save(update_fields=["testimonial_status"])
    # Al editarlo se avisa de nuevo (la notificación anterior ya pudo leerse).
    retire_notification(dedupe_key=_profile_dedupe_key(task))
    create_notification(
        recipient=task.testimonial_for,
        actor=task.user,
        notification_type=PROFILE_NOTIFICATION,
        target_type="testimonial",
        target_id=task.testimonial_for_id,
        data={
            "text": f"{task.user.username or 'Alguien'} escribió un testimonio en tu perfil. ¿Lo muestras?",
            "testimonial_id": str(task.pk),
            "title": task.title,
            "excerpt": (task.description or "")[:140],
        },
        dedupe_key=_profile_dedupe_key(task),
    )


def retire_written_testimonial(task):
    retire_notification(dedupe_key=_profile_dedupe_key(task))
    retire_notification(dedupe_key=f"{APPROVED_NOTIFICATION}:{task.pk}")


def _testimonial_data(tag, request):
    from .serializers import SimpleUserSerializer

    comment = tag.comment
    task = comment.post
    return {
        "id": str(tag.pk),
        "kind": "comment",
        "status": tag.status,
        "created_at": comment.created_at,
        "comment": {"id": comment.pk, "text": comment.text},
        "author": SimpleUserSerializer(comment.created_by, context={"request": request}).data,
        "task": {"id": str(task.pk), "title": task.title},
    }


def _task_testimonial_data(task, request):
    """La publicación completa (como en el feed) con su estado de testimonio."""
    from .serializers import TaskSerializer

    data = TaskSerializer(task, context={"request": request}).data
    data["kind"] = "task"
    data["status"] = task.testimonial_status
    return data


def _merged(tags, tasks, request):
    """Las dos clases de testimonio en una lista, de la más nueva a la más vieja."""
    rows = [(tag.comment.created_at, _testimonial_data(tag, request)) for tag in tags]
    rows += [(task.created_at, _task_testimonial_data(task, request)) for task in tasks]
    rows.sort(key=lambda row: row[0], reverse=True)
    return [data for _, data in rows[:MAX_TESTIMONIALS]]


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_testimonials(request, user_id):
    """Testimonios de un perfil: los aprobados; a su dueño también los pendientes y a
    quien escribió alguno sin aprobar, los suyos (`mine`). Para escribir uno se crea una
    publicación con `testimonial_for` (tasks/)."""
    from .views import TASK_SERIALIZER_PREFETCH, exclude_hidden_tasks

    owner = get_object_or_404(User, pk=user_id)
    tags = exclude_hidden_tasks(
        CommentTag.objects.filter(user=owner), request.user, prefix="comment__post__",
    ).select_related(
        "comment__created_by", "comment__post",
    ).prefetch_related("comment__created_by__fixed_images").order_by("-created_at")
    written = exclude_hidden_tasks(
        Task.objects.filter(testimonial_for=owner), viewer=request.user,
    ).select_related("user").prefetch_related(*TASK_SERIALIZER_PREFETCH).order_by("-created_at")

    is_owner = owner.pk == request.user.pk
    approved = CommentTag.STATUS_APPROVED
    response = {
        "testimonials": _merged(
            tags.filter(status=approved)[:MAX_TESTIMONIALS],
            written.filter(testimonial_status=approved)[:MAX_TESTIMONIALS],
            request,
        ),
        "is_owner": is_owner,
        "can_write": not is_owner,
    }
    if is_owner:
        pending = CommentTag.STATUS_PENDING
        response["pending"] = _merged(
            tags.filter(status=pending)[:MAX_TESTIMONIALS],
            written.filter(testimonial_status=pending)[:MAX_TESTIMONIALS],
            request,
        )
    else:
        mine = written.filter(user=request.user).exclude(testimonial_status=approved)[:MAX_TESTIMONIALS]
        response["mine"] = [_task_testimonial_data(task, request) for task in mine]
    return Response(response)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def testimonial_detail(request, pk):
    """La persona del perfil aprueba (lo muestra) o rechaza (lo oculta) el testimonio."""
    new_status = request.data.get("status")
    if new_status not in {CommentTag.STATUS_APPROVED, CommentTag.STATUS_REJECTED}:
        return Response(
            {"status": ["Debe ser approved o rejected."]}, status=status.HTTP_400_BAD_REQUEST,
        )
    tag = CommentTag.objects.select_related("comment__created_by", "comment__post").filter(
        pk=pk, user=request.user,
    ).first()
    if tag is not None:
        tag.status = new_status
        tag.save(update_fields=["status", "updated_at"])
        return Response(_testimonial_data(tag, request))
    task = get_object_or_404(Task.objects.select_related("user"), pk=pk, testimonial_for=request.user)
    task.testimonial_status = new_status
    task.save(update_fields=["testimonial_status"])
    if new_status == CommentTag.STATUS_APPROVED:
        # A quien lo escribió: ya se ve en el perfil.
        create_notification(
            recipient=task.user,
            actor=request.user,
            notification_type=APPROVED_NOTIFICATION,
            target_type="task",
            target_id=task.pk,
            data={"text": f"{request.user.username or 'Alguien'} mostró tu testimonio en su perfil."},
            dedupe_key=f"{APPROVED_NOTIFICATION}:{task.pk}",
        )
    return Response(_task_testimonial_data(task, request))
