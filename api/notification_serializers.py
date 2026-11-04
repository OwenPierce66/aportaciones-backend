from rest_framework import serializers

from .models import ImagenFija, Notification
from .serializers import file_to_abs_url, latest_user_image_url


class NotificationSerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()
    type = serializers.CharField(source="notification_type", read_only=True)
    text = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "actor",
            "type",
            "notification_type",
            "text",
            "data",
            "target_type",
            "target_id",
            "secondary_target_type",
            "secondary_target_id",
            "is_read",
            "read_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_actor(self, obj):
        if not obj.actor:
            return None
        full_name = " ".join(
            part for part in (obj.actor.first_name, obj.actor.last_name) if part
        )
        return {
            "id": str(obj.actor_id),
            "username": obj.actor.username,
            # Nunca el correo: el aviso lo ve otra persona.
            "name": full_name or obj.actor.username or "",
            "image": latest_user_image_url(obj.actor, self.context.get("request")),
        }

    def get_text(self, obj):
        return obj.data.get("text", "")

    def to_representation(self, obj):
        data = super().to_representation(obj)
        if obj.notification_type == "story_expired":
            from .models import Task

            story = Task.objects.filter(pk=obj.target_id).first()
            request = self.context.get("request")
            data["data"] = {
                **(data.get("data") or {}),
                "story_exists": bool(story),
                "story_saved": bool(story and story.story_saved),
                "story_image": file_to_abs_url(story.image, request) if story and story.image else None,
                "story_video": file_to_abs_url(story.video, request) if story and story.video else None,
            }
        return data
