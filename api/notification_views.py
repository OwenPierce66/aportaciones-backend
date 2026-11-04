from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import app_like_requests
from .models import Notification
from .notification_serializers import NotificationSerializer
from .notifications import ensure_story_expiry_notifications


class NotificationPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class NotificationListView(generics.ListAPIView):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = NotificationPagination

    def get_queryset(self):
        ensure_story_expiry_notifications(self.request.user)
        notifications = Notification.objects.filter(
            recipient=self.request.user
        ).select_related("actor").prefetch_related("actor__fixed_images")
        if self.request.query_params.get("unread", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            notifications = notifications.filter(is_read=False)
        return notifications


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def unread_count(request):
    ensure_story_expiry_notifications(request.user)
    count = Notification.objects.filter(
        recipient=request.user,
        is_read=False,
    ).count()
    return Response({"unread_count": count})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mark_read(request, notification_id):
    notification = get_object_or_404(
        Notification.objects.select_related("actor"),
        id=notification_id,
        recipient=request.user,
    )
    if not notification.is_read or notification.read_at is None:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at", "updated_at"])
    return Response(
        NotificationSerializer(notification, context={"request": request}).data
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mark_all_read(request):
    now = timezone.now()
    updated = Notification.objects.filter(
        recipient=request.user,
        is_read=False,
    ).update(is_read=True, read_at=now, updated_at=now)
    return Response({"updated": updated})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def set_app_like_request_seen(request, notification_id):
    """Marca o desmarca como visto el aviso de dar like como la App. Body: {seen}."""
    notification = get_object_or_404(
        Notification.objects.select_related("actor"),
        id=notification_id,
        recipient=request.user,
        notification_type=app_like_requests.APP_LIKE_REQUEST,
    )
    seen = request.data.get("seen", True)
    if not isinstance(seen, bool):
        return Response({"seen": "Debe ser un valor booleano."}, status=status.HTTP_400_BAD_REQUEST)
    app_like_requests.set_seen(notification, seen)
    return Response(
        NotificationSerializer(notification, context={"request": request}).data
    )


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_notification(request, notification_id):
    notification = get_object_or_404(
        Notification,
        id=notification_id,
        recipient=request.user,
    )
    notification.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)
