import uuid
from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# Etiqueta reservada de aprobación (podcasts/peticiones). Solo staff puede ponerla.
APPROVED_TAG = "aprobada"
# Categoría que convierte una publicación en propuesta de podcast.
PODCAST_CATEGORY = "Grabar Podcast"
# Testimonios: técnicamente un tipo de aportación más (pch), pero no es una de las 3 PCH
# del frontend: no sale en el feed, solo en la pestaña Testimonios del perfil.
TESTIMONIAL_PCH = "testimonios"

# Etiquetas de estado: viven en `categories` pero no son categorías de filtro.
STATUS_TAGS = {"aprobada", "aprobadas", "procesando"}

# Espejo del árbol de subtemas del cliente: lo que aparezca aquí NO es categoría principal.
DEFAULT_SUBTHEMES = {
    "programacion": ["React", "Python", "Node.js", "Django", "React Native", "JavaScript", "Frontend", "Backend"],
    "tecnologia": ["Inteligencia Artificial", "Ciberseguridad", "Hardware", "Software", "Innovación"],
    "educacion": ["Matemáticas", "Idiomas", "Ciencias", "Historia", "Pedagogía"],
    "salud": ["Nutrición", "Ejercicio", "Bienestar Mental", "Medicina", "Psicología"],
    "finanzas": ["Inversiones", "Ahorro", "Criptomonedas", "Emprendimiento", "Economía"],
    "arte": ["Pintura", "Música", "Fotografía", "Cine", "Diseño"],
    "deportes": ["Fútbol", "Baloncesto", "Tenis", "Natación", "Fitness"],
}
SUBTHEME_NAMES = {
    name.casefold() for names in DEFAULT_SUBTHEMES.values() for name in names
}

# ============================================================================
# CLASES BASE Y GESTIÓN DE USUARIOS
# ============================================================================

class TimeStampedModel(models.Model):
    """Clase abstracta para añadir timestamps automáticamente"""
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        abstract = True


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError(_("Users must have an email address"))
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save()
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.update({"is_staff": True, "is_superuser": True, "is_active": True})
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """Modelo de Usuario Aportaciones con UUID e Email"""
    id = models.UUIDField(_("id"), primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(_("username"), max_length=150, unique=True, null=True, blank=True)
    email = models.EmailField(_("email address"), unique=True)
    first_name = models.CharField(_("first name"), max_length=255, blank=True)
    last_name = models.CharField(_("last name"), max_length=255, blank=True)
    is_active = models.BooleanField(_("is active"), default=True)
    is_staff = models.BooleanField(_("is staff"), default=False)
    date_joined = models.DateTimeField(_("date joined"), default=timezone.now)
    updated_at = models.DateTimeField(_("last updated"), auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    # createsuperuser también lo pide: sin usuario no se le puede mencionar ni etiquetar.
    REQUIRED_FIELDS = ["username"]

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ["-date_joined"]

    def __str__(self):
        return self.email


class Profile(TimeStampedModel):
    """Perfil extendido para la lógica de la app"""
    id = models.UUIDField(_("ID"), primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
        verbose_name=_("user"),
    )
    is_verified = models.BooleanField(_("is verified"), default=False)
    is_recommended = models.BooleanField(_("is recommended"), default=False)
    subscriptionActive = models.BooleanField(_("subscription active"), default=False)
    subscription_amount = models.DecimalField(
        _("subscription amount"), max_digits=10, decimal_places=2, default=0
    )
    personal_filter_public = models.BooleanField(
        _("show personal filter everywhere"), default=False
    )
    favorite_profiles_public = models.BooleanField(
        _("show favorite profiles"), default=False
    )
    favorite_tasks_public = models.BooleanField(
        _("show favorite tasks"), default=False
    )
    role = models.IntegerField(_("role"), default=1, help_text=_("1=user, 2=editor, 3=admin"))
    recommendations_public = models.BooleanField(
        _("show recommendations"), default=True,
        help_text=_("Si es falso, otros no ven las recomendaciones que recibió ni las que dio."),
    )
    # Redes sociales: {red: usuario, enlace o número} (ver social_links.py).
    social_links = models.JSONField(_("social links"), default=dict, blank=True)
    # Quién ve cada tipo de tu actividad: {tipo: private|subscribers|public} (ver profile_activity.py).
    activity_levels = models.JSONField(_("activity visibility"), default=dict, blank=True)
    # Permitir que otros vean la app "como yo": mi feed (ver view_as.py).
    feed_public = models.BooleanField(_("let others view my feed"), default=False)
    # Visitar perfiles sin aparecer en sus visitas (ver profile_visits.py).
    anonymous_visits = models.BooleanField(_("visit profiles anonymously"), default=False)
    language = models.CharField(
        _("language"), max_length=10, default="es",
        help_text=_("Idioma al que el traductor de mensajes traduce para esta persona."),
    )
    stripe_customer_id = models.CharField(
        _("Stripe customer"), max_length=255, blank=True, default="",
        help_text=_("Cliente de Stripe con el que esta persona paga sus suscripciones."),
    )

    class Meta:
        verbose_name = _("profile")
        verbose_name_plural = _("profiles")
        ordering = ["-user__date_joined"]

    def __str__(self):
        return f"Profile for {self.user.email}"


class ProfileVisit(models.Model):
    """Visitas a un perfil: una fila por visitante y día, con cuántas veces entró
    (ver profile_visits.py). `anonymous`: visitó en modo anónimo (no aparece)."""
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile_visits_received",
    )
    visitor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile_visits_made",
    )
    day = models.DateField()
    visits = models.PositiveIntegerField(default=1)
    anonymous = models.BooleanField(default=False)
    last_visited_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "visitor", "day"], name="profile_visit_owner_visitor_day_uniq"),
        ]
        indexes = [
            models.Index(fields=["owner", "-day"], name="profile_visit_owner_day_idx"),
        ]


class UserVerification(TimeStampedModel):
    """Quién verificó a quién (la marca es `Profile.is_verified`; esto es el historial
    que se puede mostrar en el perfil de ambos)."""
    verifier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="verifications_given",
    )
    verified = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="verifications_received",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["verifier", "verified"], name="unique_user_verification"),
        ]


class UserRecommendation(TimeStampedModel):
    """Un usuario recomienda a otro. Una por pareja, con comentario opcional.

    Es independiente de la marca de admin `Profile.is_recommended`.
    """
    recommender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="recommendations_given",
    )
    recommended = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="recommendations_received",
    )
    comment = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["recommender", "recommended"],
                name="unique_user_recommendation",
            ),
            models.CheckConstraint(
                condition=~models.Q(recommender=models.F("recommended")),
                name="prevent_self_recommendation",
            ),
        ]

    def __str__(self):
        return f"{self.recommender_id} recomienda a {self.recommended_id}"


class Follow(TimeStampedModel):
    """Seguir a un perfil. Jerarquía: Seguir -> Favorito (pFavorito) -> Anclado.

    Favoritar o anclar a alguien lo sigue automaticamente, y dejar de seguir
    lo quita de favoritos (y por tanto de anclados). Ver api/signals.py."""
    follower = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="following_relationships",
    )
    following = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="follower_relationships",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["follower", "following"],
                name="unique_user_follow",
            ),
            models.CheckConstraint(
                condition=~models.Q(follower=models.F("following")),
                name="prevent_self_follow",
            ),
        ]


class UserReport(TimeStampedModel):
    TARGET_USER = "user"
    TARGET_TASK = "task"
    TARGET_COMMENT = "comment"
    TARGET_SHARED_COMMENT = "shared_comment"
    TARGET_SHARED_TASK = "shared_task"
    TARGET_FORUM_POST = "forum_post"
    TARGET_CHOICES = [
        (TARGET_USER, "User"),
        (TARGET_TASK, "Task"),
        (TARGET_COMMENT, "Task comment"),
        (TARGET_SHARED_COMMENT, "Shared task comment"),
        (TARGET_SHARED_TASK, "Shared task"),
        (TARGET_FORUM_POST, "Forum post"),
    ]
    STATUS_PENDING = "pending"
    STATUS_RESOLVED = "resolved"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_DISMISSED, "Dismissed"),
    ]

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="submitted_reports",
    )
    target_type = models.CharField(max_length=24, choices=TARGET_CHOICES)
    target_id = models.CharField(max_length=64)
    reason = models.TextField(max_length=1000)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_reports",
    )
    resolution_note = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["status", "-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="report_status_date_idx"),
            models.Index(fields=["target_type", "target_id"], name="report_target_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["reporter", "target_type", "target_id"],
                condition=models.Q(status="pending"),
                name="unique_pending_reporter_target",
            ),
        ]


class Notification(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="triggered_notifications",
    )
    notification_type = models.CharField(max_length=64)
    target_type = models.CharField(max_length=32)
    target_id = models.CharField(max_length=255)
    secondary_target_type = models.CharField(max_length=32, blank=True, default="")
    secondary_target_id = models.CharField(max_length=255, blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    dedupe_key = models.CharField(max_length=255, blank=True, default="")
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["recipient", "is_read", "-created_at"],
                name="notification_rec_read_idx",
            ),
            models.Index(
                fields=["target_type", "target_id"],
                name="notification_target_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                condition=~models.Q(dedupe_key=""),
                name="notification_rec_dedupe_uniq",
            )
        ]

    def __str__(self):
        return f"{self.notification_type} for {self.recipient_id}"


class UserSavedFilter(TimeStampedModel):
    """Filtro propio guardado por el usuario en su perfil (CRUD).
    `filters` guarda en un solo campo JSON el mismo payload que produce
    el modal de filtros (category, status, date_filter, sort_by, etc.),
    para poder reaplicarlo sin tener que renderizar todas las tareas."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saved_filters",
    )
    name = models.CharField(_("name"), max_length=120)
    filters = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("user", "name")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.user_id})"


# ============================================================================
# CONTENIDO Y CATEGORÍAS
# ============================================================================

class NewCategory(models.Model):
    name = models.CharField(_("name"), max_length=100)
    # Vacío = categoría global (visible en todos los tipos de aportación).
    pch = models.CharField(
        _("pch"), max_length=20, blank=True, default="",
        help_text=_("Tipo de aportación dueño de esta categoría (consejos/peticiones/historias)"),
    )
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    # Para presentarla mejor (cabecera de la categoría): una imagen y de qué trata.
    description = models.TextField(_("description"), blank=True, default="", max_length=500)
    image = models.ImageField(upload_to="categories/", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "New Categories"
        unique_together = ("name", "pch")

    def __str__(self):
        return self.name


class CategoryP(models.Model):
    """Filtro personal del usuario: árbol de categorías propias (padre/hijo sin
    límite de profundidad), guardado aparte para consultarlo sin recorrer tareas."""
    name = models.CharField(_("name"), max_length=100)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="categoriesp")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    position = models.PositiveIntegerField(_("position"), default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "name", "parent")
        ordering = ["position", "created_at"]

    def __str__(self):
        return f"{self.name} ({self.user.email})"


class Task(TimeStampedModel):
    """Modelo principal de Peticiones/Recetas/Tareas"""
    # ✅ FIX: Estandarizar a UUID para consistencia en toda la app.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="tasks"
    )
    title = models.CharField(_("title"), max_length=1000, blank=True, default="")
    description = models.TextField(_("description"), blank=True, default="")
    pch = models.TextField(_("pch type"), blank=True, default="")
    username = models.TextField(blank=True, default="")
    categories = models.TextField(blank=True, default="")
    image = models.ImageField(upload_to="tasks/", null=True, blank=True)
    video = models.FileField(upload_to="tasks/videos/", null=True, blank=True)
    story_is_shared = models.BooleanField(default=False)
    # Historia publicada directamente (24h). Solo pasa a las aportaciones
    # publicas si el dueño la guarda (story_saved=True).
    is_story = models.BooleanField(default=False)
    story_saved = models.BooleanField(default=False)
    # Ya se le notifico al dueño que la historia termino (ver
    # notifications.ensure_story_expiry_notifications).
    story_prompted = models.BooleanField(default=False)
    story_source_task = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="story_copies",
    )
    # Modo oculto: solo su dueño la ve, en su perfil, como vista previa de
    # cómo quedaría publicada. Al quitarlo se publica para todos.
    is_hidden = models.BooleanField(_("hidden"), default=False)
    # "Compartir desde mi ubicación": dónde estaba su autor al publicarla (redondeado a
    # unos 100 m, ver TaskSerializer._validate_location). Las que la tienen salen en el mapa.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    # Testimonio escrito en el perfil de `testimonial_for` (pch "testimonios"): una
    # publicación con todas sus funciones que esa persona aprueba antes de que se vea
    # (`testimonial_status`: pending / approved / rejected).
    testimonial_for = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="testimonial_tasks",
    )
    testimonial_status = models.CharField(max_length=16, blank=True, default="")
    # "Siguiente recomendado": la publicación que su autor recomienda ver después
    # (series de consejos, el próximo podcast...). Las que apuntan a esta son su "anterior".
    next_task = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="previous_tasks",
    )
    # "Anterior recomendado": la que su autor recomienda ver antes. Si no elige ninguna,
    # el anterior es la que recomienda esta como siguiente.
    previous_task = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="recommended_after",
    )
    # Publicada en un grupo: los integrantes la envían y un admin la aprueba
    # antes de que se vea (las de los admins se aprueban solas).
    group = models.ForeignKey(
        "CommunityGroup",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="tasks",
    )
    group_status = models.CharField(max_length=16, blank=True, default="")
    group_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_group_tasks",
    )
    group_reviewed_at = models.DateTimeField(null=True, blank=True)
    share_count = models.PositiveIntegerField(default=0)
    interaction_score = models.IntegerField(default=0, help_text=_("Score for feed ranking"))

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # Feed principal: filtro por tipo (pch) + fecha DESC
            models.Index(fields=["pch", "-created_at"], name="task_pch_date_idx"),
            # Perfil de usuario: sus tareas ordenadas por fecha
            models.Index(fields=["user", "-created_at"], name="task_user_date_idx"),
            # Filtro combinado tipo + propietario
            models.Index(fields=["pch", "user"], name="task_pch_user_idx"),
            # Ranking por interacción (sort_by=likes)
            models.Index(fields=["-interaction_score", "-created_at"], name="task_score_date_idx"),
            # Historias: pch=historias filtradas por story_is_shared
            models.Index(fields=["pch", "story_is_shared"], name="task_pch_shared_idx"),
        ]

    def __str__(self):
        return self.title or "Untitled Task"


# ============================================================================
# SOCIAL: LIKES, FAVORITOS Y COMPARTIDOS
# ============================================================================

class Like(TimeStampedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="task_likes")
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="likes")
    # Oculto: cuenta en el total pero el nombre no aparece en la lista hasta
    # que la misma persona vuelva a dar like (ver toggle_task_like).
    is_hidden = models.BooleanField(default=False)
    # Like dado "como la App" por alguien del staff: va en la pestaña App de la lista, con su nombre.
    as_app = models.BooleanField(default=False)

    class Meta:
        unique_together = ("user", "task")


class Repost(TimeStampedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="task_reposts",
    )
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="reposts")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "task"],
                name="task_repost_user_task_uniq",
            )
        ]


class StoryView(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    story = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="story_views")
    viewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="story_views",
    )
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["story", "viewer"],
                name="story_view_story_viewer_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["story", "-viewed_at"],
                name="story_view_story_date_idx",
            )
        ]


class LikeP(models.Model):
    """Likes entre perfiles"""
    # ✅ FIX DEFINITIVO: Cambiamos la relación de Profile a User.
    # El código existente (vistas, serializadores) ya pasaba un objeto User,
    # lo que causaba un error fatal en la base de datos y tumbaba el servidor (Error 502).
    # Al alinear el modelo con el uso real, eliminamos la causa raíz del problema.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="likes_given")
    profile = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="likes_received")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "profile")

class Favorito(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="user_favorites")
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="favorited_by")
    created_at = models.DateTimeField(auto_now_add=True)
    position = models.PositiveIntegerField(default=0)
    is_pinned = models.BooleanField(default=False)

    class Meta:
        unique_together = ("user", "task")
        indexes = [
            # Lista de favoritos del usuario (pantalla Favoritos)
            models.Index(fields=["user", "-created_at"], name="favorito_user_date_idx"),
            # Favoritos anclados arriba
            models.Index(fields=["user", "is_pinned", "position"], name="favorito_user_pin_idx"),
        ]


class TaskSave(models.Model):
    """Tarea guardada. Jerarquía: Guardar -> Favorito (Favorito) -> Top 5 (Favorito.is_pinned),
    igual que Seguir -> Favorito -> Anclado en los perfiles.

    Agregar a favoritos o anclar la guarda automáticamente; quitarla de guardados quita también
    el favorito, el top 5 y sus anclajes por categoría (ver signals.py).
    `is_private`: solo la ve su dueño aunque sus guardados sean públicos."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_tasks")
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="saved_by")
    is_private = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "task"], name="task_save_user_task_uniq"),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="task_save_user_date_idx"),
        ]


class SaveCollection(models.Model):
    """Colección (álbum) propia dentro de guardados: agrupa tareas guardadas.
    `is_private`: solo la ve su dueño (así empiezan)."""
    MAX_PER_USER = 50
    MAX_NAME_LENGTH = 60

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="save_collections")
    name = models.CharField(max_length=MAX_NAME_LENGTH)
    is_private = models.BooleanField(default=True)
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="save_collection_name_uniq"),
        ]

    def __str__(self):
        return f"{self.name} ({self.user_id})"


class SaveCollectionItem(models.Model):
    """Una guardada dentro de una colección. Quitarla de guardados la saca de todas."""
    collection = models.ForeignKey(SaveCollection, on_delete=models.CASCADE, related_name="items")
    saved = models.ForeignKey("TaskSave", on_delete=models.CASCADE, related_name="collection_items")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["collection", "saved"], name="save_collection_item_uniq"),
        ]


class TaskCategoryPin(models.Model):
    """Top 5 por categoría: una tarea favorita anclada en una de sus categorías."""
    MAX_PER_CATEGORY = 5

    favorite = models.ForeignKey(Favorito, on_delete=models.CASCADE, related_name="category_pins")
    category = models.CharField(max_length=120)
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["favorite", "category"], name="task_category_pin_uniq"),
        ]
        ordering = ["position", "created_at"]


class pFavorito(models.Model):
    """Favoritos de perfiles/usarios."""
    # Mantener esta relación en User para coincidir con el esquema real de Postgres.
    # El campo `perfil_id` en la base viva apunta a `api_user`, no a `api_profile`.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile_favorites_given")
    perfil = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile_favorites_received")
    created_at = models.DateTimeField(auto_now_add=True)
    position = models.PositiveIntegerField(default=0)
    is_pinned = models.BooleanField(default=False)

    class Meta:
        unique_together = ("user", "perfil")


class SharedTask(TimeStampedModel):
    # ✅ FIX: Estandarizar a UUID para consistencia.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="shared_instances")
    shared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shares")
    description = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # Feed de compartidas: por fecha DESC
            models.Index(fields=["-created_at"], name="sharedtask_date_idx"),
            # Compartidas de una tarea concreta (pantalla de detalle)
            models.Index(fields=["task", "-created_at"], name="sharedtask_task_date_idx"),
            # Compartidas de un usuario
            models.Index(fields=["shared_by", "-created_at"], name="sharedtask_by_date_idx"),
        ]


class TaskTag(TimeStampedModel):
    """Personas etiquetadas en una tarea/publicación (tag común de redes sociales)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="tagged_users")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tagged_in_tasks",
    )
    tagged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tags_made",
    )

    class Meta:
        unique_together = ("task", "user")

    def __str__(self):
        return f"{self.user_id} tagged in {self.task_id}"


class PodcastInvitation(TimeStampedModel):
    """Respuesta individual de una persona invitada a un podcast aprobado."""
    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_DECLINED = "declined"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pendiente"),
        (STATUS_ACCEPTED, "Aceptada"),
        (STATUS_DECLINED, "Rechazada"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="podcast_invitations")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="podcast_invitations",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("task", "user")


# ============================================================================
# COMENTARIOS Y JERARQUÍA
# ============================================================================

class NewPeticionCommentPost(TimeStampedModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="comments")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="replies")
    
    # ✅ FIX MIGRATIONS: Se especifican related_name únicos para evitar conflictos
    # al tener dos ForeignKey apuntando al mismo modelo (Task).
    post = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="post_comments")
    aportacion = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True, related_name="aportacion_comments")
    
    # Foto o video adjunto al comentario (como en la web antigua).
    attachment = models.FileField(upload_to="comments/", null=True, blank=True)
    text = models.TextField(_("comment text"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # Comentarios raíz de una tarea (parent=None)
            models.Index(fields=["post", "parent", "-created_at"], name="comment_post_parent_idx"),
        ]


class LikeCommentPost(TimeStampedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="comment_likes")
    comment = models.ForeignKey(NewPeticionCommentPost, on_delete=models.CASCADE, related_name="likes")

    class Meta:
        unique_together = ("user", "comment")


class CommentTag(TimeStampedModel):
    """Persona etiquetada en un comentario.

    Si la persona lo aprueba, el comentario aparece en su perfil como
    testimonio (sección Testimonios, visible para todos).
    """
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pendiente"),
        (STATUS_APPROVED, "Aprobado"),
        (STATUS_REJECTED, "Rechazado"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    comment = models.ForeignKey(NewPeticionCommentPost, on_delete=models.CASCADE, related_name="tags")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="comment_tags",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)

    class Meta:
        unique_together = ("comment", "user")
        indexes = [
            models.Index(fields=["user", "status", "-created_at"], name="commenttag_user_status_idx"),
        ]

    def __str__(self):
        return f"{self.user_id} tagged in comment {self.comment_id} ({self.status})"


# ============================================================================
# COMENTARIOS Y LIKES PARA TAREAS COMPARTIDAS
# ============================================================================

class SharedTaskComment(TimeStampedModel):
    """Comentarios anidados para tareas compartidas"""
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shared_task_comments")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="replies")
    shared_task = models.ForeignKey(SharedTask, on_delete=models.CASCADE, related_name="comments")
    text = models.TextField(_("comment text"), blank=True)
    # Lo que puede acompañar el comentario (como en los de las tareas): foto o video,
    # una de tus publicaciones guardadas y personas etiquetadas.
    attachment = models.FileField(upload_to="comments/", null=True, blank=True)
    aportacion = models.ForeignKey(
        "Task", on_delete=models.SET_NULL, null=True, blank=True, related_name="shared_comment_aportaciones",
    )
    tagged_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="shared_comment_tags",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # Comentarios de una tarea compartida (parent=None primero)
            models.Index(fields=["shared_task", "parent", "-created_at"], name="stcomment_task_parent_idx"),
        ]


class LikeSharedTaskComment(TimeStampedModel):
    """Likes para comentarios de tareas compartidas"""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shared_task_comment_likes")
    comment = models.ForeignKey(SharedTaskComment, on_delete=models.CASCADE, related_name="likes")

    class Meta:
        unique_together = ("user", "comment")


class LikeSharedTask(TimeStampedModel):
    """Likes para tareas compartidas (independiente del like de la tarea original)"""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shared_task_likes")
    shared_task = models.ForeignKey(SharedTask, on_delete=models.CASCADE, related_name="likes")

    class Meta:
        unique_together = ("user", "shared_task")


# ============================================================================
# IMÁGENES Y OTROS
# ============================================================================

class Portada(models.Model):
    title = models.CharField(max_length=255, null=True, blank=True)
    image = models.ImageField(upload_to="portadas/", null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="portadas")
    created_at = models.DateTimeField(auto_now_add=True)


class ImagenFija(TimeStampedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fixed_images")
    image = models.ImageField(upload_to="fixed/", null=True, blank=True)


class Postt(TimeStampedModel):
    """Hilo de foro simple. Quién lo ve: ver forum_access.py."""
    VISIBILITY_PUBLIC = "public"
    VISIBILITY_FOLLOWERS = "followers"
    VISIBILITY_SUBSCRIBERS = "subscribers"
    VISIBILITY_INVITED = "invited"
    VISIBILITY_CHOICES = [
        (VISIBILITY_PUBLIC, "Todos"),
        (VISIBILITY_FOLLOWERS, "Seguidores"),
        (VISIBILITY_SUBSCRIBERS, "Suscriptores"),
        (VISIBILITY_INVITED, "Invitados"),
    ]
    MAX_INVITED = 50

    title = models.CharField(max_length=100, blank=True)
    content = models.TextField(blank=True)
    # Mismo formato que Task.categories ("Tema,Subtema,..."): de la app o propias del autor.
    categories = models.TextField(blank=True, default="")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="forum_posts")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="forum_replies")
    # Lo que puede acompañar un post o respuesta: foto o video, una de tus publicaciones
    # guardadas y personas etiquetadas.
    attachment = models.FileField(upload_to="forum/", null=True, blank=True)
    aportacion = models.ForeignKey(
        "Task", on_delete=models.SET_NULL, null=True, blank=True, related_name="forum_aportaciones",
    )
    tagged_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="forum_post_tags",
    )
    # Foro de un grupo (vacío = foro general). Las respuestas heredan el grupo y la
    # visibilidad de su post.
    group = models.ForeignKey(
        "CommunityGroup", on_delete=models.CASCADE, null=True, blank=True, related_name="forum_posts",
    )
    visibility = models.CharField(max_length=16, choices=VISIBILITY_CHOICES, default=VISIBILITY_PUBLIC)
    invited_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="forum_invitations",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # Posts raíz del foro (parent=None) ordenados por fecha
            models.Index(fields=["parent", "-created_at"], name="postt_parent_date_idx"),
            # Posts de un usuario
            models.Index(fields=["user", "-created_at"], name="postt_user_date_idx"),
        ]


class LikePostt(TimeStampedModel):
    """Likes para posts del foro"""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="post_likes")
    post = models.ForeignKey(Postt, on_delete=models.CASCADE, related_name="likes")

    class Meta:
        unique_together = ("user", "post")


# ============================================================================
# APORTACIONES INTERNAS (SUBTASKS, FUENTES, FACTORES)
# ============================================================================

class SubTask(models.Model):
    parent_task = models.ForeignKey(Task, related_name='subtasks', on_delete=models.CASCADE)
    title = models.CharField(max_length=1000, blank=True, default='')
    description = models.TextField(blank=True, default='')
    image = models.ImageField(upload_to="subtasks/images/", null=True, blank=True)
    video = models.FileField(upload_to="subtasks/videos/", null=True, blank=True)
    link = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

class SubFuentes(models.Model):
    parent_task = models.ForeignKey(Task, related_name='subfuentes', on_delete=models.CASCADE)
    title = models.CharField(max_length=1000, blank=True, default='')
    description = models.TextField(blank=True, default='')
    image = models.ImageField(upload_to="subfuentes/images/", null=True, blank=True)
    video = models.FileField(upload_to="subfuentes/videos/", null=True, blank=True)
    link = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

class SubFactores(models.Model):
    parent_task = models.ForeignKey(Task, related_name='subfactores', on_delete=models.CASCADE)
    title = models.CharField(max_length=1000, blank=True, default='')
    description = models.TextField(blank=True, default='')
    image = models.ImageField(upload_to="subfactores/images/", null=True, blank=True)
    video = models.FileField(upload_to="subfactores/videos/", null=True, blank=True)
    link = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

# Tambien faltan los comentarios de los Subtasks, añadimos las referencias base
class SubTaskCommentPost(models.Model):
    parent_task = models.ForeignKey(NewPeticionCommentPost, related_name='subtasks', on_delete=models.CASCADE)
    title = models.CharField(max_length=1000, blank=True, default='')
    description = models.TextField(blank=True, default='')
    link = models.TextField(blank=True, default='')
    image = models.ImageField(upload_to="comments/subtasks/images/", null=True, blank=True)
    video = models.FileField(upload_to="comments/subtasks/videos/", null=True, blank=True)

class SubFactoresCommentPost(models.Model):
    parent_task = models.ForeignKey(NewPeticionCommentPost, related_name='subfactores', on_delete=models.CASCADE)
    title = models.CharField(max_length=1000, blank=True, default='')
    description = models.TextField(blank=True, default='')
    link = models.TextField(blank=True, default='')
    image = models.ImageField(upload_to="comments/subfactores/images/", null=True, blank=True)
    video = models.FileField(upload_to="comments/subfactores/videos/", null=True, blank=True)

class SubFuentesCommentPost(models.Model):
    parent_task = models.ForeignKey(NewPeticionCommentPost, related_name='subfuentes', on_delete=models.CASCADE)
    title = models.CharField(max_length=1000, blank=True, default='')
    description = models.TextField(blank=True, default='')
    link = models.TextField(blank=True, default='')
    image = models.ImageField(upload_to="comments/subfuentes/images/", null=True, blank=True)
    video = models.FileField(upload_to="comments/subfuentes/videos/", null=True, blank=True)



class PasswordResetCode(models.Model):
    """Código de un solo uso para recuperar la contraseña. Solo se guarda su hash."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="password_reset_codes",
    )
    code_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "-created_at"], name="password_reset_user_idx")]


# ── Push Notifications ────────────────────────────────────────────────────────

class PushToken(models.Model):
    """
    Almacena los ExponentPushToken de los dispositivos de cada usuario.
    Un usuario puede tener múltiples tokens activos (varios dispositivos),
    hasta el límite definido en push_views.MAX_ACTIVE_TOKENS_PER_USER.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_tokens",
    )
    token = models.CharField(max_length=250, unique=True)
    platform = models.CharField(
        max_length=10,
        choices=[("ios", "iOS"), ("android", "Android"), ("web", "Web")],
        default="ios",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["user", "is_active"],
                name="pushtoken_user_active_idx",
            ),
        ]

    def __str__(self):
        return f"PushToken({self.user_id}, {self.platform})"


class Subscription(TimeStampedModel):
    """Suscripción mensual que `subscriber` paga a favor de `recipient`.

    Quien paga elige el monto (mínimo 10 MXN o 1 USD) y si se renueva sola.
    Puede ser para sí mismo o un regalo a otro perfil. El estado lo manda
    Stripe; `Profile.subscriptionActive/subscription_amount` se recalculan a
    partir de las suscripciones activas que recibe cada perfil.
    """
    CURRENCY_MXN = "mxn"
    CURRENCY_USD = "usd"
    CURRENCY_CHOICES = [(CURRENCY_MXN, "MXN"), (CURRENCY_USD, "USD")]

    STATUS_PENDING = "pending"
    # El resto son los estados de Stripe tal cual (active, past_due, canceled...).
    ACTIVE_STATUSES = ("active", "trialing")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscriber = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscriptions_paid",
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscriptions_received",
    )
    amount_cents = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES)
    auto_renew = models.BooleanField(default=True)
    status = models.CharField(max_length=32, default=STATUS_PENDING)
    current_period_end = models.DateTimeField(null=True, blank=True)
    stripe_checkout_session_id = models.CharField(max_length=255, blank=True, default="")
    stripe_subscription_id = models.CharField(max_length=255, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "status"], name="subscription_recipient_idx"),
            models.Index(fields=["subscriber", "status"], name="subscription_subscriber_idx"),
        ]

    @property
    def is_active(self):
        return (
            self.status in self.ACTIVE_STATUSES
            and self.current_period_end is not None
            and self.current_period_end > timezone.now()
        )

    def __str__(self):
        return f"Subscription({self.subscriber_id} -> {self.recipient_id}, {self.amount_cents} {self.currency})"

class PetitionSupport(TimeStampedModel):
    """Apoyo monetario único que `supporter` da a una petición.

    Lo puede dar cualquiera, también quien creó la petición. Solo cuentan los
    pagados; los pendientes son sesiones de Checkout que aún no se pagan.
    """
    STATUS_PENDING = "pending"
    STATUS_PAID = "paid"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [(STATUS_PENDING, "Pendiente"), (STATUS_PAID, "Pagado"), (STATUS_EXPIRED, "Expirado")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="petition_supports")
    supporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="petition_supports",
    )
    amount_cents = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, choices=Subscription.CURRENCY_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    paid_at = models.DateTimeField(null=True, blank=True)
    stripe_checkout_session_id = models.CharField(max_length=255, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["task", "status"], name="petition_support_task_idx")]

    def __str__(self):
        return f"PetitionSupport({self.supporter_id} -> {self.task_id}, {self.amount_cents} {self.currency})"



class CommunityGroup(TimeStampedModel):
    """Grupo de la red social (no confundir con los grupos de chat de `massaging`).

    Cualquiera lo crea con nombre, descripción y reglas. Público: se entra al
    instante. Privado: un admin aprueba cada solicitud y sus publicaciones solo
    las ven sus integrantes.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    rules = models.TextField(blank=True, default="")
    image = models.ImageField(upload_to="groups/", null=True, blank=True)
    is_private = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_community_groups",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class CommunityGroupMember(TimeStampedModel):
    ROLE_OWNER = "owner"
    ROLE_ADMIN = "admin"
    ROLE_MEMBER = "member"
    ROLE_CHOICES = [(ROLE_OWNER, "Creador"), (ROLE_ADMIN, "Admin"), (ROLE_MEMBER, "Integrante")]
    ADMIN_ROLES = (ROLE_OWNER, ROLE_ADMIN)

    STATUS_ACTIVE = "active"
    # Solicitud para entrar a un grupo privado, esperando a un admin.
    STATUS_PENDING = "pending"
    STATUS_CHOICES = [(STATUS_ACTIVE, "Activo"), (STATUS_PENDING, "Solicitud pendiente")]

    group = models.ForeignKey(CommunityGroup, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="community_memberships",
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["group", "user"], name="unique_community_group_member"),
        ]
        indexes = [models.Index(fields=["user", "status"], name="community_member_user_idx")]

    @property
    def is_admin(self):
        return self.status == self.STATUS_ACTIVE and self.role in self.ADMIN_ROLES

    def __str__(self):
        return f"{self.user_id} en {self.group_id} ({self.role}, {self.status})"
