from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db.models import Case, Count, IntegerField, OuterRef, Q, Subquery, Value, When
from django.conf import settings
from . import models as ms
from .app_account import app_account_id_for
from .upload_validation import validate_image_upload, validate_video_upload

User = get_user_model()

# Slug de la etiqueta de aprobación (espejo de ms.APPROVED_TAG).
APPROVED_TAG = "aprobada"
APPROVED_TAG_ALIASES = {"aprobada", "aprobadas", "aprovada", "aprovadas"}


def file_to_abs_url(file_or_str, request=None):
    """Convierte FieldFile o string a URL absoluta"""
    if not file_or_str:
        return None

    if hasattr(file_or_str, "url"):
        url = file_or_str.url
    else:
        url = str(file_or_str).strip()
        if not url:
            return None

        if url.startswith(("http://", "https://")):
            return url

        media_url = (getattr(settings, "MEDIA_URL", "/media/") or "/media/").rstrip("/")

        if not url.startswith("/") and not url.startswith(media_url + "/"):
            url = f"{media_url}/{url.lstrip('/')}"
        elif not url.startswith("/"):
            url = "/" + url

    if request and url and not url.startswith(("http://", "https://")):
        if not url.startswith("/"):
            url = "/" + url
        return request.build_absolute_uri(url)

    return url


# ---------------------------------------------------------------------------
# Helpers anti N+1 (Fase 36)
# ---------------------------------------------------------------------------

def is_prefetched(obj, relation):
    """True si `relation` ya fue cargada con prefetch_related en `obj`."""
    return relation in getattr(obj, "_prefetched_objects_cache", {})


def related_count(obj, relation):
    """Cuenta una relaci??n reutilizando el prefetch si existe (evita un COUNT por fila)."""
    if is_prefetched(obj, relation):
        return len(getattr(obj, relation).all())
    return getattr(obj, relation).count()


def related_has_user(obj, relation, user):
    """??`user` aparece en `relation` (likes, etc.)? Usa el prefetch si existe."""
    if is_prefetched(obj, relation):
        return any(item.user_id == user.id for item in getattr(obj, relation).all())
    return getattr(obj, relation).filter(user=user).exists()


def my_ids(request, key, model, field, owner_field="user"):
    """
    Conjunto de ids (`field`) de los registros de `model` creados por el usuario
    autenticado. Se consulta UNA vez por petici??n y se reutiliza para todos los
    items de la lista (en vez de un `.exists()` por item).
    """
    if not request or not request.user.is_authenticated:
        return set()
    cache = request.__dict__.setdefault("_perf_cache", {})
    if key not in cache:
        cache[key] = set(
            model.objects.filter(**{owner_field: request.user}).values_list(field, flat=True)
        )
    return cache[key]


def latest_user_image_url(user, request=None):
    """URL absoluta de la ??ltima ImagenFija del usuario (usa prefetch `fixed_images`)."""
    if is_prefetched(user, "fixed_images"):
        images = list(user.fixed_images.all())
        image = max(images, key=lambda i: i.id) if images else None
    else:
        image = ms.ImagenFija.objects.filter(user=user).order_by("-id").first()
    return file_to_abs_url(image.image if image and image.image else None, request)


class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.Profile
        fields = [
            "id",
            "is_verified",
            "is_recommended",
            "subscriptionActive",
            "subscription_amount",
            "role",
            "favorite_profiles_public",
            "favorite_tasks_public",
        ]
        read_only_fields = ["id"]


class ImagenFijaSerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.ImagenFija
        fields = ["id", "image", "user", "created_at"]
        read_only_fields = ["id", "user", "created_at"]

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


class UserSerializer(serializers.ModelSerializer):
    profile = ProfileSerializer(read_only=True)
    password = serializers.CharField(write_only=True, required=False, style={"input_type": "password"})
    user_image = serializers.ImageField(write_only=True, required=False)
    
    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "password",
            "profile",
            "user_image",
            "is_superuser",
            "is_staff",
            "date_joined",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "is_superuser",
            "is_staff",
            "date_joined",
            "updated_at",
        ]

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User.objects.create_user(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


# ============================================================================
# SERIALIZERS PARA CATEGORÍAS
# ============================================================================

class NewCategorySerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ms.NewCategory
        fields = ["id", "name", "pch", "parent", "description", "image", "image_url", "created_at"]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {"image": {"write_only": True, "required": False}, "description": {"required": False}}

    def get_image_url(self, obj):
        return file_to_abs_url(obj.image, self.context.get("request")) if obj.image else None

    def validate_image(self, value):
        return validate_image_upload(value) if value else value

    def validate(self, attrs):
        """Solo se rechaza si el mismo nombre ya existe DENTRO del mismo PCH."""
        # Al editar solo la imagen o la descripción se conservan el nombre y el PCH.
        name = str(attrs.get("name", getattr(self.instance, "name", "")) or "").strip()
        pch = str(attrs.get("pch", getattr(self.instance, "pch", "")) or "").strip()
        request = self.context.get("request")
        is_admin = bool(
            request and request.user.is_authenticated
            and (request.user.is_staff or request.user.is_superuser)
        )
        normalized_name = " ".join(name.split()).casefold()
        approval_aliases = {"aprobada", "aprobadas", "aprovada", "aprovadas"}
        if normalized_name in approval_aliases:
            raise serializers.ValidationError({
                "name": "Aprobada es un estado del sistema y no puede crearse como categoría."
            })
        if name:
            qs = ms.NewCategory.objects.filter(name__iexact=name, pch__iexact=pch)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({
                    "name": f"La categoría '{name}' ya existe en este tema."
                })
        attrs["name"] = name
        attrs["pch"] = pch
        return attrs


class CategoryPSerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.CategoryP
        fields = ["id", "name", "user", "parent", "position", "created_at"]
        read_only_fields = ["id", "user", "created_at"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("El filtro necesita un nombre.")
        if value.casefold() == APPROVED_TAG.casefold():
            raise serializers.ValidationError(
                "La etiqueta 'aprobada' es exclusiva de administradores, no puede usarse en tu filtro personal."
            )
        return value

    def validate_parent(self, value):
        if value is None:
            return value
        request = self.context.get("request")
        if request and value.user_id != request.user.id:
            raise serializers.ValidationError("Esa categoría padre no es tuya.")
        return value

    def create(self, validated_data):
        user = self.context["request"].user
        validated_data["user"] = user
        if "position" not in validated_data:
            last = ms.CategoryP.objects.filter(
                user=user, parent=validated_data.get("parent")
            ).order_by("-position").first()
            validated_data["position"] = (last.position + 1) if last else 0
        return super().create(validated_data)


class PersonalFilterVisibilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.Profile
        fields = [
            "personal_filter_public",
            "favorite_profiles_public",
            "favorite_tasks_public",
        ]


class UserSavedFilterSerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.UserSavedFilter
        fields = ["id", "name", "filters", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("El filtro necesita un nombre.")
        return value

    def validate_filters(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("El filtro debe ser un objeto de criterios.")
        return value

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


# ============================================================================
# SERIALIZERS PARA USUARIO SIMPLE
# ============================================================================

class SimpleUserSerializer(serializers.ModelSerializer):
    profile = ProfileSerializer(read_only=True)
    likes_count = serializers.SerializerMethodField()
    user_image = serializers.SerializerMethodField()
    has_liked = serializers.SerializerMethodField()
    is_favorited = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    # La cuenta de la App: las listas la ponen en la pestaña App.
    is_app = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "profile",
            "is_superuser",
            "is_staff",
            "likes_count",
            "user_image",
            "has_liked",
            "is_favorited",
            "is_following",
            "is_app",
        ]

    def get_is_app(self, obj):
        from .app_account import app_account_id_for

        request = self.context.get("request")
        return request is not None and str(obj.pk) == str(app_account_id_for(request))

    def get_is_following(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.pk in my_ids(request, "follow", ms.Follow, "following_id", owner_field="follower")
        return False

    def get_is_favorited(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            # `pFavorito` se relaciona con el usuario objetivo, no con el perfil.
            return obj.pk in my_ids(request, "pfav", ms.pFavorito, "perfil_id")
        return False

    def get_likes_count(self, obj):
        # ✅ FIX DEFINITIVO: El modelo LikeP.profile ahora apunta a User, no a Profile.
        # Usamos `obj` (que es un User) directamente en la consulta.
        # Esto soluciona el error fatal que tumbaba el servidor.
        return related_count(obj, "likes_received")

    def get_user_image(self, obj):
        return latest_user_image_url(obj, self.context.get("request"))

    def get_has_liked(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            # ✅ FIX DEFINITIVO: Usamos `obj` (User) directamente, igual que en get_likes_count.
            return obj.pk in my_ids(request, "likep", ms.LikeP, "profile_id")
        return False


class TaskAuthorSerializer(SimpleUserSerializer):
    """Autor de una tarea: además, cuántos lo siguen (el contador junto a su avatar en los reels).

    Va aparte de SimpleUserSerializer para no contar seguidores en comentarios, listas, etc."""
    followers_count = serializers.SerializerMethodField()

    class Meta(SimpleUserSerializer.Meta):
        fields = [*SimpleUserSerializer.Meta.fields, "followers_count"]

    def get_followers_count(self, obj):
        return related_count(obj, "follower_relationships")


class AdminUserSerializer(serializers.ModelSerializer):
    profile = ProfileSerializer(read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "is_staff",
            "is_superuser",
            "date_joined",
            "profile",
        ]


class UserReportSerializer(serializers.ModelSerializer):
    reporter = SimpleUserSerializer(read_only=True)
    target_summary = serializers.SerializerMethodField()

    class Meta:
        model = ms.UserReport
        fields = [
            "id",
            "reporter",
            "target_type",
            "target_id",
            "target_summary",
            "reason",
            "status",
            "reviewed_by",
            "resolution_note",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_target_summary(self, report):
        target_models = {
            ms.UserReport.TARGET_USER: (User, "username"),
            ms.UserReport.TARGET_TASK: (ms.Task, "title"),
            ms.UserReport.TARGET_COMMENT: (ms.NewPeticionCommentPost, "text"),
            ms.UserReport.TARGET_SHARED_COMMENT: (ms.SharedTaskComment, "text"),
            ms.UserReport.TARGET_SHARED_TASK: (ms.SharedTask, "task__title"),
            ms.UserReport.TARGET_FORUM_POST: (ms.Postt, "title"),
        }
        model_field = target_models.get(report.target_type)
        if not model_field:
            return None
        model, preview_field = model_field
        target = model.objects.filter(pk=report.target_id).first()
        if not target:
            return None
        preview = target
        for field in preview_field.split("__"):
            preview = getattr(preview, field, None)
            if preview is None:
                break
        return {
            "id": report.target_id,
            "preview": str(preview or "")[:240],
        }


# ============================================================================
# SERIALIZERS PARA LIKES Y FAVORITOS
# ============================================================================

class LikeSerializer(serializers.ModelSerializer):
    user = SimpleUserSerializer(read_only=True)

    class Meta:
        model = ms.Like
        fields = ["id", "user", "task", "created_at"]
        read_only_fields = ["id", "created_at"]


class LikeCommentPostSerializer(serializers.ModelSerializer):
    user = SimpleUserSerializer(read_only=True)

    class Meta:
        model = ms.LikeCommentPost
        fields = ["id", "user", "comment", "created_at"]
        read_only_fields = ["id", "created_at"]


class FavoritoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.Favorito
        fields = ["id", "user", "task", "created_at"]
        read_only_fields = ["id", "user", "created_at"]

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


class pFavoritoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.pFavorito
        fields = ["id", "user", "perfil", "created_at"]
        read_only_fields = ["id", "user", "created_at"]

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


# ============================================================================
# SERIALIZERS PARA TAREAS Y COMENTARIOS
# ============================================================================

class RecursiveCommentSerializer(serializers.Serializer):
    """Serializer recursivo para comentarios anidados"""
    def to_representation(self, value):
        serializer = NewPeticionCommentSerializer(value, context=self.context)
        return serializer.data


def first_task_image(task):
    """Imagen de portada: la de la tarea o, si no tiene, la del primer bloque con imagen.

    Muchas aportaciones guardan la media en sus consejos, factores o fuentes y no
    en la tarea. Recorre `.all()` para aprovechar el prefetch de las listas.
    """
    if task.image:
        return task.image
    for relation in ("subtasks", "subfactores", "subfuentes"):
        for block in getattr(task, relation).all():
            if block.image:
                return block.image
    return None


COMMENT_IMAGE_MAX_BYTES = 10 * 1024 * 1024
COMMENT_VIDEO_MAX_BYTES = 50 * 1024 * 1024
_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif"}
_VIDEO_EXTENSIONS = {"mp4", "mov", "m4v", "webm", "avi", "mkv", "3gp"}


def comment_attachment_kind(name, content_type=""):
    """'image', 'video' o None según el tipo o la extensión del archivo adjunto."""
    content_type = str(content_type or "").lower()
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    extension = str(name or "").rsplit(".", 1)[-1].lower() if "." in str(name or "") else ""
    if extension in _IMAGE_EXTENSIONS:
        return "image"
    if extension in _VIDEO_EXTENSIONS:
        return "video"
    return None


class AttachmentAportacionMixin(serializers.Serializer):
    """Lo que puede acompañar un comentario o mensaje: una foto o video (`attachment`, se sube
    con multipart y se lee en `attachment_url` / `attachment_type`) y una de tus publicaciones
    guardadas (`aportacion`, se lee en `aportacion_info`).

    `TEXT_FIELD` es el campo del texto: al crear puede ir vacío solo si lleva foto o video.
    """
    TEXT_FIELD = "text"

    attachment = serializers.FileField(write_only=True, required=False, allow_null=True)
    attachment_url = serializers.SerializerMethodField()
    attachment_type = serializers.SerializerMethodField()
    aportacion_info = serializers.SerializerMethodField()

    def get_attachment_url(self, obj):
        return file_to_abs_url(obj.attachment, self.context.get("request")) if obj.attachment else None

    def get_attachment_type(self, obj):
        if not obj.attachment:
            return None
        return "video" if comment_attachment_kind(obj.attachment.name) == "video" else "image"

    def validate_attachment(self, value):
        if value is None:
            return value
        kind = comment_attachment_kind(getattr(value, "name", ""), getattr(value, "content_type", ""))
        if kind is None:
            raise serializers.ValidationError("Solo puedes adjuntar una foto o un video.")
        limit = COMMENT_VIDEO_MAX_BYTES if kind == "video" else COMMENT_IMAGE_MAX_BYTES
        if value.size > limit:
            raise serializers.ValidationError(f"El archivo es muy grande (máximo {limit // (1024 * 1024)} MB).")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Al crear, puede ser solo una foto o video, pero no puede ir vacío.
        if self.instance is None and not str(attrs.get(self.TEXT_FIELD) or "").strip() and not attrs.get("attachment"):
            raise serializers.ValidationError({self.TEXT_FIELD: "Escribe algo o adjunta una foto o video."})
        return attrs

    def get_aportacion_info(self, obj):
        """Datos mínimos de la publicación guardada adjunta (None si no hay)."""
        task = obj.aportacion
        if task is None:
            return None
        request = self.context.get("request")
        owner = task.user
        return {
            "id": str(task.id),
            "title": task.title,
            "image": file_to_abs_url(first_task_image(task), request),
            "pch": task.pch or None,
            "username": (owner.username if owner else None) or task.username or None,
        }

    def validate_aportacion(self, value):
        """Solo se puede adjuntar una aportación que el autor tiene guardada (las favoritas
        también están guardadas).

        Igual que en la web antigua ("Seleccionar Aportación Favorita"). Si ya la tenía
        (edición) no se vuelve a exigir, para no romper mensajes cuyo autor la quitó luego.
        """
        if value is None:
            return value
        if self.instance is not None and self.instance.aportacion_id == value.pk:
            return value
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            raise serializers.ValidationError("Debes iniciar sesión.")
        if value.is_story and not value.story_saved:
            raise serializers.ValidationError("Esta aportación no está disponible.")
        if not ms.TaskSave.objects.filter(user=user, task=value).exists():
            raise serializers.ValidationError(
                "Solo puedes adjuntar una de tus aportaciones guardadas."
            )
        return value


class MessageExtrasMixin(AttachmentAportacionMixin):
    """Además de la foto o video y la publicación guardada, personas etiquetadas
    (`tagged_user_ids` al escribir, `tagged_users` al leer). Cada etiquetado nuevo
    recibe un aviso (`notify_tagged`, lo define cada serializer)."""
    MAX_TAGS = 10

    tagged_users = serializers.SerializerMethodField()
    tagged_user_ids = serializers.ListField(
        child=serializers.UUIDField(), write_only=True, required=False, max_length=MAX_TAGS,
    )

    def get_tagged_users(self, obj):
        return [{"id": str(user.id), "username": user.username} for user in obj.tagged_users.all()]

    def _tag(self, instance, tagged_ids, actor):
        users = list(User.objects.filter(id__in=tagged_ids).exclude(pk=actor.pk))
        before = set(instance.tagged_users.values_list("id", flat=True))
        instance.tagged_users.set(users)
        new_users = [user for user in users if user.pk not in before]
        if new_users:
            self.notify_tagged(instance, new_users, actor)

    def notify_tagged(self, instance, users, actor):
        raise NotImplementedError

    def create(self, validated_data):
        tagged_ids = validated_data.pop("tagged_user_ids", None)
        instance = super().create(validated_data)
        if tagged_ids:
            self._tag(instance, tagged_ids, self.context["request"].user)
        return instance

    def update(self, instance, validated_data):
        tagged_ids = validated_data.pop("tagged_user_ids", None)
        instance = super().update(instance, validated_data)
        if tagged_ids is not None:
            self._tag(instance, tagged_ids, self.context["request"].user)
        return instance


class NewPeticionCommentSerializer(AttachmentAportacionMixin, serializers.ModelSerializer):
    created_by = SimpleUserSerializer(read_only=True)
    
    # 🛡️ FIX 500: Lee del related_name "replies", pero lo exporta como "children" para React Native
    children = RecursiveCommentSerializer(source='replies', many=True, read_only=True)
    
    likes_count = serializers.SerializerMethodField()
    user_has_liked = serializers.SerializerMethodField()
    # Personas etiquetadas: si aprueban, el comentario sale en su perfil como testimonio.
    tagged_users = serializers.SerializerMethodField()
    tagged_user_ids = serializers.ListField(
        child=serializers.UUIDField(), write_only=True, required=False, max_length=10,
    )

    class Meta:
        model = ms.NewPeticionCommentPost
        fields = [
            "id",
            "created_by",
            "parent",
            "post",
            "aportacion",
            "aportacion_info",
            "tagged_users",
            "tagged_user_ids",
            "text",
            "attachment",
            "attachment_url",
            "attachment_type",
            "children",
            "likes_count",
            "user_has_liked",
            "created_at",
            "updated_at",
        ]
        # 🛡️ FIX 400: Añadimos 'post' para que DRF no lo exija en la validación inicial
        read_only_fields = ["id", "created_by", "post"]
        extra_kwargs = {"text": {"required": False, "allow_blank": True}}

    def get_likes_count(self, obj):
        return related_count(obj, "likes")

    def get_user_has_liked(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return related_has_user(obj, "likes", request.user)
        return False

    def get_tagged_users(self, obj):
        # Las que rechazaron el testimonio ya no salen etiquetadas.
        return [
            {"id": str(tag.user_id), "username": tag.user.username, "status": tag.status}
            for tag in obj.tags.all()
            if tag.status != ms.CommentTag.STATUS_REJECTED
        ]

    def create(self, validated_data):
        from .testimonials import sync_comment_tags

        tagged_ids = validated_data.pop("tagged_user_ids", None)
        comment = super().create(validated_data)
        if tagged_ids:
            sync_comment_tags(comment, tagged_ids)
        return comment

    def update(self, instance, validated_data):
        from .testimonials import sync_comment_tags

        tagged_ids = validated_data.pop("tagged_user_ids", None)
        comment = super().update(instance, validated_data)
        if tagged_ids is not None:
            sync_comment_tags(comment, tagged_ids)
        return comment


class SubTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.SubTask # Asegúrate de que el modelo sea el correcto
        fields = '__all__'

class SubFactoresSerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.SubFactores
        fields = '__all__'

class SubFuentesSerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.SubFuentes
        fields = '__all__'



class TaskSerializer(serializers.ModelSerializer):
    user = TaskAuthorSerializer(read_only=True)
    likes_count = serializers.SerializerMethodField()
    period_likes_count = serializers.IntegerField(source="period_like_count", read_only=True)
    views_count = serializers.SerializerMethodField()
    # Historias: si quien consulta ya la vio (aro gris en su avatar).
    viewed_by_me = serializers.SerializerMethodField()
    user_has_liked = serializers.SerializerMethodField()
    user_has_reposted = serializers.SerializerMethodField()
    # Cuántas personas la impulsaron (el número junto al botón de impulsar). No es
    # `interaction_score`, que también sube al compartir.
    reposts_count = serializers.SerializerMethodField()
    # Cuántas veces la adjuntaron en comentarios, el foro o compartidas ("citada").
    citations_count = serializers.SerializerMethodField()
    comments_count = serializers.SerializerMethodField()
    # ✅ FIX: Especificamos que el campo 'comments' debe leer de la relación 'post_comments' del modelo Task.
    comments = NewPeticionCommentSerializer(source='post_comments', many=True, read_only=True)
    subtasks = SubTaskSerializer(many=True, read_only=True)
    subfactores = SubFactoresSerializer(many=True, read_only=True)
    subfuentes = SubFuentesSerializer(many=True, read_only=True)
    is_favorited = serializers.SerializerMethodField()
    is_original = serializers.SerializerMethodField()
    shared_by_list = serializers.SerializerMethodField()
    favorite_shared_by_list = serializers.SerializerMethodField()
    favorite_shared_by = serializers.SerializerMethodField()
    favorite_sharers_count = serializers.SerializerMethodField()
    tagged_users = serializers.SerializerMethodField()
    podcast_invitation_status = serializers.SerializerMethodField()
    app_has_liked = serializers.SerializerMethodField()
    support_total = serializers.SerializerMethodField()
    group_info = serializers.SerializerMethodField()
    # Testimonio: en qué perfil se escribió ("Testimonio para @...").
    testimonial_for_info = serializers.SerializerMethodField()
    # Anterior / siguiente recomendado y la serie completa (solo en el detalle:
    # context["with_series"]). Ver task_series.py.
    next_task_info = serializers.SerializerMethodField()
    previous_task_info = serializers.SerializerMethodField()
    series = serializers.SerializerMethodField()
    # Historia que comparte un testimonio: su texto, para verla como tarjeta con la cita.
    testimonial_story = serializers.SerializerMethodField()

    class Meta:
        model = ms.Task
        fields = '__all__'
        read_only_fields = [
            "id", "user", "share_count", "created_at",
            "is_story", "story_saved", "story_prompted",
            "group_status", "group_reviewed_by", "group_reviewed_at",
            "testimonial_status",
        ]

    def _series_visible(self):
        from .views import visible_tasks_for

        request = self.context.get("request")
        if not self.context.get("with_series") or not request:
            return None
        return visible_tasks_for(request.user)

    @staticmethod
    def _series_data(task, chosen=True):
        return {
            "id": str(task.pk),
            "title": task.title,
            "pch": task.pch,
            "user": {"id": str(task.user_id), "username": task.user.username if task.user else ""},
            # Si su autor la eligió (o es la que recomienda esta como siguiente).
            "chosen": chosen,
        }

    def get_next_task_info(self, obj):
        from .task_series import resolve_next

        visible = self._series_visible()
        if visible is None:
            return None
        task, chosen = resolve_next(obj, visible)
        return self._series_data(task, chosen) if task else None

    def get_previous_task_info(self, obj):
        from .task_series import resolve_previous

        visible = self._series_visible()
        if visible is None:
            return None
        task, chosen = resolve_previous(obj, visible)
        return self._series_data(task, chosen) if task else None

    def get_series(self, obj):
        """Las partes en orden (solo si hay más de una), marcando la actual."""
        from .task_series import series_of

        visible = self._series_visible()
        if visible is None:
            return None
        parts = series_of(obj, visible)
        if len(parts) < 2:
            return None
        return [{**self._series_data(part), "current": part.pk == obj.pk} for part in parts]

    def validate_previous_task(self, value):
        return self.validate_next_task(value)

    def validate_next_task(self, value):
        from .views import visible_tasks_for

        if value is None:
            return value
        instance = getattr(self, "instance", None)
        if instance is not None and value.pk == instance.pk:
            raise serializers.ValidationError("No puede recomendarse a sí misma.")
        request = self.context.get("request")
        if request and not visible_tasks_for(request.user).filter(pk=value.pk).exists():
            raise serializers.ValidationError("Esa publicación no existe.")
        return value

    def get_testimonial_story(self, obj):
        if not obj.story_source_task_id:
            return None
        source = obj.story_source_task
        if source is None or not source.testimonial_for_id:
            return None
        return {
            "title": source.title,
            "text": source.description,
            "for_username": source.testimonial_for.username if source.testimonial_for else "",
        }

    def get_testimonial_for_info(self, obj):
        recipient = obj.testimonial_for
        if recipient is None:
            return None
        return {"id": str(recipient.pk), "username": recipient.username}

    def _validate_testimonial(self, attrs):
        """Un testimonio es una publicación en el perfil de OTRA persona (pch "testimonios").
        La pch "testimonios" no se usa para nada más y el perfil no se cambia después."""
        instance = getattr(self, "instance", None)
        if instance is not None and "testimonial_for" in attrs and attrs["testimonial_for"] != instance.testimonial_for:
            raise serializers.ValidationError({"testimonial_for": "Un testimonio no se puede mover a otro perfil."})
        recipient = attrs.get("testimonial_for", getattr(instance, "testimonial_for", None))
        pch = str(attrs.get("pch", getattr(instance, "pch", "")) or "").strip().casefold()
        if recipient is None:
            if pch == ms.TESTIMONIAL_PCH:
                raise serializers.ValidationError({"pch": "Los testimonios se escriben en el perfil de alguien."})
            return
        request = self.context.get("request")
        if instance is None and getattr(getattr(request, "user", None), "pk", None) == recipient.pk:
            raise serializers.ValidationError({"testimonial_for": "No puedes escribir un testimonio en tu propio perfil."})
        if attrs.get("group") is not None:
            raise serializers.ValidationError({"group": "Un testimonio no se publica en un grupo."})
        attrs["pch"] = ms.TESTIMONIAL_PCH
        attrs["is_hidden"] = False

    def get_group_info(self, obj):
        """El grupo donde se publicó (nombre para la etiqueta "en el grupo X")."""
        group = obj.group
        if group is None:
            return None
        return {"id": str(group.id), "name": group.name, "is_private": group.is_private}

    def validate_group(self, group):
        """Solo se elige al crear la tarea, y hay que ser integrante del grupo."""
        if self.instance is not None:
            if group != self.instance.group:
                raise serializers.ValidationError("Una publicación no se puede mover de grupo.")
            return group
        if group is None:
            return group
        request = self.context.get("request")
        user = getattr(request, "user", None)
        is_member = ms.CommunityGroupMember.objects.filter(
            group=group, user=user, status=ms.CommunityGroupMember.STATUS_ACTIVE,
        ).exists() if getattr(user, "is_authenticated", False) else False
        if not is_member:
            raise serializers.ValidationError("Únete al grupo para publicar en él.")
        return group

    def validate_video(self, value):
        return validate_video_upload(value)

    def validate_image(self, value):
        return validate_image_upload(value)

    def get_is_original(self, obj):
        # Por defecto, si serializamos una Task directamente, es original.
        return True

    def get_tagged_users(self, obj):
        return [
            {
                "id": str(tag.user.id),
                "username": getattr(tag.user, "username", None),
                "first_name": getattr(tag.user, "first_name", "") or "",
                "last_name": getattr(tag.user, "last_name", "") or "",
            }
            for tag in obj.tagged_users.all()
        ]

    def get_podcast_invitation_status(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        if is_prefetched(obj, "podcast_invitations"):
            invitation = next(
                (i for i in obj.podcast_invitations.all() if i.user_id == request.user.id),
                None,
            )
        else:
            invitation = obj.podcast_invitations.filter(user=request.user).first()
        return invitation.status if invitation else None

    def get_support_total(self, obj):
        """Lo recaudado por una petición (en MXN). None si la tarea no es petición."""
        from .petition_supports import is_petition, support_summary
        if not is_petition(obj):
            return None
        supports = obj.petition_supports.all()
        if not is_prefetched(obj, "petition_supports"):
            supports = supports.filter(status=ms.PetitionSupport.STATUS_PAID)
        return support_summary(supports)

    def get_app_has_liked(self, obj):
        """¿Ya di mi like como la App? Solo para staff y superadmins (menú "Like de la App").

        Para la propia cuenta de la App, cualquier like suyo es de la App."""
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return None
        is_app_account = user.pk == app_account_id_for(request)
        if is_prefetched(obj, "likes"):
            return any(
                like.user_id == user.pk and (like.as_app or is_app_account) for like in obj.likes.all()
            )
        likes = obj.likes.filter(user_id=user.pk)
        return likes.exists() if is_app_account else likes.filter(as_app=True).exists()

    # Decimales que se guardan de la ubicación: 3 son unos 100 m (no la casa exacta).
    LOCATION_DECIMALS = 3

    def _validate_location(self, attrs):
        """La ubicación va completa (latitud y longitud) o vacía, y se guarda redondeada."""
        if "latitude" not in attrs and "longitude" not in attrs:
            return
        latitude, longitude = attrs.get("latitude"), attrs.get("longitude")
        if (latitude is None) != (longitude is None):
            raise serializers.ValidationError({"latitude": "La ubicación necesita latitud y longitud."})
        if latitude is None:
            return
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise serializers.ValidationError({"latitude": "La ubicación no es válida."})
        attrs["latitude"] = round(latitude, self.LOCATION_DECIMALS)
        attrs["longitude"] = round(longitude, self.LOCATION_DECIMALS)

    def validate(self, attrs):
        """Los estados de moderación solo cambian mediante el endpoint de aprobación."""
        self._validate_testimonial(attrs)
        self._validate_location(attrs)
        instance = getattr(self, "instance", None)
        group = attrs.get("group", getattr(instance, "group", None))
        if group is not None and attrs.get("is_hidden"):
            raise serializers.ValidationError({
                "is_hidden": "Las publicaciones de un grupo no se guardan ocultas: las revisa un admin del grupo.",
            })
        if "categories" in attrs or "pch" in attrs:
            def moderation_statuses(value):
                statuses = set()
                for category in str(value or "").split(","):
                    normalized = category.strip().casefold()
                    if normalized in APPROVED_TAG_ALIASES:
                        statuses.add(APPROVED_TAG)
                    elif normalized == "procesando":
                        statuses.add("procesando")
                return statuses

            categories_value = attrs.get(
                "categories",
                getattr(instance, "categories", ""),
            )
            new_statuses = moderation_statuses(categories_value)
            old_statuses = moderation_statuses(instance.categories) if instance else set()

            if instance and new_statuses != old_statuses:
                raise serializers.ValidationError({
                    "categories": "Los estados de moderación solo se pueden cambiar con la acción de aprobación."
                })
            if not instance and new_statuses:
                raise serializers.ValidationError({
                    "categories": "El estado inicial de moderación se asigna automáticamente."
                })

            new_categories = {
                str(category).strip().casefold()
                for category in str(categories_value or "").split(",")
                if str(category).strip()
            }
            is_podcast = "grabar podcast" in new_categories
            if (
                is_podcast
                and APPROVED_TAG not in new_statuses
                and "procesando" not in new_statuses
            ):
                categories_value = f"{categories_value or ''}, Procesando".strip(", ")
                attrs["categories"] = categories_value
        return attrs

    def get_is_favorited(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return obj.pk in my_ids(request, "fav", ms.Favorito, "task_id")
        return False

    def _get_share_img_cache(self):
        if not hasattr(self, "_share_img_cache"):
            self._share_img_cache = {}
        return self._share_img_cache

    def _get_sorted_shared_instances(self, obj):
        if not hasattr(self, "_sorted_shared_instances_cache"):
            self._sorted_shared_instances_cache = {}

        task_key = str(obj.id)
        if task_key not in self._sorted_shared_instances_cache:
            shared_instances = list(obj.shared_instances.all())
            shared_instances.sort(key=lambda share: (share.created_at, str(share.id)))
            self._sorted_shared_instances_cache[task_key] = shared_instances
        return self._sorted_shared_instances_cache[task_key]

    def _serialize_shared_instance(self, share, request):
        shared_by = share.shared_by
        if not shared_by:
            return {
                "id": None,
                "username": "unknown",
                "description": share.description or "",
                "user_image": None,
            }

        share_img_cache = self._get_share_img_cache()
        if shared_by.id not in share_img_cache:
            share_img_cache[shared_by.id] = latest_user_image_url(shared_by, request)

        return {
            "id": shared_by.id,
            "username": getattr(shared_by, "username", "unknown"),
            "description": share.description or "",
            "user_image": share_img_cache[shared_by.id],
        }

    def _get_favorite_profile_ids(self):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return set()

        if not hasattr(self, "_favorite_profile_ids_cache"):
            self._favorite_profile_ids_cache = my_ids(
                request, "pfav", ms.pFavorito, "perfil_id"
            )
        return self._favorite_profile_ids_cache

    def _get_favorite_shared_by_list(self, obj):
        if not hasattr(self, "_favorite_shared_by_cache"):
            self._favorite_shared_by_cache = {}

        task_key = str(obj.id)
        if task_key in self._favorite_shared_by_cache:
            return self._favorite_shared_by_cache[task_key]

        request = self.context.get("request")
        favorite_profile_ids = self._get_favorite_profile_ids()
        if not favorite_profile_ids:
            self._favorite_shared_by_cache[task_key] = []
            return self._favorite_shared_by_cache[task_key]

        favorite_shared = []
        for share in self._get_sorted_shared_instances(obj):
            if share.shared_by_id in favorite_profile_ids:
                favorite_shared.append(self._serialize_shared_instance(share, request))
        self._favorite_shared_by_cache[task_key] = favorite_shared
        return self._favorite_shared_by_cache[task_key]

    def get_shared_by_list(self, obj):
        request = self.context.get("request")
        return [
            self._serialize_shared_instance(share, request)
            for share in self._get_sorted_shared_instances(obj)
        ]

    def get_favorite_shared_by_list(self, obj):
        return self._get_favorite_shared_by_list(obj)

    def get_favorite_shared_by(self, obj):
        favorite_shared = self._get_favorite_shared_by_list(obj)
        return favorite_shared[-1] if favorite_shared else None

    def get_favorite_sharers_count(self, obj):
        return len(self._get_favorite_shared_by_list(obj))

    def _count_nested_comments(self, comment):
        """Cuenta un comentario y todas sus respuestas recursivamente"""
        count = 1  # Contar el comentario actual
        replies = comment.replies.all()  # Acceder a las respuestas anidadas (usa prefetch si existe)
        for reply in replies:
            count += self._count_nested_comments(reply)
        return count

    def get_likes_count(self, obj):
        return related_count(obj, "likes")

    def get_views_count(self, obj):
        if obj.pch != "historias":
            return 0
        annotated_count = getattr(obj, "views_count", None)
        if annotated_count is not None:
            return annotated_count
        return obj.story_views.count()

    def get_viewed_by_me(self, obj):
        if obj.pch != "historias":
            return False
        annotated = getattr(obj, "viewed_by_me", None)
        if annotated is not None:
            return bool(annotated)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False
        return obj.story_views.filter(viewer=user).exists()

    def get_user_has_liked(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            if is_prefetched(obj, "likes"):
                return any(
                    like.user_id == request.user.id and not like.is_hidden
                    for like in obj.likes.all()
                )
            return obj.likes.filter(user=request.user, is_hidden=False).exists()
        return False

    def get_reposts_count(self, obj):
        return related_count(obj, "reposts")

    def get_citations_count(self, obj):
        return sum(
            related_count(obj, relation)
            for relation in ("aportacion_comments", "forum_aportaciones", "shared_comment_aportaciones")
        )

    def get_user_has_reposted(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return related_has_user(obj, "reposts", request.user)
        return False

    def get_comments_count(self, obj):
        """Devuelve el total de comentarios anidados"""
        try:
            # Obtener solo los comentarios padre (sin parent). Se filtra en Python
            # para reutilizar el prefetch de `post_comments`.
            parent_comments = [c for c in obj.post_comments.all() if c.parent_id is None]
            total = sum(self._count_nested_comments(c) for c in parent_comments)
            return total
        except Exception as e:
            return 0
    

class SharedTaskSerializer(serializers.ModelSerializer):
    task = TaskSerializer(read_only=True)
    shared_by = SimpleUserSerializer(read_only=True)
    likes_count = serializers.SerializerMethodField()
    period_likes_count = serializers.IntegerField(source="period_like_count", read_only=True)
    user_has_liked = serializers.SerializerMethodField()
    comments_count = serializers.SerializerMethodField()

    class Meta:
        model = ms.SharedTask
        fields = [
            "id",
            "task",
            "shared_by",
            "description",
            "likes_count",
            "period_likes_count",
            "user_has_liked",
            "comments_count",
            "created_at",
        ]
        read_only_fields = ["id", "shared_by", "created_at"]

    def get_likes_count(self, obj):
        return related_count(obj, "likes")

    def get_user_has_liked(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return related_has_user(obj, "likes", request.user)
        return False

    def get_comments_count(self, obj):
        parent_comments = [c for c in obj.comments.all() if c.parent_id is None]
        total = 0
        for comment in parent_comments:
            total += self._count_nested_comments(comment)
        return total

    def _count_nested_comments(self, comment):
        count = 1
        for child in comment.replies.all():
            count += self._count_nested_comments(child)
        return count

    def create(self, validated_data):
        validated_data["shared_by"] = self.context["request"].user
        return super().create(validated_data)

# ============================================================================
# SERIALIZER PARA EL FEED UNIFICADO
# ============================================================================

class FeedItemSerializer(serializers.Serializer):
    """
    Serializador "adaptador" que sabe cómo manejar la clase proxy FeedItem.
    Determina si el item es una Task o una SharedTask y usa el serializador correcto.
    """
    def to_representation(self, instance):
        # `instance` aquí es un objeto `FeedItem`
        if instance.is_original:
            # Es una Task original
            serializer = TaskSerializer(instance.item, context=self.context)
        else:
            # Es una SharedTask
            serializer = SharedTaskSerializer(instance.item, context=self.context)
        
        data = serializer.data
        data['is_original'] = instance.is_original # Añadimos el flag para el frontend
        return data

# ============================================================================
# SERIALIZERS PARA IMÁGENES Y PERFILES
# ============================================================================

class PortadaSerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.Portada
        fields = ["id", "title", "image", "user", "created_at"]
        read_only_fields = ["id", "user", "created_at"]

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


class ImagenFijaSerializer(serializers.ModelSerializer):
    class Meta:
        model = ms.ImagenFija
        fields = ["id", "image", "user", "created_at"]
        read_only_fields = ["id", "user", "created_at"]

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


class PosttSerializer(MessageExtrasMixin, serializers.ModelSerializer):
    TEXT_FIELD = "content"

    user = SimpleUserSerializer(read_only=True)
    replies = serializers.SerializerMethodField()
    title = serializers.CharField(required=False, allow_blank=True)
    likes_count = serializers.SerializerMethodField()
    has_liked = serializers.SerializerMethodField()
    # Foro privado (ver forum_access.py): grupo, visibilidad e invitados.
    group_info = serializers.SerializerMethodField()
    invited_users = serializers.SerializerMethodField()
    invited_user_ids = serializers.ListField(
        child=serializers.UUIDField(), write_only=True, required=False, max_length=ms.Postt.MAX_INVITED,
    )

    class Meta:
        model = ms.Postt
        fields = [
            "id", "title", "content", "categories", "user", "parent", "replies", "created_at", "likes_count",
            "has_liked", "attachment", "attachment_url", "attachment_type", "aportacion", "aportacion_info",
            "tagged_users", "tagged_user_ids",
            "group", "group_info", "visibility", "invited_users", "invited_user_ids",
        ]
        read_only_fields = ["id", "user", "created_at", "likes_count", "has_liked"]
        extra_kwargs = {
            "content": {"required": False, "allow_blank": True},
            "categories": {"required": False, "allow_blank": True},
        }

    MAX_CATEGORIES = 20

    def validate_categories(self, value):
        """Igual que en las tareas ("Tema,Subtema,..."), sin repetidas y sin las etiquetas
        de estado (aprobada/procesando), que en el foro no aplican."""
        names, seen = [], set()
        for raw in str(value or "").split(","):
            name = " ".join(raw.split())[:100]
            key = name.casefold()
            if not name or key in seen or key in ms.STATUS_TAGS or key in APPROVED_TAG_ALIASES:
                continue
            seen.add(key)
            names.append(name)
        if len(names) > self.MAX_CATEGORIES:
            raise serializers.ValidationError(f"Máximo {self.MAX_CATEGORIES} categorías por post.")
        return ",".join(names)

    def get_group_info(self, obj):
        group = obj.group
        if group is None:
            return None
        return {"id": str(group.id), "name": group.name, "is_private": group.is_private}

    def get_invited_users(self, obj):
        # A quiénes invitó: solo lo ve el autor.
        request = self.context.get("request")
        if not request or getattr(request.user, "pk", None) != obj.user_id:
            return []
        return [{"id": str(user.id), "username": user.username} for user in obj.invited_users.all()]

    def validate(self, attrs):
        from .forum_access import can_view_post, forum_root

        attrs = super().validate(attrs)
        instance = getattr(self, "instance", None)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        parent = attrs.get("parent", getattr(instance, "parent", None))
        if parent is not None:
            # Una respuesta: solo en un post que puedes ver; hereda su grupo y su visibilidad.
            if not can_view_post(parent, user):
                raise serializers.ValidationError({"parent": "Ese post no existe."})
            attrs["group"] = forum_root(parent).group
            attrs.pop("visibility", None)
            attrs.pop("invited_user_ids", None)
            return attrs
        if instance is not None and "group" in attrs and attrs["group"] != instance.group:
            raise serializers.ValidationError({"group": "Un post no se puede mover de foro."})
        group = attrs.get("group")
        if instance is None and group is not None and not (user.is_staff or user.is_superuser):
            is_member = ms.CommunityGroupMember.objects.filter(
                group=group, user=user, status=ms.CommunityGroupMember.STATUS_ACTIVE,
            ).exists()
            if not is_member:
                raise serializers.ValidationError({"group": "Únete al grupo para publicar en su foro."})
        visibility = attrs.get("visibility", getattr(instance, "visibility", ms.Postt.VISIBILITY_PUBLIC))
        if visibility == ms.Postt.VISIBILITY_INVITED and instance is None and not attrs.get("invited_user_ids"):
            raise serializers.ValidationError({"invited_user_ids": "Elige al menos a una persona para invitar."})
        return attrs

    def _invite(self, instance, invited_ids, actor):
        from .notifications import create_notification

        users = list(User.objects.filter(id__in=invited_ids, is_active=True).exclude(pk=actor.pk))
        before = set(instance.invited_users.values_list("id", flat=True))
        instance.invited_users.set(users)
        for user in users:
            if user.pk in before:
                continue
            create_notification(
                recipient=user,
                actor=actor,
                notification_type="forum_invite",
                target_type="forum_post",
                target_id=instance.pk,
                data={"text": f"{actor.username or 'Alguien'} te invitó a una conversación del foro.", "title": instance.title},
                dedupe_key=f"forum_invite:{instance.pk}:{user.pk}",
            )

    def notify_tagged(self, instance, users, actor):
        from .notifications import create_notification

        from .mentions import display_name

        root = instance
        while root.parent_id:
            root = root.parent
        where = "una respuesta del foro" if instance.parent_id else "una publicación del foro"
        for user in users:
            create_notification(
                recipient=user,
                actor=actor,
                notification_type="forum_tag",
                target_type="forum_post",
                target_id=root.pk,
                data={
                    "text": f"{display_name(actor)} te etiquetó en {where}.",
                    "title": root.title,
                    "excerpt": (instance.content or "")[:140],
                },
                dedupe_key=f"forum_tag:{instance.pk}:{user.pk}",
            )

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        invited_ids = validated_data.pop("invited_user_ids", None)
        # Si no hay title o está vacío, usar null o un valor por defecto
        if not validated_data.get("title"):
            validated_data["title"] = ""
        instance = super().create(validated_data)
        if invited_ids and not instance.parent_id:
            self._invite(instance, invited_ids, validated_data["user"])
        # Ya con los invitados guardados: solo avisa a quien puede verlo.
        from .mentions import notify_forum_mentions
        notify_forum_mentions(instance)
        return instance

    def update(self, instance, validated_data):
        invited_ids = validated_data.pop("invited_user_ids", None)
        instance = super().update(instance, validated_data)
        if invited_ids is not None and not instance.parent_id:
            self._invite(instance, invited_ids, self.context["request"].user)
        from .mentions import notify_forum_mentions
        notify_forum_mentions(instance)
        return instance

    def get_replies(self, obj):
        # `forum_replies.all()` reutiliza el prefetch de la vista (si existe).
        replies = obj.forum_replies.all()
        return PosttSerializer(replies, many=True, context=self.context).data

    def get_likes_count(self, obj):
        return related_count(obj, "likes")

    def get_has_liked(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return related_has_user(obj, "likes", request.user)
        return False


# ============================================================================
# SERIALIZERS PARA COMENTARIOS Y LIKES DE TAREAS COMPARTIDAS
# ============================================================================

class RecursiveSharedCommentSerializer(serializers.Serializer):
    """Serializer recursivo para comentarios anidados de tareas compartidas"""
    def to_representation(self, value):
        serializer = SharedTaskCommentSerializer(value, context=self.context)
        return serializer.data


class SharedTaskCommentSerializer(MessageExtrasMixin, serializers.ModelSerializer):
    created_by = SimpleUserSerializer(read_only=True)
    children = RecursiveSharedCommentSerializer(source='replies', many=True, read_only=True)
    likes_count = serializers.SerializerMethodField()
    user_has_liked = serializers.SerializerMethodField()

    def notify_tagged(self, instance, users, actor):
        from .notifications import create_notification

        from .mentions import display_name

        where = (
            "una respuesta a un comentario de una publicación compartida" if instance.parent_id
            else "un comentario de una publicación compartida"
        )
        for user in users:
            create_notification(
                recipient=user,
                actor=actor,
                notification_type="shared_comment_tag",
                target_type="shared_task",
                target_id=instance.shared_task_id,
                data={
                    "text": f"{display_name(actor)} te etiquetó en {where}.",
                    "title": instance.shared_task.task.title,
                    "excerpt": (instance.text or "")[:140],
                },
                dedupe_key=f"shared_comment_tag:{instance.pk}:{user.pk}",
            )

    class Meta:
        model = ms.SharedTaskComment
        fields = [
            "id",
            "created_by",
            "parent",
            "shared_task",
            "text",
            "attachment",
            "attachment_url",
            "attachment_type",
            "aportacion",
            "aportacion_info",
            "tagged_users",
            "tagged_user_ids",
            "children",
            "likes_count",
            "user_has_liked",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "shared_task"]
        extra_kwargs = {"text": {"required": False, "allow_blank": True}}

    def get_likes_count(self, obj):
        return related_count(obj, "likes")

    def get_user_has_liked(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return related_has_user(obj, "likes", request.user)
        return False


class SharedTaskDetailSerializer(serializers.ModelSerializer):
    """Serializer extendido para tareas compartidas con comentarios y likes"""
    task = TaskSerializer(read_only=True)
    shared_by = SimpleUserSerializer(read_only=True)
    comments = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    user_has_liked = serializers.SerializerMethodField()
    comments_count = serializers.SerializerMethodField()
    # ✅ AÑADIMOS EL NUEVO CAMPO
    favorite_sharers_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = ms.SharedTask
        fields = [
            "id",
            "task",
            "shared_by",
            "description",
            "comments",
            "likes_count",
            "user_has_liked",
            "comments_count",
            "favorite_sharers_count",
            "created_at",
        ]
        read_only_fields = ["id", "shared_by", "created_at"]

    def get_comments(self, obj):
        """Retorna solo comentarios padre (nivel raíz)"""
        parent_comments = obj.comments.filter(parent__isnull=True)
        return SharedTaskCommentSerializer(parent_comments, many=True, context=self.context).data

    def get_likes_count(self, obj):
        return obj.likes.count()

    def get_user_has_liked(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            return ms.LikeSharedTask.objects.filter(user=request.user, shared_task=obj).exists()
        return False

    def _count_nested_comments(self, comment):
        """Cuenta comentarios anidados recursivamente"""
        count = 1
        for child in comment.replies.all():
            count += self._count_nested_comments(child)
        return count

    def get_comments_count(self, obj):
        """Retorna el conteo total de comentarios (incluyendo anidados)"""
        parent_comments = obj.comments.filter(parent__isnull=True)
        total = 0
        for comment in parent_comments:
            total += self._count_nested_comments(comment)
        return total
