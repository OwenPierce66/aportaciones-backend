from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import prefetch_related_objects
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.db import transaction, IntegrityError
from django.shortcuts import get_object_or_404
from django.db.models import Case, When, Value, IntegerField, CharField
from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from datetime import timedelta

from rest_framework import status, generics
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAuthenticatedOrReadOnly
from rest_framework.pagination import PageNumberPagination, LimitOffsetPagination
from rest_framework.parsers import JSONParser, MultiPartParser, FormParser
from rest_framework_simplejwt.tokens import RefreshToken
from uuid import UUID
import re
import json
import unicodedata

from .serializers import ProfileSerializer, file_to_abs_url
from .serializers import (
    AdminUserSerializer,
    UserSerializer,
    SimpleUserSerializer,
    UserReportSerializer,
    NewCategorySerializer,
    CategoryPSerializer,
    PersonalFilterVisibilitySerializer,
    UserSavedFilterSerializer,
    TaskSerializer,
    SharedTaskSerializer,
    SharedTaskDetailSerializer,
    NewPeticionCommentSerializer,
    FeedItemSerializer,
    SharedTaskCommentSerializer,
    FavoritoSerializer,
    PortadaSerializer,
    ImagenFijaSerializer,
    PosttSerializer
)
from . import models as ms
from . import app_like_requests, mentions
from .account import revoke_sessions
from .app_account import app_account_email, app_account_id_for, get_app_account
from .notifications import retire_notification, create_notification
from .signals import notify_task_tags_on_publish
from . import task_saves
from . import testimonials
from .social_links import clean_social_links
from .forum_access import can_view_post, visible_forum_posts
from .task_series import sync_series_links
from .view_as import can_view_feed_of, feed_owner, followed_ids
from .profile_visits import visit_counts
from itertools import chain
from massaging.models import Message
from . import throttling as ts
from django.db.models import Case, F, IntegerField, Value, When
from django.core.cache import cache
import hashlib

User = get_user_model()


# ─────────────────────────────────────────────────────────────────────────────
# CACHÉ POR USUARIO — helper para cachear respuestas de lista
# ─────────────────────────────────────────────────────────────────────────────

def make_cache_key(prefix: str, user_id, query_params: dict, timeout: int = 300) -> str:
    """
    Genera una clave de caché única por: prefijo + usuario + parámetros de consulta.

    Usar el user_id en la clave garantiza que nunca se mezclen respuestas de
    usuarios distintos (cada uno ve sus favoritos, filtros personalizados, etc.).
    Los query_params se hashean con MD5 para mantener la clave corta.
    """
    params_str = "&".join(f"{k}={v}" for k, v in sorted(query_params.items()))
    params_hash = hashlib.md5(params_str.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{prefix}:u{user_id}:{params_hash}"



def category_tokens(category_filter):
    """"Tema, Subtema" -> ["Tema", "Subtema"]: sin vacías ni repetidas."""
    categories = []
    seen = set()
    for raw_category in (category_filter or "").split(","):
        category = " ".join(raw_category.split())
        normalized = category.casefold()
        if category and normalized not in seen:
            categories.append(category)
            seen.add(normalized)
    return categories


def filter_by_categories(queryset, category_filter, field_name):
    """Match every requested category token, including user-entered terms.

    Categories are still stored as a comma-separated legacy field, so matching
    must stay token-aware and must not turn a search for "art" into a match for
    "artificial". The same helper is used by original and shared tasks and by
    forum posts.
    """
    for category in category_tokens(category_filter):
        if category.casefold() in {"aprobada", "aprobadas", "aprovada", "aprovadas"}:
            pattern = r"(?:^|,)\s*(?:aprobada?s?|aprovada?s?)\s*(?:,|$)"
        else:
            pattern = rf"(?:^|,)\s*{re.escape(category)}\s*(?:,|$)"
        queryset = queryset.filter(**{f"{field_name}__iregex": pattern})
    return queryset


def prioritize_category_path(queryset, category_filter, field_name):
    """Put tasks whose comma-separated category path matches first."""
    path = ",".join(
        " ".join(part.split())
        for part in (category_filter or "").split(",")
        if " ".join(part.split())
    )
    if not path:
        return queryset

    escaped_path = re.escape(path)
    prefix_path = rf"^{escaped_path}(?:,|$)"
    exact_path = rf"^{escaped_path}$"
    return queryset.annotate(
        category_path_rank=Case(
            When(**{f"{field_name}__iregex": exact_path}, then=Value(0)),
            When(**{f"{field_name}__iregex": prefix_path}, then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        )
    )


def filter_by_category_path(queryset, category_filter, field_name="categories"):
    """Filtro por niveles ("Tema,Subtema") de una lista por fecha: los de esa ruta exacta
    primero y luego los más nuevos. Sin categoría no cambia nada."""
    if not category_tokens(category_filter):
        return queryset
    return prioritize_category_path(
        filter_by_categories(queryset, category_filter, field_name), category_filter, field_name,
    ).order_by("category_path_rank", "-created_at")


def normalize_filter_text(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in value if not unicodedata.combining(char)).casefold().strip()


def _like_window_start(sort_by):
    from datetime import timedelta
    from django.utils import timezone

    window_days = {"top_day": 1, "top_week": 7, "top_month": 30}.get(sort_by)
    return timezone.now() - timedelta(days=window_days) if window_days else None


def exclude_unsaved_stories(queryset, prefix=""):
    """Las historias publicadas directamente solo llegan a las aportaciones
    (feed, perfil, busqueda) si su dueño las guardó. `prefix` permite aplicarlo
    a relaciones (p. ej. "task__")."""
    return queryset.exclude(**{f"{prefix}is_story": True, f"{prefix}story_saved": False})


def exclude_hidden_tasks(queryset, owner=None, prefix="", viewer=None):
    """Las tareas en modo oculto solo las ve su dueño (`owner`); nadie más.
    `prefix` permite aplicarlo a relaciones (p. ej. "task__").

    También aplica las reglas de los grupos (ver `exclude_unavailable_group_tasks`).
    `viewer` es quien mira (por defecto `owner`): decide si ve lo de sus grupos privados."""
    hidden = Q(**{f"{prefix}is_hidden": True})
    if _is_user(owner):
        hidden &= ~Q(**{f"{prefix}user_id": owner.pk})
    queryset = queryset.exclude(hidden)
    who = viewer if viewer is not None else owner
    # Testimonios sin aprobar: solo quien lo escribió y la persona del perfil.
    unapproved_testimonial = (
        Q(**{f"{prefix}testimonial_for__isnull": False})
        & ~Q(**{f"{prefix}testimonial_status": TESTIMONIAL_APPROVED})
    )
    if _is_user(who):
        unapproved_testimonial &= ~Q(**{f"{prefix}user_id": who.pk}) & ~Q(**{f"{prefix}testimonial_for_id": who.pk})
    queryset = queryset.exclude(unapproved_testimonial)
    return exclude_unavailable_group_tasks(queryset, owner, who, prefix)


TESTIMONIAL_APPROVED = "approved"


def exclude_testimonials(queryset, prefix=""):
    """Los testimonios no son una de las 3 PCH: no salen en el feed ni en las listas de
    aportaciones; solo en la pestaña Testimonios del perfil."""
    return queryset.exclude(**{f"{prefix}testimonial_for__isnull": False})


def _is_user(user):
    return user is not None and getattr(user, "is_authenticated", False)


def exclude_unavailable_group_tasks(queryset, owner, viewer, prefix=""):
    """Tareas de grupos:
    - En revisión (pendientes o rechazadas): solo su autor y los admins del grupo, y
      solo donde se trabaja con la tarea (`owner`: su perfil, el detalle); nunca en el feed.
    - De un grupo privado: solo sus integrantes y su autor (`viewer`).
    """
    in_review = Q(**{f"{prefix}group__isnull": False}) & ~Q(**{f"{prefix}group_status": GROUP_TASK_APPROVED})
    private = Q(**{f"{prefix}group__is_private": True})
    if _is_user(owner):
        admin_groups = ms.CommunityGroupMember.objects.filter(
            user=owner, status=ms.CommunityGroupMember.STATUS_ACTIVE,
            role__in=ms.CommunityGroupMember.ADMIN_ROLES,
        ).values("group_id")
        in_review &= ~Q(**{f"{prefix}user_id": owner.pk}) & ~Q(**{f"{prefix}group_id__in": admin_groups})
    if _is_user(viewer):
        member_groups = ms.CommunityGroupMember.objects.filter(
            user=viewer, status=ms.CommunityGroupMember.STATUS_ACTIVE,
        ).values("group_id")
        private &= ~Q(**{f"{prefix}user_id": viewer.pk}) & ~Q(**{f"{prefix}group_id__in": member_groups})
    return queryset.exclude(in_review | private)


GROUP_TASK_PENDING = "pending"
GROUP_TASK_APPROVED = "approved"
GROUP_TASK_REJECTED = "rejected"


def visible_tasks_for(user):
    """Tareas con las que `user` puede interactuar (las públicas y sus ocultas)."""
    return exclude_hidden_tasks(ms.Task.objects.all(), user)


def ensure_visible_like(user, task):
    """Garantiza que `user` tenga un like visible en `task`.

    Si el like existia oculto (historia guardada), solo se vuelve a mostrar: el
    conteo no cambia. Devuelve (like, created)."""
    like, created = ms.Like.objects.get_or_create(user=user, task=task)
    if not created and like.is_hidden:
        like.is_hidden = False
        like.save(update_fields=["is_hidden"])
    return like, created


def apply_task_filters(queryset, params, owner_id=None, request_user=None, category_field="categories"):
    """Apply the shared task-filter contract to any task queryset."""
    from django.utils import timezone
    from datetime import timedelta
    from django.db.models import Count

    pch = params.get("pch")
    category = params.get("category")
    status_filter = params.get("status")
    date_filter = params.get("date_filter")
    sort_by = params.get("sort_by")
    raw_search = " ".join(str(params.get("search") or "").split())
    search = normalize_filter_text(raw_search)
    favorites_only = params.get("favorites_only")
    favorite_users_only = params.get("favorite_users_only")
    verified_users_only = params.get("verified_users_only")
    recommended_users_only = params.get("recommended_users_only")

    queryset = exclude_unsaved_stories(queryset)
    if str(pch or "").strip().casefold() != ms.TESTIMONIAL_PCH:
        queryset = exclude_testimonials(queryset)
    # Las ocultas solo aparecen en el perfil de su dueño, cuando él lo ve.
    viewing_own_profile = (
        owner_id is not None and request_user is not None
        and str(owner_id) == str(getattr(request_user, "pk", ""))
    )
    queryset = exclude_hidden_tasks(
        queryset, request_user if viewing_own_profile else None, viewer=request_user,
    )
    # Ver como otra persona (?as_user=): lo que publican quienes sigue, con mis permisos.
    view_as = feed_owner(params, request_user)
    relation_user = view_as or request_user
    if view_as is not None:
        queryset = queryset.filter(user_id__in=followed_ids(view_as))
    if owner_id:
        queryset = queryset.filter(user_id=owner_id)
    if pch:
        queryset = queryset.filter(pch__iexact=pch)
    if date_filter == "hoy":
        queryset = queryset.filter(created_at__gte=timezone.now() - timedelta(days=1))
    elif date_filter == "esta_semana":
        queryset = queryset.filter(created_at__gte=timezone.now() - timedelta(days=7))
    elif date_filter == "este_mes":
        queryset = queryset.filter(created_at__gte=timezone.now() - timedelta(days=30))
    if category:
        queryset = filter_by_categories(queryset, category, category_field)
        queryset = prioritize_category_path(queryset, category, category_field)
    if status_filter:
        queryset = filter_by_categories(queryset, status_filter, category_field)
    hashtag = mentions.hashtag_in_search(raw_search)
    if hashtag:
        # "#viaje" busca ese hashtag como palabra completa (no #viajes).
        queryset = queryset.filter(mentions.hashtag_q(hashtag, "title", "description"))
    elif search:
        # Free text is deliberately transient: it searches existing content
        # without creating catalog categories.
        search_query = (
            Q(title__icontains=raw_search)
            | Q(description__icontains=raw_search)
            | Q(**{f"{category_field}__icontains": raw_search})
        )
        if search != raw_search.casefold():
            search_query |= (
                Q(title__icontains=search)
                | Q(description__icontains=search)
                | Q(**{f"{category_field}__icontains": search})
            )
        queryset = queryset.filter(search_query)
    if favorites_only in ["true", "1", "True", True]:
        if owner_id:
            queryset = queryset.filter(favorited_by__user_id=owner_id)
        elif request_user and request_user.is_authenticated:
            queryset = queryset.filter(favorited_by__user=request_user)
    if favorite_users_only in ["true", "1", "True", True] and relation_user and relation_user.is_authenticated:
        queryset = queryset.filter(user__profile_favorites_received__user=relation_user).distinct()
    if params.get("followed_users_only") in ["true", "1", "True", True] and relation_user and relation_user.is_authenticated:
        queryset = queryset.filter(user__follower_relationships__follower=relation_user).distinct()
    if verified_users_only in ["true", "1", "True", True]:
        queryset = queryset.filter(user__profile__is_verified=True)
    if recommended_users_only in ["true", "1", "True", True]:
        queryset = queryset.filter(user__profile__is_recommended=True)
    if params.get("app_liked_only") in ["true", "1", "True", True]:
        # Con Exists (no un join) para no alterar el conteo de likes al ordenar.
        queryset = queryset.filter(Exists(
            ms.Like.objects.filter(task=OuterRef("pk")).filter(app_like_requests.app_like_q())
        ))
    if params.get("admin_users_only") in ["true", "1", "True", True]:
        queryset = queryset.filter(Q(user__is_staff=True) | Q(user__is_superuser=True))

    if sort_by == "likes":
        queryset = queryset.annotate(like_count=Count("likes")).order_by("-like_count", "-created_at")
        if category:
            queryset = queryset.order_by("category_path_rank", "-like_count", "-created_at")
    elif like_window_start := _like_window_start(sort_by):
        queryset = queryset.annotate(
            period_like_count=Count(
                "likes",
                filter=Q(likes__created_at__gte=like_window_start),
                distinct=True,
            )
        ).order_by("-period_like_count", "-created_at")
        if category:
            queryset = queryset.order_by(
                "category_path_rank",
                "-period_like_count",
                "-created_at",
            )
    else:
        if category:
            queryset = queryset.order_by("category_path_rank", "-created_at")
        else:
            queryset = queryset.order_by("-created_at")
    return queryset


# ============================================================================
# CONFIGURACIÓN DE PAGINACIÓN
# ============================================================================

from rest_framework.exceptions import NotFound

class StandardPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        try:
            return super().paginate_queryset(queryset, request, view=view)
        except NotFound:
            # Gracefully handle the error by returning an empty list instead of throwing 404
            self.request = request
            return []

    def get_paginated_response(self, data):
        if not hasattr(self, 'page') or self.page is None:
            return Response({
                'count': 0,
                'next': None,
                'previous': None,
                'results': data
            })
        return super().get_paginated_response(data)

class CommentPagination(LimitOffsetPagination):
    default_limit = 10
    max_limit = 50


class StoriesPagination(LimitOffsetPagination):
    default_limit = 60
    max_limit = 100

# ============================================================================
# AUTENTICACIÓN Y USUARIOS
# ============================================================================

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = [AllowAny]
    serializer_class = UserSerializer
    throttle_classes = [ts.RegisterThrottle]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        return Response({
            "user": UserSerializer(user, context={'request': request}).data,
            "refresh": str(refresh),
            "access": str(refresh.access_token),
        }, status=status.HTTP_201_CREATED)

class LogoutView(APIView):
    """
    POST /api/auth/logout/
    Body: { "refresh": "<refresh_token>" }

    Blacklistea el refresh token en la BD para que no pueda usarse más.
    Siempre responde 205 Reset Content para no bloquear el flujo del cliente,
    incluso si el token ya expiró o es inválido.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ts.LogoutThrottle]

    def post(self, request):
        refresh_token = request.data.get("refresh")

        if not refresh_token:
            return Response(
                {"detail": "Se requiere el refresh token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except Exception:
            # Token ya expirado, inválido o ya blacklisteado — no importa,
            # el objetivo (invalidar la sesión) ya se logró o nunca fue necesario.
            pass

        return Response(status=status.HTTP_205_RESET_CONTENT)


class UserMeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ts.UserMeThrottle]
    # ⚡ AÑADIMOS PARSERS PARA ACEPTAR IMÁGENES (multipart/form-data)
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        serializer = SimpleUserSerializer(request.user, context={'request': request})
        data = dict(serializer.data)
        profile = getattr(request.user, "profile", None)
        data["social_links"] = getattr(profile, "social_links", None) or {}
        # Las cuentas creadas con Google no tienen: al eliminarla, confirman con Google.
        data["has_password"] = request.user.has_usable_password()
        return Response(data)

    def patch(self, request, *args, **kwargs):
        """
        Permite a un usuario actualizar su propio perfil (nombre, apellido, imagen).
        """

        user = request.user
        
        image_file = request.FILES.get('user_image')

        # La imagen se valida y guarda por separado para no volver a validar
        # el archivo después de que Django ya lo haya consumido.
        profile_data = request.data.copy()
        profile_data.pop('user_image', None)
        # Redes sociales: llegan como JSON (en multipart, como texto).
        social_links = profile_data.pop('social_links', None)
        if isinstance(social_links, list):
            social_links = social_links[0] if social_links else None
        if isinstance(social_links, str):
            try:
                social_links = json.loads(social_links) if social_links.strip() else {}
            except ValueError:
                raise ValidationError({"social_links": "No es un JSON válido."})
        if social_links is not None:
            social_links = clean_social_links(social_links)

        with transaction.atomic():
            serializer = UserSerializer(
                user,
                data=profile_data,
                partial=True,
                context={'request': request},
            )
            serializer.is_valid(raise_exception=True)
            changed_password = bool(serializer.validated_data.get("password"))
            serializer.save()
            if changed_password:
                revoke_sessions(user)
            if image_file:
                ms.ImagenFija.objects.create(user=user, image=image_file)
            if social_links is not None:
                profile, _ = ms.Profile.objects.get_or_create(user=user)
                profile.social_links = social_links
                profile.save(update_fields=["social_links", "updated_at"])

        data = dict(SimpleUserSerializer(user, context={'request': request}).data)
        # Recién guardadas (el perfil del usuario puede venir en caché).
        data["social_links"] = (
            ms.Profile.objects.filter(user=user).values_list("social_links", flat=True).first() or {}
        )
        if changed_password:
            # Las demás sesiones se cerraron; esta sigue con estos tokens nuevos.
            refresh = RefreshToken.for_user(user)
            data.update(refresh=str(refresh), access=str(refresh.access_token))
        return Response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_social_stats(request, user_id):
    """Contadores sociales de un perfil: seguir y favoritos.

    - followers_count / following_count / is_following: relacion Seguir.
    - favorited_by_count: cuántas personas tienen este perfil en favoritos.
    - favorites_count: a cuántos perfiles tiene en favoritos; solo visible para el
      dueño o si `favorite_profiles_public`; si no, es None.
    - is_favorited: si quien consulta ya lo tiene en favoritos.
    """
    target = get_object_or_404(User, id=user_id)
    is_owner = request.user.id == target.id
    profile = getattr(target, "profile", None)
    favorites_visible = is_owner or bool(getattr(profile, "favorite_profiles_public", False))
    data = {
        "followers_count": ms.Follow.objects.filter(following=target).count(),
        "following_count": ms.Follow.objects.filter(follower=target).count(),
        "is_following": (
            not is_owner
            and ms.Follow.objects.filter(follower=request.user, following=target).exists()
        ),
        "favorited_by_count": ms.pFavorito.objects.filter(perfil=target).count(),
        "favorites_count": (
            ms.pFavorito.objects.filter(user=target).count() if favorites_visible else None
        ),
        "is_favorited": (
            not is_owner
            and ms.pFavorito.objects.filter(user=request.user, perfil=target).exists()
        ),
        # Publicaciones que quien consulta puede ver (sin testimonios ni historias sin guardar).
        "posts_count": exclude_hidden_tasks(
            exclude_testimonials(exclude_unsaved_stories(ms.Task.objects.filter(user=target))),
            request.user if is_owner else None, viewer=request.user,
        ).count(),
        "profile_likes_count": ms.LikeP.objects.filter(profile=target).count(),
        "testimonials_count": (
            ms.CommentTag.objects.filter(user=target, status=ms.CommentTag.STATUS_APPROVED).count()
            + ms.Task.objects.filter(testimonial_for=target, testimonial_status=TESTIMONIAL_APPROVED).count()
        ),
        # None si las mantiene ocultas (y no es su dueño).
        "recommendations_count": (
            ms.UserRecommendation.objects.filter(recommended=target).count()
            if is_owner or getattr(profile, "recommendations_public", True) else None
        ),
        "social_links": getattr(profile, "social_links", None) or {},
        # Ver como esta persona (su feed): si lo permite; su dueño ve su interruptor.
        "can_view_feed": not is_owner and can_view_feed_of(request.user, target),
        "feed_public": bool(getattr(profile, "feed_public", False)) if is_owner else None,
        # Visitas al perfil (últimos 30 días): solo su dueño las ve.
        "visits_30d": visit_counts(target)["visits_30d"] if is_owner else None,
    }
    if request.user.is_superuser or request.user.is_staff:
        # Staff y superadmin ven el estado de la cuenta, para el menu de moderacion del perfil
        # (el staff solo puede verificar; lo demás es del superadmin).
        data["admin_state"] = {
            "is_active": target.is_active,
            "is_staff": target.is_staff,
            "is_superuser": target.is_superuser,
            "is_verified": bool(getattr(profile, "is_verified", False)),
            "is_recommended": bool(getattr(profile, "is_recommended", False)),
        }
    return Response(data)


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
def toggle_user_follow(request, user_id):
    """POST sigue, DELETE deja de seguir (idempotentes).

    Dejar de seguir quita tambien el favorito y el anclaje (ver signals.py)."""
    target = get_object_or_404(User, id=user_id)
    if request.user.id == target.id:
        return Response(
            {"detail": "No puedes seguir tu propia cuenta."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if request.method == "POST":
        ms.Follow.objects.get_or_create(follower=request.user, following=target)
        is_following = True
    else:
        ms.Follow.objects.filter(follower=request.user, following=target).delete()
        is_following = False
    return Response({
        "is_following": is_following,
        "is_favorited": ms.pFavorito.objects.filter(user=request.user, perfil=target).exists(),
        "followers_count": ms.Follow.objects.filter(following=target).count(),
        "following_count": ms.Follow.objects.filter(follower=target).count(),
    })


FOLLOW_LIST_LIMIT = 200
USERS_IN_CATEGORY_LIMIT = 500


def filter_users_by_category(users, category_filter, viewer=None):
    """Perfiles con esa categoría ("Tema,Subtema"): las de un perfil son las de sus
    tareas, así que entra quien tenga una tarea visible con todas esas partes, o
    quien las tenga todas en su filtro personal (CategoryP)."""
    tokens = category_tokens(category_filter)
    if not tokens:
        return users
    tasks = filter_by_categories(
        exclude_hidden_tasks(exclude_unsaved_stories(ms.Task.objects.all()), viewer=viewer),
        ",".join(tokens),
        "categories",
    )
    personal = Q()
    for token in tokens:
        personal &= Q(Exists(ms.CategoryP.objects.filter(user_id=OuterRef("pk"), name__iexact=token)))
    return users.filter(Q(Exists(tasks.filter(user_id=OuterRef("pk")))) | personal)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def users_in_category(request):
    """Cuáles de estos perfiles tienen la categoría `category`. Así cualquier lista de
    perfiles de la app (seguidores, likes, favoritos...) se filtra igual que las tareas.

    POST {category: "Tema,Subtema", user_ids: [...]} -> {user_ids: [...]}"""
    raw_ids = request.data.get("user_ids")
    if not isinstance(raw_ids, list):
        return Response({"user_ids": "Debes enviar una lista de ids."}, status=status.HTTP_400_BAD_REQUEST)
    ids = []
    for value in raw_ids[:USERS_IN_CATEGORY_LIMIT]:
        try:
            ids.append(UUID(str(value)))
        except ValueError:
            continue
    users = filter_users_by_category(
        User.objects.filter(id__in=ids), request.data.get("category"), viewer=request.user,
    )
    return Response({"user_ids": [str(pk) for pk in users.values_list("id", flat=True)]})


def _follow_list(request, user_id, *, followers):
    target = get_object_or_404(User, id=user_id)
    if followers:
        relations = ms.Follow.objects.filter(following=target).select_related("follower__profile")
        users = [relation.follower for relation in relations.order_by("-created_at")[:FOLLOW_LIST_LIMIT]]
    else:
        relations = ms.Follow.objects.filter(follower=target).select_related("following__profile")
        users = [relation.following for relation in relations.order_by("-created_at")[:FOLLOW_LIST_LIMIT]]
    payload = {
        "count": relations.count(),
        "users": SimpleUserSerializer(users, many=True, context={"request": request}).data,
    }
    if followers:
        # Si quien consulta ya lo sigue (aunque no salga entre los primeros de la lista).
        payload["viewer_follows"] = relations.filter(follower=request.user).exists()
    return Response(payload)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_followers(request, user_id):
    return _follow_list(request, user_id, followers=True)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_following(request, user_id):
    return _follow_list(request, user_id, followers=False)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_favorited_by(request, user_id):
    """Quién tiene a este perfil en favoritos.

    El dueño ve a todos. El resto solo ve a quienes mantienen públicos sus
    perfiles favoritos (`favorite_profiles_public`); `count` es el total real y
    `visible_count` cuántos se pueden listar.
    """
    target = get_object_or_404(User, id=user_id)
    is_owner = request.user.id == target.id
    relations = ms.pFavorito.objects.filter(perfil=target).select_related("user__profile")
    total = relations.count()
    if not is_owner:
        relations = relations.filter(user__profile__favorite_profiles_public=True)
    users = [item.user for item in relations.order_by("-created_at")[:FOLLOW_LIST_LIMIT]]
    return Response({
        "count": total,
        "visible_count": total if is_owner else relations.count(),
        "users": SimpleUserSerializer(users, many=True, context={"request": request}).data,
    })


RECOMMENDATIONS_LIST_LIMIT = 50


def _recommendation_payload(item, other_user, request, stats):
    """Fila de la lista. `stats` trae, por persona, el conteo de recomendaciones que
    recibió (None si las ocultó) y si quien consulta ya la recomienda."""
    person = stats.get(other_user.id, {})
    return {
        "id": item.id,
        "user": SimpleUserSerializer(other_user, context={"request": request}).data,
        "comment": item.comment,
        "created_at": item.created_at,
        "recommendations_count": person.get("count"),
        "is_recommended_by_me": person.get("mine", False),
    }


def _people_recommendation_stats(request, user_ids):
    """Para cada usuario: recomendaciones recibidas (None si su perfil las oculta y no
    es quien consulta) y si quien consulta ya lo recomienda. 3 consultas en total."""
    user_ids = set(user_ids)
    if not user_ids:
        return {}
    counts = dict(
        ms.UserRecommendation.objects.filter(recommended_id__in=user_ids)
        .order_by()
        .values_list("recommended_id")
        .annotate(total=Count("id"))
        .values_list("recommended_id", "total")
    )
    public_by_user = dict(
        ms.Profile.objects.filter(user_id__in=user_ids)
        .values_list("user_id", "recommendations_public")
    )
    mine = set(
        ms.UserRecommendation.objects.filter(
            recommender=request.user, recommended_id__in=user_ids
        ).values_list("recommended_id", flat=True)
    )
    stats = {}
    for uid in user_ids:
        visible = uid == request.user.id or public_by_user.get(uid, True)
        stats[uid] = {
            "count": counts.get(uid, 0) if visible else None,
            "mine": uid in mine,
        }
    return stats


def _recommendations_response(request, target):
    is_owner = request.user.id == target.id
    profile = ms.Profile.objects.get_or_create(user=target)[0]
    visible = is_owner or profile.recommendations_public
    mine = (
        None if is_owner
        else ms.UserRecommendation.objects.filter(recommender=request.user, recommended=target).first()
    )

    data = {
        "public": profile.recommendations_public,
        "hidden": not visible,
        "received_count": None,
        "given_count": None,
        "received": [],
        "given": [],
        "is_recommended_by_me": mine is not None,
        "my_comment": mine.comment if mine else "",
    }
    if visible:
        received = ms.UserRecommendation.objects.filter(recommended=target).select_related(
            "recommender__profile"
        ).prefetch_related("recommender__fixed_images", "recommender__likes_received")
        given = ms.UserRecommendation.objects.filter(recommender=target).select_related(
            "recommended__profile"
        ).prefetch_related("recommended__fixed_images", "recommended__likes_received")
        data["received_count"] = received.count()
        data["given_count"] = given.count()
        received_items = list(received[:RECOMMENDATIONS_LIST_LIMIT])
        given_items = list(given[:RECOMMENDATIONS_LIST_LIMIT])
        stats = _people_recommendation_stats(
            request,
            [item.recommender_id for item in received_items]
            + [item.recommended_id for item in given_items],
        )
        data["received"] = [
            _recommendation_payload(item, item.recommender, request, stats)
            for item in received_items
        ]
        data["given"] = [
            _recommendation_payload(item, item.recommended, request, stats)
            for item in given_items
        ]
    return data


@api_view(["GET", "POST", "DELETE", "PATCH"])
@permission_classes([IsAuthenticated])
def user_recommendations(request, user_id):
    """Recomendaciones entre usuarios (independientes de la marca de admin).

    - GET: recomendaciones recibidas y dadas. Si el dueño las ocultó, otros reciben
      `hidden: true` y listas vacías (siempre se informa si yo ya lo recomendé).
    - POST {comment?}: recomienda (o edita el comentario). No se puede a uno mismo.
    - DELETE: quita mi recomendación.
    - PATCH {public}: solo el dueño; muestra u oculta sus recomendaciones.
    """
    target = get_object_or_404(User, id=user_id)

    if request.method == "PATCH":
        if request.user.id != target.id:
            return Response(
                {"detail": "Solo puedes cambiar tu propia privacidad."},
                status=status.HTTP_403_FORBIDDEN,
            )
        value = request.data.get("public")
        if not isinstance(value, bool):
            return Response(
                {"detail": "`public` debe ser verdadero o falso."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        profile = ms.Profile.objects.get_or_create(user=target)[0]
        profile.recommendations_public = value
        profile.save(update_fields=["recommendations_public", "updated_at"])

    elif request.method == "POST":
        if request.user.id == target.id:
            return Response(
                {"detail": "No puedes recomendarte a ti mismo."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        comment = " ".join(str(request.data.get("comment") or "").split())
        if len(comment) > 200:
            return Response(
                {"detail": "El comentario no puede pasar de 200 caracteres."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        _, created = ms.UserRecommendation.objects.update_or_create(
            recommender=request.user,
            recommended=target,
            defaults={"comment": comment},
        )
        return Response(
            _recommendations_response(request, target),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    elif request.method == "DELETE":
        ms.UserRecommendation.objects.filter(recommender=request.user, recommended=target).delete()

    return Response(_recommendations_response(request, target))


# Relaciones que TaskSerializer / SharedTaskSerializer / PosttSerializer recorren
# por cada item. Precargarlas mantiene constante el n?? de queries por p??gina (Fase 36).
_USER_EXTRAS = ("profile", "fixed_images", "likes_received")
# Lo que serializa cada comentario o respuesta: la aportación favorita adjunta
# (aportacion_info: su autor y su portada) y las personas etiquetadas.
_COMMENT_EXTRAS = (
    "aportacion__user",
    "aportacion__subtasks",
    "aportacion__subfactores",
    "aportacion__subfuentes",
    "tags__user",
)
TASK_SERIALIZER_PREFETCH = (
    *(f"user__{rel}" for rel in _USER_EXTRAS),
    # Solo el autor de la tarea trae cuántos lo siguen (TaskAuthorSerializer).
    "user__follower_relationships",
    "likes",
    "reposts",
    *(f"post_comments__created_by__{rel}" for rel in _USER_EXTRAS),
    "post_comments__likes",
    "post_comments__replies__likes",
    "post_comments__replies__replies__replies",
    *(f"post_comments__replies__created_by__{rel}" for rel in _USER_EXTRAS),
    "tagged_users__user",
    "testimonial_for",
    # Historias que comparten un testimonio (testimonial_story).
    "story_source_task__testimonial_for",
    # Citas: dónde la adjuntaron (citations_count).
    "aportacion_comments",
    "forum_aportaciones",
    "shared_comment_aportaciones",
    "podcast_invitations",
    "petition_supports",
    "group",
    "subtasks",
    "subfactores",
    "subfuentes",
    "shared_instances__shared_by__fixed_images",
    *(f"post_comments__{rel}" for rel in _COMMENT_EXTRAS),
    *(f"post_comments__replies__{rel}" for rel in _COMMENT_EXTRAS),
)
SHARED_TASK_PREFETCH = (
    *(f"shared_by__{rel}" for rel in _USER_EXTRAS),
    "likes",
    "comments__replies",
    *(f"task__{rel}" for rel in TASK_SERIALIZER_PREFETCH),
)
# Lo que acompaña un post o respuesta: etiquetados y la publicación guardada (con su portada).
_POST_EXTRAS = (
    "tagged_users",
    "aportacion__user",
    "aportacion__subtasks",
    "aportacion__subfactores",
    "aportacion__subfuentes",
)
POST_PREFETCH = (
    *(f"user__{rel}" for rel in _USER_EXTRAS),
    "likes",
    *_POST_EXTRAS,
    "forum_replies__likes",
    *(f"forum_replies__user__{rel}" for rel in _USER_EXTRAS),
    *(f"forum_replies__{rel}" for rel in _POST_EXTRAS),
    "forum_replies__forum_replies",
    "invited_users",
)


class UserMyTasksView(generics.ListAPIView):
    serializer_class = TaskSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardPagination

    def get_queryset(self):
        queryset = ms.Task.objects.select_related("user").prefetch_related(
            *TASK_SERIALIZER_PREFETCH
        )
        return apply_task_filters(
            queryset,
            self.request.query_params,
            owner_id=self.request.user.id,
            request_user=self.request.user,
        )

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_users(request):
    users = User.objects.all().select_related("profile")
    serializer = SimpleUserSerializer(users, many=True, context={"request": request})
    return Response({
        "users": serializer.data,
        "is_admin": request.user.is_staff or request.user.is_superuser
    })

# ============================================================================
# TAREAS Y PETICIONES
# ============================================================================

class TaskListCreateView(generics.ListCreateAPIView):
    queryset = ms.Task.objects.all()
    serializer_class = TaskSerializer
    # ✅ FIX 401: Permitir que cualquiera vea la lista (GET),
    # pero solo usuarios autenticados puedan crear (POST).
    permission_classes = [IsAuthenticatedOrReadOnly]
    throttle_classes = [ts.BurstThrottle, ts.SustainedThrottle]
    pagination_class = StandardPagination
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        from django.utils import timezone
        from datetime import timedelta
        from django.db.models import Count
        
        qs = super().get_queryset()
        # Keep the public task endpoint on the same filter contract used by
        # the authenticated profile endpoint.
        return apply_task_filters(
            qs.select_related("user"),
            self.request.query_params,
            owner_id=self.request.query_params.get("user_id"),
            request_user=self.request.user,
        ).prefetch_related(*TASK_SERIALIZER_PREFETCH)

        # Legacy filtering code below is intentionally unreachable while
        # retained temporarily for easier comparison during migration.
        pch = self.request.query_params.get("pch")
        user_id = self.request.query_params.get("user_id")
        date_filter = self.request.query_params.get("date_filter")
        category = self.request.query_params.get("category")
        status_filter = self.request.query_params.get("status")
        sort_by = self.request.query_params.get("sort_by")
        raw_search = " ".join(str(self.request.query_params.get("search") or "").split())
        search = normalize_filter_text(raw_search)
        favorites_only = self.request.query_params.get("favorites_only")
        favorite_users_only = self.request.query_params.get("favorite_users_only")
        verified_users_only = self.request.query_params.get("verified_users_only")
        recommended_users_only = self.request.query_params.get("recommended_users_only")
        
        if pch:
            qs = qs.filter(pch=pch)
        if user_id and favorites_only not in ["true", "1", "True", True]:
            qs = qs.filter(user_id=user_id)
            
        if date_filter == "hoy":
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=1))
        elif date_filter == "esta_semana":
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=7))
        elif date_filter == "este_mes":
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=30))
            
        if category:
            qs = filter_by_categories(qs, category, "categories")

        if status_filter:
            qs = filter_by_categories(qs, status_filter, "categories")
            
        if favorites_only in ['true', '1', 'True', True]:
            if user_id:
                qs = qs.filter(favorited_by__user_id=user_id)
            else:
                qs = qs.filter(favorited_by__user=self.request.user)

        if favorite_users_only in ['true', '1', 'True', True]:
            qs = qs.filter(user__profile_favorites_received__user=self.request.user).distinct()
            
        if verified_users_only in ['true', '1', 'True', True]:
            qs = qs.filter(user__profile__is_verified=True)
            
        if recommended_users_only in ['true', '1', 'True', True]:
            qs = qs.filter(user__profile__is_recommended=True)
            
        qs = qs.select_related("user").prefetch_related(
            "likes", "post_comments", "subtasks", "subfuentes", "subfactores", "shared_instances__shared_by"
        )
        
        if sort_by == "likes":
            qs = qs.annotate(like_count=Count('likes')).order_by('-like_count', '-created_at')
        elif like_window_start := _like_window_start(sort_by):
            qs = qs.annotate(
                period_like_count=Count(
                    "likes",
                    filter=Q(likes__created_at__gte=like_window_start),
                    distinct=True,
                )
            ).order_by("-period_like_count", "-created_at")
        else:
            qs = qs.order_by('-created_at')
            
        return qs

    def list(self, request, *args, **kwargs):
        # ── Caché de lista por usuario + parámetros de consulta ──────────────
        # TTL corto (60s) para que siempre se sientan actualizaciones recientes.
        # POST invalida la caché del usuario automáticamente después de crear.
        CACHE_TTL = 60
        cache_key = make_cache_key(
            "tasks_list",
            request.user.id if request.user.is_authenticated else "anon",
            dict(request.query_params),
        )
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        favorite_profile_ids = []
        if request.user.is_authenticated:
            favorite_profile_ids = list(
                ms.pFavorito.objects.filter(user=request.user).values_list("perfil_id", flat=True)
            )

        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page if page is not None else queryset, many=True)
        payload = serializer.data

        if page is not None:
            response_data = self.get_paginated_response(payload).data
            cache.set(cache_key, response_data, CACHE_TTL)
            return Response(response_data)

        cache.set(cache_key, payload, CACHE_TTL)
        return Response(payload)
    def post(self, request, *args, **kwargs):
        
        # 1. Extraemos los datos básicos y creamos la tarea PRINCIPAL
        data = request.data.dict() if hasattr(request.data, 'dict') else request.data
        data['user'] = request.user.id
        
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        task = serializer.save(user=request.user)
        if task.group_id:
            # Del integrante: queda pendiente para los admins. Del admin: aprobada.
            from .community_groups import submit_task
            submit_task(task, request.user)
        if task.testimonial_for_id:
            # Testimonio: la persona del perfil decide si se muestra.
            testimonials.submit_written_testimonial(task)
        sync_series_links(task)

        # 2. PROCESAMIENTO DE ARRAYS (Usando el FOR seguro)
        
        # --- Procesar Subtasks ---
        for index in range(20):
            title = request.data.get(f'subtasks[{index}][title]')
            if title:
                ms.SubTask.objects.create(
                    parent_task=task,
                    title=title,
                    description=request.data.get(f'subtasks[{index}][description]', ''),
                    image=request.FILES.get(f'subtasks[{index}][image]'),
                    video=request.FILES.get(f'subtasks[{index}][video]'),
                    link=request.data.get(f'subtasks[{index}][link]', '')
                )

        # --- Procesar SubFuentes ---
        for index in range(20):
            title = request.data.get(f'subfuentes[{index}][title]')
            if title:
                ms.SubFuentes.objects.create(
                    parent_task=task,
                    title=title,
                    description=request.data.get(f'subfuentes[{index}][description]', ''),
                    image=request.FILES.get(f'subfuentes[{index}][image]'),
                    video=request.FILES.get(f'subfuentes[{index}][video]'),
                    link=request.data.get(f'subfuentes[{index}][link]', '')
                )

        # --- Procesar SubFactores ---
        for index in range(20):
            title = request.data.get(f'subfactores[{index}][title]')
            if title:
                ms.SubFactores.objects.create(
                    parent_task=task,
                    title=title,
                    description=request.data.get(f'subfactores[{index}][description]', ''),
                    image=request.FILES.get(f'subfactores[{index}][image]'),
                    video=request.FILES.get(f'subfactores[{index}][video]'),
                    link=request.data.get(f'subfactores[{index}][link]', '')
                )

        # 3. Devolvemos el objeto completo
        full_serializer = self.get_serializer(task)
        return Response(full_serializer.data, status=status.HTTP_201_CREATED)


class StoryListCreateView(generics.ListCreateAPIView):
    serializer_class = TaskSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    pagination_class = StoriesPagination

    def get_queryset(self):
        stories_window_start = timezone.now() - timedelta(hours=24)
        media_filter = (
            (Q(video__isnull=False) & ~Q(video=""))
            | (Q(image__isnull=False) & ~Q(image=""))
            | (Q(subtasks__video__isnull=False) & ~Q(subtasks__video=""))
            | (Q(subtasks__image__isnull=False) & ~Q(subtasks__image=""))
            | (Q(subfactores__video__isnull=False) & ~Q(subfactores__video=""))
            | (Q(subfactores__image__isnull=False) & ~Q(subfactores__image=""))
            | (Q(subfuentes__video__isnull=False) & ~Q(subfuentes__video=""))
            | (Q(subfuentes__image__isnull=False) & ~Q(subfuentes__image=""))
        )

        # Los testimonios compartidos son solo texto (tarjeta con la cita).
        media_filter |= Q(story_source_task__testimonial_for__isnull=False)
        return (
            ms.Task.objects.filter(
                pch="historias",
                created_at__gte=stories_window_start,
            )
            .filter(media_filter)
            .annotate(
                views_count=Count("story_views", distinct=True),
                viewed_by_me=Exists(ms.StoryView.objects.filter(
                    story=OuterRef("pk"),
                    viewer_id=getattr(self.request.user, "id", None),
                )),
            )
            .select_related("user", "story_source_task__testimonial_for")
            .prefetch_related(*TASK_SERIALIZER_PREFETCH)
            .order_by("-created_at")
            .distinct()
        )

    def create(self, request, *args, **kwargs):
        # ⚡ FIX: QueryDict.copy() hace deepcopy y truena al clonar el archivo subido
        # (TypeError: cannot pickle 'BufferedRandom'). Usamos .dict() como en TaskListCreateView.
        incoming_data = request.data.dict() if hasattr(request.data, 'dict') else dict(request.data)
        caption_value = (incoming_data.get("caption") or "").strip()
        if hasattr(incoming_data, "pop"):
            incoming_data.pop("caption", None)
        incoming_data["pch"] = "historias"
        incoming_data["username"] = request.user.username or ""
        if not incoming_data.get("description"):
            incoming_data["description"] = caption_value
        if not incoming_data.get("title"):
            incoming_data["title"] = "Historia"

        serializer = self.get_serializer(data=incoming_data)
        serializer.is_valid(raise_exception=True)
        story = serializer.save(user=request.user, is_story=True)
        output = self.get_serializer(story)
        return Response(output.data, status=status.HTTP_201_CREATED)
    
    
    
class TaskDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = ms.Task.objects.all()
    serializer_class = TaskSerializer
    permission_classes = [IsAuthenticated]

    def get_serializer_context(self):
        # En el detalle se muestran el anterior y el siguiente recomendado.
        return {**super().get_serializer_context(), "with_series": True}
    throttle_classes = [ts.UploadThrottle, ts.SustainedThrottle]
    lookup_field = "id"

    def get_queryset(self):
        # Precarga todas las relaciones que TaskSerializer accede para eliminar N+1
        queryset = ms.Task.objects.all()
        if not (self.request.user.is_staff or self.request.user.is_superuser):
            queryset = exclude_hidden_tasks(queryset, self.request.user)
        return queryset.select_related(
            "user",
            "user__profile",
        ).prefetch_related(
            "subtasks",
            "subfactores",
            "subfuentes",
            "likes",
            "likes__user",
            "reposts",
            "post_comments",
            "post_comments__created_by",
            "post_comments__likes",
            "shared_instances",
            "shared_instances__shared_by",
            "shared_instances__shared_by__profile",
        )

    def perform_update(self, serializer):
        if serializer.instance.user_id != self.request.user.id and not (
            self.request.user.is_staff or self.request.user.is_superuser
        ):
            raise PermissionDenied("No tienes permiso para editar esta tarea.")
        was_hidden = serializer.instance.is_hidden
        before_next_id = serializer.instance.next_task_id
        before_previous_id = serializer.instance.previous_task_id
        task = serializer.save()
        # Anterior / siguiente: el enlace se refleja en la otra publicación si es suya.
        sync_series_links(task, before_next_id, before_previous_id)
        if was_hidden and not task.is_hidden:
            # Se publicó: ahora sí se avisa a quienes estaban etiquetados.
            notify_task_tags_on_publish(task)
        if task.testimonial_for_id and task.user_id == self.request.user.id:
            # Si quien lo escribió lo cambia, la persona del perfil vuelve a decidir.
            testimonials.submit_written_testimonial(task)
        block_models = {
            "subtasks": ms.SubTask,
            "subfactores": ms.SubFactores,
            "subfuentes": ms.SubFuentes,
        }

        for field_name, block_model in block_models.items():
            has_json_blocks = field_name in self.request.data
            has_multipart_blocks = any(
                key.startswith(f"{field_name}[") for key in self.request.data.keys()
            )
            if not has_json_blocks and not has_multipart_blocks:
                continue

            if has_json_blocks:
                blocks = self.request.data.get(field_name) or []
                if isinstance(blocks, str):
                    try:
                        blocks = json.loads(blocks)
                    except (TypeError, ValueError):
                        raise ValidationError({field_name: "Formato de bloques inválido."})
            else:
                blocks = []
                for index in range(20):
                    prefix = f"{field_name}[{index}]"
                    block = {
                        "id": self.request.data.get(f"{prefix}[id]"),
                        "title": self.request.data.get(f"{prefix}[title]", ""),
                        "description": self.request.data.get(f"{prefix}[description]", ""),
                        "link": self.request.data.get(f"{prefix}[link]"),
                        "image_file": self.request.FILES.get(f"{prefix}[image]"),
                        "video_file": self.request.FILES.get(f"{prefix}[video]"),
                    }
                    if any((block["id"], block["title"], block["description"], block["link"], block["image_file"], block["video_file"])):
                        blocks.append(block)

            kept_ids = set()
            for block_data in blocks:
                block_id = block_data.get("id")
                values = {
                    "title": (block_data.get("title") or "").strip(),
                    "description": block_data.get("description") or "",
                }
                # Solo si se envía: los clientes que no conocen el enlace no lo borran.
                if block_data.get("link") is not None:
                    values["link"] = str(block_data.get("link")).strip()
                image_file = block_data.get("image_file")
                video_file = block_data.get("video_file")
                if block_id:
                    block = get_object_or_404(block_model, id=block_id, parent_task=task)
                    for field, value in values.items():
                        setattr(block, field, value)
                    update_fields = list(values)
                    if image_file:
                        block.image = image_file
                        update_fields.append("image")
                    if video_file:
                        block.video = video_file
                        update_fields.append("video")
                    block.save(update_fields=update_fields)
                    kept_ids.add(block.id)
                elif values["title"] or values["description"] or values.get("link"):
                    create_values = {**values}
                    if image_file:
                        create_values["image"] = image_file
                    if video_file:
                        create_values["video"] = video_file
                    block = block_model.objects.create(parent_task=task, **create_values)
                    kept_ids.add(block.id)

            block_model.objects.filter(parent_task=task).exclude(id__in=kept_ids).delete()

    def perform_destroy(self, instance):
        if instance.user_id != self.request.user.id and not (
            self.request.user.is_staff or self.request.user.is_superuser
        ):
            raise PermissionDenied("No tienes permiso para eliminar esta tarea.")
        instance.delete()

# ============================================================================
# LIKES Y SOCIAL
# ============================================================================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_task_like(request, task_id):
    task = get_object_or_404(visible_tasks_for(request.user), id=task_id)
    like, created = ms.Like.objects.get_or_create(user=request.user, task=task)
    if not created and like.is_hidden:
        # Like de una historia guardada: ya contaba en el total, solo reaparece
        # el nombre en la lista. El conteo no cambia.
        like.is_hidden = False
        like.save(update_fields=["is_hidden"])
        return Response({"liked": True, "likes_count": task.likes.count()}, status=201)
    if not created:
        like.delete()
        return Response({"liked": False, "likes_count": task.likes.count()})
    return Response({"liked": True, "likes_count": task.likes.count()}, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def record_story_view(request, story_id):
    story = get_object_or_404(ms.Task, id=story_id, pch="historias")
    created = False
    if story.user_id != request.user.id:
        _, created = ms.StoryView.objects.get_or_create(
            story=story,
            viewer=request.user,
        )

    return Response({
        "created": created,
        "views_count": story.story_views.count(),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def story_viewers(request, story_id):
    story = get_object_or_404(ms.Task, id=story_id, pch="historias")
    if story.user_id != request.user.id:
        return Response(
            {"detail": "No tienes permiso para ver las visualizaciones de esta historia."},
            status=status.HTTP_403_FORBIDDEN,
        )

    latest_viewer_image = ms.ImagenFija.objects.filter(
        user_id=OuterRef("viewer_id")
    ).order_by("-created_at").values("image")[:1]
    views = list(
        story.story_views.select_related("viewer", "viewer__profile")
        .annotate(activity_image=Subquery(latest_viewer_image))
        .order_by("-viewed_at")
    )

    latest_liker_image = ms.ImagenFija.objects.filter(
        user_id=OuterRef("user_id")
    ).order_by("-created_at").values("image")[:1]
    likes = list(
        story.likes.filter(is_hidden=False).select_related("user", "user__profile")
        .annotate(activity_image=Subquery(latest_liker_image))
        .order_by("-created_at")
    )

    users_by_id = {}

    def activity_user(user, image_value):
        user_id = str(user.id)
        if user_id not in users_by_id:
            full_name = " ".join(
                part for part in (user.first_name, user.last_name) if part
            )
            image = file_to_abs_url(image_value, request)
            users_by_id[user_id] = {
                "id": user_id,
                "username": user.username,
                # Nunca el correo: esta lista la ve el dueño de la historia.
                "name": full_name or user.username or "",
                "image": image,
                "user_image": image,
                "profile": ProfileSerializer(user.profile).data,
                "is_app": user_id == str(app_account_id_for(request)),
                "viewed": False,
                "liked": False,
                "viewed_at": None,
                "liked_at": None,
                "_activity_at": None,
            }
        return users_by_id[user_id]

    for story_view in views:
        viewer = story_view.viewer
        item = activity_user(viewer, story_view.activity_image)
        item["viewed"] = True
        item["viewed_at"] = story_view.viewed_at
        item["_activity_at"] = story_view.viewed_at

    for like in likes:
        item = activity_user(like.user, like.activity_image)
        item["liked"] = True
        item["liked_at"] = like.created_at
        if item["_activity_at"] is None or like.created_at > item["_activity_at"]:
            item["_activity_at"] = like.created_at

    users = sorted(
        users_by_id.values(),
        key=lambda item: (item["_activity_at"], item["id"]),
        reverse=True,
    )
    for item in users:
        item.pop("_activity_at")

    return Response({
        "views_count": len(views),
        "likes_count": story.likes.count(),
        "count": len(users),
        "users": users,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@transaction.atomic
def save_story_to_aportaciones(request, story_id):
    """El dueño acepta guardar su historia (ya terminada) en sus aportaciones.

    Los likes se conservan en el total pero quedan ocultos: cada persona vuelve
    a aparecer en la lista solo cuando le da like otra vez (el conteo no
    cambia)."""
    story = get_object_or_404(
        ms.Task.objects.select_for_update(), id=story_id, is_story=True
    )
    if story.user_id != request.user.id:
        return Response(
            {"detail": "Solo el autor puede guardar esta historia."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if not story.story_saved:
        if story.created_at > timezone.now() - timedelta(hours=24):
            return Response(
                {"detail": "La historia sigue activa. Podrás guardarla cuando termine."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        story.story_saved = True
        story.save(update_fields=["story_saved", "updated_at"])
        story.likes.update(is_hidden=True)
    story = ms.Task.objects.select_related("user").prefetch_related(
        *TASK_SERIALIZER_PREFETCH
    ).get(pk=story.pk)
    return Response(TaskSerializer(story, context={"request": request}).data)



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_comment_like(request, comment_id):
    comment = get_object_or_404(ms.NewPeticionCommentPost, id=comment_id)
    like, created = ms.LikeCommentPost.objects.get_or_create(user=request.user, comment=comment)
    
    # ⚡ Sincronizar: Likear también los posibles clones en las tareas compartidas
    shared_clones = ms.SharedTaskComment.objects.filter(
        shared_task__task=comment.post, created_by=comment.created_by, text=comment.text
    )
    
    if not created:
        like.delete()
        for sc in shared_clones:
            ms.LikeSharedTaskComment.objects.filter(user=request.user, comment=sc).delete()
        return Response({"liked": False, "likes_count": comment.likes.count()})
        
    for sc in shared_clones:
        ms.LikeSharedTaskComment.objects.get_or_create(user=request.user, comment=sc)
        
    return Response({"liked": True, "likes_count": comment.likes.count()}, status=201)

class TaskCommentListCreateView(generics.ListCreateAPIView):
    serializer_class = NewPeticionCommentSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = CommentPagination
    parser_classes = [JSONParser, MultiPartParser, FormParser] # ⚡ Soportar tanto JSON como FormData

    def get_queryset(self):
        task_id = self.kwargs.get("task_id")
        # 🛡️ FIX 500: Usamos "replies" porque así se llama el related_name en models.py
        return ms.NewPeticionCommentPost.objects.filter(
            post_id=task_id, parent__isnull=True
        ).select_related(
            "created_by",
            "created_by__profile",
            "aportacion",
            "aportacion__user",
        ).prefetch_related(
            # Foto y likes del autor (SimpleUserSerializer): sin esto, 2 consultas por comentario
            "created_by__fixed_images",
            "created_by__likes_received",
            *_COMMENT_EXTRAS,
            "replies",
            "replies__created_by",
            *(f"replies__created_by__{rel}" for rel in _USER_EXTRAS),
            *(f"replies__{rel}" for rel in _COMMENT_EXTRAS),
            "replies__likes",
            "replies__replies",
            "replies__replies__created_by",
            *(f"replies__replies__created_by__{rel}" for rel in _USER_EXTRAS),
            *(f"replies__replies__{rel}" for rel in _COMMENT_EXTRAS),
            "replies__replies__likes",
            "likes",
        )

    def perform_create(self, serializer):
        task_id = self.kwargs.get("task_id")
        get_object_or_404(visible_tasks_for(self.request.user), id=task_id)
        original_comment = serializer.save(created_by=self.request.user, post_id=task_id)

        # ⚡ SINCRONIZAR BIDIRECCIONAL: Replicar respuesta en la tarea compartida si el padre es un clon
        if original_comment.parent:
            shared_clones = ms.SharedTaskComment.objects.filter(
                shared_task__task_id=task_id,
                created_by=original_comment.parent.created_by,
                text=original_comment.parent.text
            )
            for sc in shared_clones:
                ms.SharedTaskComment.objects.create(
                    shared_task=sc.shared_task,
                    created_by=self.request.user,
                    text=original_comment.text,
                    parent=sc
                )


class NewPeticionCommentDetailsView(generics.RetrieveUpdateDestroyAPIView):
    queryset = ms.NewPeticionCommentPost.objects.all()
    serializer_class = NewPeticionCommentSerializer
    permission_classes = [IsAuthenticated]
    lookup_url_kwarg = "comment_id" # Django buscará el <int:comment_id> de la URL
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def perform_update(self, serializer):
        # 🛡️ SEGURIDAD: solo el creador del comentario (o un admin) puede editarlo
        if serializer.instance.created_by != self.request.user and not self.request.user.is_staff:
            raise PermissionDenied("No tienes permiso para editar este comentario.")
        serializer.save()

    def perform_destroy(self, instance):
        # 🛡️ SEGURIDAD: Solo el creador del comentario (o un admin) puede borrarlo
        if instance.created_by != self.request.user and not self.request.user.is_staff:
            raise PermissionDenied("No tienes permiso para eliminar este comentario.")
            
        # ⚡ Sincronizar: borrar clones en tareas compartidas si borran de la original
        ms.SharedTaskComment.objects.filter(
            shared_task__task=instance.post,
            created_by=instance.created_by,
            text=instance.text
        ).delete()
        
        instance.delete()
        

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def like_unlike_profile(request, profile_id):
    """
    Da o quita like a un perfil.
    Devuelve el estado final del like y el nuevo contador total.
    """
    # ✅ FIX 500: El ID puede ser de User o de Profile. Buscamos el User directamente.
    try:
        target_user = get_object_or_404(User, Q(id=profile_id) | Q(profile__id=profile_id))
    except (ValueError, User.DoesNotExist):
        return Response({"error": "User or Profile not found"}, status=status.HTTP_404_NOT_FOUND)

    # El modelo LikeP.profile apunta a un User, así que usamos target_user.
    like, created = ms.LikeP.objects.get_or_create(user=request.user, profile=target_user)


    if not created:
        like.delete()
        liked = False
    else:
        liked = True

    # Devolvemos el estado final y el nuevo contador
    likes_count = target_user.likes_received.count()
    return Response({'liked': liked, 'likes_count': likes_count}, status=status.HTTP_200_OK)

# ============================================================================
# REPOST (IMPULSO)
# ============================================================================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
@transaction.atomic
def repost_task(request, task_id):
    """
    Activa o desactiva un 'repost' de una tarea por parte de un usuario.
    Esto solo afecta la puntuación de interacción de la tarea original para el feed,
    sin crear un objeto SharedTask. Funciona como un interruptor (toggle).
    """
    try:
        # Primero se revisa que la pueda ver (eso cruza con su grupo) y luego se bloquea
        # solo su fila: PostgreSQL no deja bloquear un LEFT JOIN ("FOR UPDATE cannot be
        # applied to the nullable side of an outer join").
        if not visible_tasks_for(request.user).filter(id=task_id).exists():
            raise ms.Task.DoesNotExist
        task = ms.Task.objects.select_for_update().get(id=task_id)

        repost_instance, created = ms.Repost.objects.get_or_create(user=request.user, task=task)

        if not created:
            repost_instance.delete()
            task.interaction_score = F('interaction_score') - 1
            reposted = False
        else:
            ensure_visible_like(request.user, task)
            task.interaction_score = F('interaction_score') + 1
            reposted = True

        task.save(update_fields=['interaction_score'])
        task.refresh_from_db(fields=['interaction_score'])

        return Response({
            "reposted": reposted,
            "user_has_reposted": reposted,
            "user_has_liked": ms.Like.objects.filter(user=request.user, task=task, is_hidden=False).exists(),
            "likes_count": task.likes.count(),
            "interaction_score": task.interaction_score,
            "reposts_count": task.reposts.count(),
        }, status=status.HTTP_200_OK)

    except ms.Task.DoesNotExist:
        return Response({"error": "Task not found"}, status=status.HTTP_404_NOT_FOUND)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def share_task_to_story(request):
    source_task_id = request.data.get("task_id")
    caption = (request.data.get("caption") or "").strip()
    source_item_type = (request.data.get("source_item_type") or request.data.get("source_type") or "").strip()
    source_item_id = request.data.get("source_item_id") or request.data.get("source_id")

    if not source_task_id:
        return Response({"error": "task_id is required"}, status=status.HTTP_400_BAD_REQUEST)

    source_task = get_object_or_404(visible_tasks_for(request.user), id=source_task_id)
    is_testimonial = bool(source_task.testimonial_for_id)
    if is_testimonial and source_task.testimonial_status != TESTIMONIAL_APPROVED:
        return Response(
            {"error": "Solo se comparten testimonios que ya se muestran en el perfil."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    source_item = None
    if source_item_type and source_item_id:
        related_name = source_item_type.rstrip("s") if source_item_type.endswith("s") else source_item_type
        normalized_type = {
            "subtask": "subtasks",
            "subtasks": "subtasks",
            "subfactor": "subfactores",
            "subfactores": "subfactores",
            "subfuente": "subfuentes",
            "subfuentes": "subfuentes",
        }.get(str(source_item_type).lower(), str(source_item_type).lower())
        related_manager = {
            "subtasks": source_task.subtasks,
            "subfactores": source_task.subfactores,
            "subfuentes": source_task.subfuentes,
        }.get(normalized_type)
        if related_manager is not None:
            source_item = related_manager.filter(id=source_item_id).first()

    if not source_item:
        for related_name in ["subtasks", "subfactores", "subfuentes"]:
            manager = getattr(source_task, related_name, None)
            if manager is None:
                continue
            candidates = manager.all().order_by("created_at")
            chosen = next((item for item in candidates if item.image or item.video), None)
            if chosen:
                source_item = chosen
                source_item_type = related_name
                break

    story_media = source_item if source_item and (source_item.image or source_item.video) else None
    story_image = story_media.image if story_media and story_media.image else source_task.image
    story_video = story_media.video if story_media and story_media.video else source_task.video

    # Un testimonio es solo texto: en la historia se ve como una tarjeta con la cita.
    if not story_image and not story_video and not is_testimonial:
        return Response(
            {"error": "Solo se puede compartir a historia una tarea o subtarea con imagen o video."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    story_title = source_task.title or (source_item.title if source_item else "Historia compartida")
    story_description = caption if caption else ((source_item.description if source_item and source_item.description else source_task.description) or "")

    story = ms.Task.objects.create(
        user=request.user,
        username=(source_task.user.username if source_task.user else request.user.username) or request.user.username or "",
        title=story_title,
        description=story_description,
        pch="historias",
        categories=source_task.categories or "",
        image=story_image,
        video=story_video,
        story_is_shared=True,
        story_source_task=source_task,
    )

    serializer = TaskSerializer(story, context={"request": request})
    response_data = serializer.data
    response_data['source_task_id'] = str(source_task.id)
    response_data['source_task_title'] = source_task.title or ""
    response_data['source_task_user'] = source_task.user.username if source_task.user else ""
    response_data['source_task_user_id'] = str(source_task.user_id) if source_task.user_id else None
    response_data['source_task_description'] = source_task.description or ""
    response_data['source_task_image'] = file_to_abs_url(source_task.image, request) if source_task.image else None
    response_data['source_task_video'] = file_to_abs_url(source_task.video, request) if source_task.video else None
    response_data['source_item_type'] = source_item_type or None
    response_data['source_item_id'] = str(source_item.id) if source_item else None
    response_data['source_item_title'] = source_item.title if source_item else ""
    response_data['source_item_description'] = source_item.description if source_item else ""
    response_data['source_item_image'] = file_to_abs_url(source_item.image, request) if source_item and source_item.image else None
    response_data['source_item_video'] = file_to_abs_url(source_item.video, request) if source_item and source_item.video else None
    return Response(response_data, status=status.HTTP_201_CREATED)

# ============================================================================
# FUNCIONES DE COMPATIBILIDAD URLS
# ============================================================================

def _mark_app_account(rows, request, app_likers=()):
    """Marca con `is_app` a la cuenta de la App y a quien dio like como la App (`app_likers`):
    la lista los pone en la pestaña App. `is_app_member` (staff o admin) solo pone la etiqueta."""
    app_ids = {str(user_id) for user_id in app_likers}
    app_id = app_account_id_for(request)
    if app_id is not None:
        app_ids.add(str(app_id))
    member_ids = {
        str(user_id) for user_id in User.objects.filter(
            Q(is_staff=True) | Q(is_superuser=True), id__in=[row.get("id") for row in rows if row.get("id")],
        ).values_list("id", flat=True)
    }
    for row in rows:
        row["is_app"] = str(row.get("id")) in app_ids
        row["is_app_member"] = row["is_app"] or str(row.get("id")) in member_ids
    return rows


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def users_who_liked_task(request, task_id):
    """ 
    Obtiene la lista de usuarios a los que les gustó una tarea.
    Esta es la versión optimizada y correcta.
    """
    users_qs = User.objects.filter(
        task_likes__task_id=task_id, task_likes__is_hidden=False
    ).distinct()
    # Subconsulta para obtener la imagen de perfil más reciente
    latest_img = ms.ImagenFija.objects.filter(user=OuterRef("pk")).order_by("-id").values("image")[:1]

    # Subconsulta para verificar si el usuario dio like a esta tarea
    # likes_exists = ms.Like.objects.filter(task_id=task_id, user_id=OuterRef("pk")) # No se usa actualmente

    # Obtenemos los usuarios que dieron like a esta tarea específica
    users_qs = users_qs.annotate(user_image=Subquery(latest_img)).distinct()

    # Construimos la respuesta manualmente para que coincida con lo que el modal espera
    out = []
    for u in users_qs:
        try:
            p = u.profile
        except Exception:
            p = None

        out.append({
            "id": u.id,
            "username": u.username,
            # ✅ FIX DEFINITIVO: El modelo LikeP.profile ahora apunta a User.
            # Usamos el related_name `likes_received` para contar eficientemente.
            # Esto soluciona el error fatal que tumbaba el servidor.
            "likes_count": u.likes_received.count(),
            "user_image": file_to_abs_url(getattr(u, "user_image", None), request),
            "profile": ProfileSerializer(p).data if p else None
        })

    app_likers = ms.Like.objects.filter(task_id=task_id, as_app=True).values_list("user_id", flat=True)
    return Response(_mark_app_account(out, request, app_likers), status=status.HTTP_200_OK)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def users_who_liked_comment(request, comment_id):
    likes = ms.LikeCommentPost.objects.filter(comment_id=comment_id).select_related("user__profile")
    users = [like.user for like in likes if like.user]
    serializer = SimpleUserSerializer(users, many=True, context={"request": request})
    return Response(serializer.data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def users_who_liked_shared_task(request, shared_task_id):
    likes = ms.LikeSharedTask.objects.filter(shared_task_id=shared_task_id).select_related("user__profile")
    users = [like.user for like in likes if like.user]
    serializer = SimpleUserSerializer(users, many=True, context={"request": request})
    return Response(serializer.data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def users_who_liked_shared_comment(request, comment_id):
    likes = ms.LikeSharedTaskComment.objects.filter(comment_id=comment_id).select_related("user__profile")
    users = [like.user for like in likes if like.user]
    serializer = SimpleUserSerializer(users, many=True, context={"request": request})
    return Response(serializer.data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def users_who_shared_task(request, task_id):
    shares = ms.SharedTask.objects.filter(task_id=task_id).select_related("shared_by__profile")
    users = [share.shared_by for share in shares if share.shared_by]
    serializer = SimpleUserSerializer(users, many=True, context={"request": request})
    return Response(serializer.data)

class SharedTaskListCreateView(generics.ListCreateAPIView):
    """Lista tareas compartidas y permite crear nuevas compartidas."""
    queryset = ms.SharedTask.objects.all()
    serializer_class = SharedTaskSerializer
    # ✅ FIX 401: Permitir que cualquiera vea la lista (GET),
    # pero solo usuarios autenticados puedan compartir (POST).
    permission_classes = [IsAuthenticatedOrReadOnly]
    throttle_classes = [ts.BurstThrottle, ts.SustainedThrottle]
    pagination_class = StandardPagination

    def get_queryset(self):
        from django.utils import timezone
        from datetime import timedelta
        from django.db.models import Count
        
        qs = exclude_hidden_tasks(super().get_queryset(), prefix="task__", viewer=self.request.user)
        # Ver como otra persona (?as_user=): lo que comparten o publican quienes sigue.
        view_as = feed_owner(self.request.query_params, self.request.user)
        relation_user = view_as or self.request.user
        if view_as is not None:
            following = followed_ids(view_as)
            qs = qs.filter(Q(shared_by_id__in=following) | Q(task__user_id__in=following))
        pch = self.request.query_params.get("pch")
        date_filter = self.request.query_params.get("date_filter")
        category = self.request.query_params.get("category")
        status_filter = self.request.query_params.get("status")
        sort_by = self.request.query_params.get("sort_by")
        raw_search = " ".join(str(self.request.query_params.get("search") or "").split())
        search = normalize_filter_text(raw_search)
        favorites_only = self.request.query_params.get("favorites_only")
        favorite_users_only = self.request.query_params.get("favorite_users_only")
        verified_users_only = self.request.query_params.get("verified_users_only")
        recommended_users_only = self.request.query_params.get("recommended_users_only")

        if pch:
            qs = qs.filter(task__pch__iexact=pch)

        task_id = self.request.query_params.get("task_id")
        if task_id:
            try:
                qs = qs.filter(task_id=UUID(str(task_id)))
            except (ValueError, AttributeError):
                return qs.none()
        
        if date_filter == "hoy":
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=1))
        elif date_filter == "esta_semana":
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=7))
        elif date_filter == "este_mes":
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=30))
            
        if category:
            qs = filter_by_categories(qs, category, "task__categories")
        if status_filter:
            qs = filter_by_categories(qs, status_filter, "task__categories")
        hashtag = mentions.hashtag_in_search(raw_search)
        if hashtag:
            qs = qs.filter(mentions.hashtag_q(hashtag, "task__title", "task__description", "description"))
        elif search:
            qs = qs.filter(
                Q(task__title__icontains=raw_search)
                | Q(task__description__icontains=raw_search)
                | Q(task__categories__icontains=raw_search)
                | Q(task__title__icontains=search)
                | Q(task__description__icontains=search)
                | Q(task__categories__icontains=search)
            )
            
        if favorites_only in ['true', '1', 'True', True]:
            qs = qs.filter(task__favorited_by__user=self.request.user)

        if favorite_users_only in ['true', '1', 'True', True]:
            qs = qs.filter(Q(shared_by__profile_favorites_received__user=relation_user) | Q(task__user__profile_favorites_received__user=relation_user)).distinct()

        if self.request.query_params.get("followed_users_only") in ['true', '1', 'True', True]:
            qs = qs.filter(
                Q(shared_by__follower_relationships__follower=relation_user)
                | Q(task__user__follower_relationships__follower=relation_user)
            ).distinct()

        if verified_users_only in ['true', '1', 'True', True]:
            qs = qs.filter(shared_by__profile__is_verified=True) | qs.filter(task__user__profile__is_verified=True)

        if recommended_users_only in ['true', '1', 'True', True]:
            qs = qs.filter(shared_by__profile__is_recommended=True) | qs.filter(task__user__profile__is_recommended=True)

        if self.request.query_params.get("app_liked_only") in ['true', '1', 'True', True]:
            qs = qs.filter(Exists(
                ms.Like.objects.filter(task=OuterRef("task_id")).filter(app_like_requests.app_like_q())
            ))

        if self.request.query_params.get("admin_users_only") in ['true', '1', 'True', True]:
            qs = qs.filter(
                Q(shared_by__is_staff=True) | Q(shared_by__is_superuser=True)
                | Q(task__user__is_staff=True) | Q(task__user__is_superuser=True)
            )

        qs = qs.select_related(
            "task",
            "task__user",
            "task__user__profile",
            "shared_by",
            "shared_by__profile",
        ).prefetch_related(
            *SHARED_TASK_PREFETCH,
            "likes__user",
            "comments__created_by__profile",
        )

        if sort_by == "likes":
            qs = qs.annotate(like_count=Count('likes')).order_by('-like_count', '-created_at')
        elif like_window_start := _like_window_start(sort_by):
            qs = qs.annotate(
                period_like_count=Count(
                    "task__likes",
                    filter=Q(task__likes__created_at__gte=like_window_start),
                    distinct=True,
                )
            ).order_by("-period_like_count", "-created_at")
        else:
            qs = qs.order_by('-created_at')
            
        return qs

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        task_id = request.data.get("task_id")
        if not task_id:
            return Response({"error": "task_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        

        # Bloqueamos la tarea para actualizarla de forma segura
        task = get_object_or_404(ms.Task.objects.select_for_update(), id=task_id)
        if task.is_hidden:
            return Response(
                {"detail": "Esta aportación está oculta. Publícala antes de compartirla."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Cada compartida es independiente: un usuario puede compartir la misma tarea
        # cuantas veces quiera, cada una con su propia descripción.
        shared = ms.SharedTask.objects.create(
            task=task,
            shared_by=request.user,
            description=request.data.get("description", ""),
        )

        has_interacted_before = ms.Like.objects.filter(user=request.user, task=task).exists()

        update_fields = {'share_count': F('share_count') + 1}
        if not has_interacted_before:
            update_fields['interaction_score'] = F('interaction_score') + 1
            interaction, interaction_created = ms.Like.objects.get_or_create(
                user=request.user,
                task=task,
            )
            if interaction_created:
                retire_notification(dedupe_key=f"api.like:{interaction.pk}")
        
        ms.Task.objects.filter(pk=task.pk).update(**update_fields)

        # Recargamos la tarea desde la DB para obtener los contadores actualizados.
        task.refresh_from_db()

        serializer = self.get_serializer(shared)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def agregar_favorito(request):
    task = get_object_or_404(visible_tasks_for(request.user), id=request.data.get("task_id"))
    fav, created = ms.Favorito.objects.get_or_create(user=request.user, task=task)
    if not created:
        fav.delete()
        return Response({"mensaje": "Removed"}, status=204)
    return Response({"mensaje": "Added"}, status=201)


def _favorite_position(model, user):
    return (
        model.objects.filter(user=user)
        .order_by("-position", "-created_at")
        .values_list("position", flat=True)
        .first()
        or 0
    ) + 1


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def favorite_collection(request, user_id=None):
    owner = get_object_or_404(User, id=user_id) if user_id else request.user
    is_owner = owner.pk == request.user.pk
    profile = ms.Profile.objects.get_or_create(user=owner)[0]
    if not is_owner and not (
        profile.favorite_profiles_public or profile.favorite_tasks_public
    ):
        return Response({"profiles": [], "tasks": [], "visibility": {
            "profiles": False, "tasks": False,
        }})

    if request.method == "PATCH":
        if not is_owner:
            raise PermissionDenied("Solo el propietario puede ordenar favoritos.")
        kind = request.data.get("type")
        order = request.data.get("order")
        if kind not in {"profiles", "tasks"} or not isinstance(order, list):
            raise ValidationError({"order": "Envía una lista válida y type profiles/tasks."})
        model = ms.pFavorito if kind == "profiles" else ms.Favorito
        favorites = {
            str(item.id): item
            for item in model.objects.filter(user=owner).select_related(
                "perfil" if kind == "profiles" else "task__user"
            )
        }
        ordered_ids = [str(item_id) for item_id in order if str(item_id) in favorites]
        if len(ordered_ids) != len(favorites) or len(set(ordered_ids)) != len(ordered_ids):
            raise ValidationError({"order": "La lista debe contener todos los favoritos una sola vez."})

        newly_pinned = []
        with transaction.atomic():
            locked = {
                str(item.id): item
                for item in model.objects.select_for_update().filter(user=owner)
            }
            for position, item_id in enumerate(ordered_ids):
                item = locked[item_id]
                was_pinned = item.is_pinned
                item.position = position
                item.is_pinned = position < 5
                item.save(update_fields=["position", "is_pinned"])
                if item.is_pinned and not was_pinned:
                    newly_pinned.append(item)

        for item in newly_pinned:
            target = item.perfil if kind == "profiles" else item.task
            recipient = target if kind == "profiles" else target.user
            create_notification(
                recipient=recipient,
                actor=request.user,
                notification_type="favorite_pin",
                target_type="profile" if kind == "profiles" else "task",
                target_id=target.pk,
                data={"text": f"{request.user.username or 'Alguien'} te colocó entre sus 5 favoritos."},
            )
        return Response({"updated": True})

    profiles = ms.pFavorito.objects.filter(user=owner).select_related("perfil")
    tasks = exclude_hidden_tasks(
        ms.Favorito.objects.filter(user=owner), request.user, prefix="task__",
    ).select_related("task").prefetch_related(
        *(f"task__{rel}" for rel in TASK_SERIALIZER_PREFETCH)
    )
    # Guardadas que no son favoritas (las favoritas ya van en `tasks`).
    saved = exclude_hidden_tasks(
        ms.TaskSave.objects.filter(user=owner).exclude(task__favorited_by__user=owner),
        request.user, prefix="task__",
    ).select_related("task").prefetch_related(
        *(f"task__{rel}" for rel in TASK_SERIALIZER_PREFETCH)
    )
    private_ids = task_saves.private_task_ids(owner)
    if not is_owner:
        if not profile.favorite_profiles_public:
            profiles = profiles.none()
        # Las "solo para mí" nunca las ven los demás.
        tasks = tasks.exclude(task_id__in=private_ids)
        saved = saved.filter(is_private=False)
        if not profile.favorite_tasks_public:
            tasks = tasks.none()
            saved = saved.none()
    tasks = list(tasks.order_by("-is_pinned", "position", "created_at"))
    pinned_categories = {}
    for favorite_id, category in ms.TaskCategoryPin.objects.filter(
        favorite_id__in=[item.id for item in tasks]
    ).order_by("category").values_list("favorite_id", "category"):
        pinned_categories.setdefault(favorite_id, []).append(category)
    visible_favorites = {item.id for item in tasks}
    category_pins = {
        name: [str(favorite.id) for favorite in top if favorite.id in visible_favorites]
        for name, top in task_saves.category_tops(owner).items()
    }
    return Response({
        "profiles": [
            {
                "id": str(item.id),
                "profile": SimpleUserSerializer(item.perfil, context={"request": request}).data,
                "position": item.position,
                "is_pinned": item.is_pinned,
            }
            for item in profiles.order_by("-is_pinned", "position", "created_at")
        ],
        "tasks": [
            {
                "id": str(item.id),
                "task": TaskSerializer(item.task, context={"request": request}).data,
                "position": item.position,
                "is_pinned": item.is_pinned,
                "is_private": item.task_id in private_ids,
                "categories": task_saves.task_categories(item.task),
                "pinned_categories": pinned_categories.get(item.id, []),
            }
            for item in tasks
        ],
        "saved": [
            {
                "id": str(item.id),
                "task": TaskSerializer(item.task, context={"request": request}).data,
                "is_private": item.is_private,
                "saved_at": item.created_at,
            }
            for item in saved.order_by("-created_at")
        ],
        # Top 5 por categoría: {categoría: [id de favorito, ...]} en orden.
        "category_pins": {name: ids for name, ids in category_pins.items() if ids},
        "visibility": {
            "profiles": profile.favorite_profiles_public if is_owner else bool(profile.favorite_profiles_public),
            "tasks": profile.favorite_tasks_public if is_owner else bool(profile.favorite_tasks_public),
        },
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_relations(request):
    """
    Relaciones del usuario autenticado con otros perfiles, en una sola respuesta.
    Sirve para pintar el corazon de seguir junto a cualquier nombre de la app.
    """
    following = [
        str(pk)
        for pk in ms.Follow.objects.filter(follower=request.user).values_list("following_id", flat=True)
    ]
    favorites = []
    pinned = []
    for perfil_id, is_pinned in ms.pFavorito.objects.filter(user=request.user).values_list(
        "perfil_id", "is_pinned"
    ):
        favorites.append(str(perfil_id))
        if is_pinned:
            pinned.append(str(perfil_id))
    # Tareas: Guardar -> Favorito -> Top 5 (y top 5 por categoría), para el menú de 3 puntos.
    saved_tasks, private_tasks = [], []
    for task_id, is_private in ms.TaskSave.objects.filter(user=request.user).values_list("task_id", "is_private"):
        saved_tasks.append(str(task_id))
        if is_private:
            private_tasks.append(str(task_id))
    favorite_tasks, pinned_tasks = [], []
    for task_id, is_pinned in ms.Favorito.objects.filter(user=request.user).values_list("task_id", "is_pinned"):
        favorite_tasks.append(str(task_id))
        if is_pinned:
            pinned_tasks.append(str(task_id))
    task_category_pins = {}
    for task_id, category in ms.TaskCategoryPin.objects.filter(favorite__user=request.user).values_list(
        "favorite__task_id", "category"
    ):
        task_category_pins.setdefault(str(task_id), []).append(category)
    return Response({
        "me": str(request.user.id),
        "following": following,
        "favorites": favorites,
        "pinned": pinned,
        "saved_tasks": saved_tasks,
        "private_tasks": private_tasks,
        "favorite_tasks": favorite_tasks,
        "pinned_tasks": pinned_tasks,
        "task_category_pins": task_category_pins,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_favorite_pin(request):
    kind = request.data.get("type")
    item_id = request.data.get("id")
    target_id = request.data.get("target_id")
    should_pin = bool(request.data.get("is_pinned", True))
    # Anclar implica favorito: si todavia no lo es, se agrega primero.
    auto_add = should_pin and not item_id
    if kind == "profile":
        if auto_add:
            target_user = get_object_or_404(User, id=target_id)
            favorite, _ = ms.pFavorito.objects.get_or_create(
                user=request.user, perfil=target_user
            )
        else:
            favorite = get_object_or_404(
                ms.pFavorito,
                **({"id": item_id} if item_id else {"perfil_id": target_id}),
                user=request.user,
            )
    elif kind == "task":
        if auto_add:
            target_task = get_object_or_404(visible_tasks_for(request.user), id=target_id)
            favorite, _ = ms.Favorito.objects.get_or_create(
                user=request.user, task=target_task
            )
        else:
            favorite = get_object_or_404(
                ms.Favorito,
                **({"id": item_id} if item_id else {"task_id": target_id}),
                user=request.user,
            )
    else:
        raise ValidationError({"type": "Debe ser profile o task."})

    model = favorite.__class__
    with transaction.atomic():
        ordered = list(model.objects.select_for_update().filter(user=request.user).order_by(
            "-is_pinned", "position", "created_at", "pk"
        ))
        ordered = [item for item in ordered if item.pk != favorite.pk]
        if should_pin:
            pinned = [item for item in ordered if item.is_pinned][:4]
            unpinned = [item for item in ordered if item not in pinned]
            favorite.is_pinned = True
            ordered = pinned + [favorite] + unpinned
        else:
            # Sale del top 5 y queda en el puesto 6: el que estaba ahí sube a ocupar su lugar.
            favorite.is_pinned = False
            ordered.insert(min(5, len(ordered)), favorite)
        for position, item in enumerate(ordered):
            item.position = position
            # Aunque haya 5 favoritos o menos, el que se acaba de desanclar no vuelve a anclarse.
            item.is_pinned = position < 5 and (should_pin or item.pk != favorite.pk)
            item.save(update_fields=["is_pinned", "position"])
    if should_pin:
        target = favorite.perfil if kind == "profile" else favorite.task
        recipient = target if kind == "profile" else target.user
        create_notification(
            recipient=recipient,
            actor=request.user,
            notification_type="favorite_pin",
            target_type=kind,
            target_id=target.pk,
            data={"text": f"{request.user.username or 'Alguien'} te colocó entre sus 5 favoritos."},
        )
    favorite.refresh_from_db()
    return Response({"id": str(favorite.id), "is_pinned": favorite.is_pinned, "position": favorite.position})


SUGGESTED_PROFILES_LIMIT = 30


def suggested_profiles(user):
    """A quién sugerir antes de buscar: a quien sigues y quien te sigue (lo más reciente
    primero) y, si son pocos, otras cuentas recientes."""
    ids = []
    for pk in chain(
        ms.Follow.objects.filter(follower=user).order_by("-created_at").values_list("following_id", flat=True)[:SUGGESTED_PROFILES_LIMIT],
        ms.Follow.objects.filter(following=user).order_by("-created_at").values_list("follower_id", flat=True)[:SUGGESTED_PROFILES_LIMIT],
    ):
        if pk != user.pk and pk not in ids:
            ids.append(pk)
    ids = ids[:SUGGESTED_PROFILES_LIMIT]
    if len(ids) < SUGGESTED_PROFILES_LIMIT:
        ids += list(
            User.objects.filter(is_active=True).exclude(pk__in=[user.pk, *ids])
            .order_by("-date_joined").values_list("pk", flat=True)[: SUGGESTED_PROFILES_LIMIT - len(ids)]
        )
    by_id = {u.pk: u for u in User.objects.filter(pk__in=ids, is_active=True).select_related("profile")}
    return [by_id[pk] for pk in ids if pk in by_id]


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def search_directory(request):
    query = " ".join(str(request.query_params.get("q", "")).split())
    scope = request.query_params.get("scope", "both")
    if not query:
        # Sin texto: sugerencias (perfiles al etiquetar; tus publicaciones recientes al
        # elegir el "siguiente recomendado").
        profiles = suggested_profiles(request.user) if scope == "profiles" else []
        tasks = exclude_hidden_tasks(
            exclude_testimonials(exclude_unsaved_stories(ms.Task.objects.filter(user=request.user))),
            request.user, viewer=request.user,
        ).select_related("user").prefetch_related(*TASK_SERIALIZER_PREFETCH).order_by("-created_at")[:20] if scope == "tasks" else []
        return Response({
            "profiles": SimpleUserSerializer(profiles, many=True, context={"request": request}).data,
            "tasks": TaskSerializer(tasks, many=True, context={"request": request}).data,
        })
    # Con la categoría elegida en el buscador, perfiles y tareas se filtran igual.
    category = request.query_params.get("category")
    profiles = filter_users_by_category(User.objects.filter(
        Q(username__icontains=query)
        | Q(first_name__icontains=query)
        | Q(last_name__icontains=query)
    ).exclude(pk=request.user.pk), category, viewer=request.user)[:20] if scope in {"profiles", "both"} else []
    tasks = filter_by_categories(exclude_hidden_tasks(exclude_testimonials(exclude_unsaved_stories(ms.Task.objects.filter(
        Q(title__icontains=query) | Q(description__icontains=query)
    ))), viewer=request.user), category, "categories").select_related("user").prefetch_related(
        *TASK_SERIALIZER_PREFETCH
    ).order_by("-created_at")[:20] if scope in {"tasks", "both"} else []
    return Response({
        "profiles": SimpleUserSerializer(profiles, many=True, context={"request": request}).data,
        "tasks": TaskSerializer(tasks, many=True, context={"request": request}).data,
    })

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def listar_favoritos(request, user_id=None):
    user = get_object_or_404(User, id=user_id) if user_id else request.user
    favoritos = ms.Favorito.objects.filter(user=user).values_list('task_id', flat=True)
    return Response(list(favoritos))

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def agregar_pfavorito(request):
    # En la base viva, `api_pfavorito.perfil_id` apunta a `api_user.id`, no a `api_profile.id`.
    perfil = get_object_or_404(User, id=request.data.get("perfil_id"))
    fav, created = ms.pFavorito.objects.get_or_create(user=request.user, perfil=perfil)
    if not created:
        fav.delete()
        return Response({"mensaje": "Removed"}, status=204)
    return Response({"mensaje": "Added"}, status=201)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def listar_pfavoritos(request, user_id=None):
    user = get_object_or_404(User, id=user_id) if user_id else request.user
    favoritos = ms.pFavorito.objects.filter(user=user).select_related("perfil__profile")
    perfiles = [f.perfil for f in favoritos]
    serializer = SimpleUserSerializer(perfiles, many=True, context={"request": request})
    return Response(serializer.data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_likes(request, profile_id):
    """
    Lista todos los usuarios que han dado like a un perfil específico.
    Incluye un campo 'viewer_has_liked' para el usuario que hace la petición.
    """
    try:
        # Buscamos el perfil a través del ID de usuario (UUID) o el ID de perfil (numérico)
        profile = get_object_or_404(ms.Profile, Q(user_id=profile_id) | Q(id=profile_id))
    except (ValueError, ms.Profile.DoesNotExist):
        return Response({"error": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)

    # ✅ FIX 500: La relación `likes_received` está en el modelo User, no en Profile.
    # El campo `profile` en el modelo `LikeP` apunta a un `User`.
    # Por lo tanto, para obtener los usuarios que dieron "like", filtramos
    # los `LikeP` donde el `profile` (que es un User) es `profile.user`.
    likes = ms.LikeP.objects.filter(profile=profile.user).select_related('user')
    users = [like.user for like in likes]
    serializer = SimpleUserSerializer(users, many=True, context={'request': request})

    viewer_has_liked = False
    if request.user.is_authenticated:
        # Usamos `profile.user` para que coincida con el modelo LikeP.
        viewer_has_liked = ms.LikeP.objects.filter(user=request.user, profile=profile.user).exists()

    return Response({
        'results': _mark_app_account(serializer.data, request),
        'viewer_has_liked': viewer_has_liked,
    })

@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def create_categoryp(request, pk=None):
    if request.method == "GET":
        cats = ms.CategoryP.objects.filter(user=request.user)
        return Response(CategoryPSerializer(cats, many=True).data)
    elif request.method == "POST":
        serializer = CategoryPSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=201)
        return Response(serializer.errors, status=400)
    elif request.method == "DELETE":
        cat = get_object_or_404(ms.CategoryP, pk=pk, user=request.user)
        cat.delete()
        return Response(status=204)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def personal_filter_visibility(request):
    profile, _ = ms.Profile.objects.get_or_create(user=request.user)
    if request.method == "PATCH":
        serializer = PersonalFilterVisibilitySerializer(
            profile, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
    return Response(PersonalFilterVisibilitySerializer(profile).data)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def reorder_categoryp(request):
    """Persiste el nuevo orden del filtro personal del usuario (arrastrar y soltar,
    igual que hacen los admins con las categorías globales del filtro)."""
    order = request.data.get("order")
    if not isinstance(order, list) or not order:
        return Response({"order": "Debes enviar una lista de ids en el nuevo orden."}, status=400)

    owned_ids = set(str(pk) for pk in ms.CategoryP.objects.filter(user=request.user).values_list("id", flat=True))
    for position, cat_id in enumerate(order):
        if str(cat_id) not in owned_ids:
            continue
        ms.CategoryP.objects.filter(pk=cat_id, user=request.user).update(position=position)

    cats = ms.CategoryP.objects.filter(user=request.user)
    return Response(CategoryPSerializer(cats, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_categoryp_for_user(request, user_id):
    """Filtro personal de un perfil: siempre visible en su propio perfil."""
    cats = ms.CategoryP.objects.filter(user_id=user_id)
    return Response(CategoryPSerializer(cats, many=True).data)


def _ensure_personal_categories(user_id):
    """Backfill: guarda en CategoryP las categorías principales de tareas ya existentes."""
    used = {}
    for categories in ms.Task.objects.filter(user_id=user_id).values_list("categories", flat=True):
        for raw in (categories or "").split(","):
            name = raw.strip()
            key = name.casefold()
            if not name or key in ms.SUBTHEME_NAMES or key in ms.STATUS_TAGS:
                continue
            used.setdefault(key, name)

    if not used:
        return

    existing = list(ms.CategoryP.objects.filter(user_id=user_id))
    existing_names = {item.name.casefold() for item in existing}
    next_position = max((item.position for item in existing), default=-1) + 1

    for key, name in used.items():
        if key in existing_names:
            continue
        ms.CategoryP.objects.create(user_id=user_id, name=name, position=next_position)
        next_position += 1


# ============================================================================
# FILTROS GUARDADOS POR PERFIL (CRUD propio, un solo campo JSON)
# ============================================================================

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def saved_filters_list_create(request):
    """Lista o crea los filtros guardados del perfil autenticado.
    No toca el feed de tareas: se consulta aparte, sin renderizar tareas."""
    if request.method == "GET":
        filters_qs = ms.UserSavedFilter.objects.filter(user=request.user)
        return Response(UserSavedFilterSerializer(filters_qs, many=True).data)

    serializer = UserSavedFilterSerializer(data=request.data, context={"request": request})
    if serializer.is_valid():
        try:
            serializer.save()
        except IntegrityError:
            return Response(
                {"name": "Ya tienes un filtro guardado con ese nombre."}, status=400
            )
        return Response(serializer.data, status=201)
    return Response(serializer.errors, status=400)


@api_view(["PUT", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def saved_filter_detail(request, pk):
    saved_filter = get_object_or_404(ms.UserSavedFilter, pk=pk, user=request.user)

    if request.method == "DELETE":
        saved_filter.delete()
        return Response(status=204)

    partial = request.method == "PATCH"
    serializer = UserSavedFilterSerializer(
        saved_filter, data=request.data, partial=partial, context={"request": request}
    )
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)
    return Response(serializer.errors, status=400)

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticatedOrReadOnly])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def new_category_list_create(request):
    if request.method == "GET":
        pch = (request.query_params.get("pch") or "").strip()
        qs = ms.NewCategory.objects.all()
        if pch:
            # Globales (pch vacío) + las del tema solicitado
            qs = qs.filter(Q(pch="") | Q(pch__iexact=pch))
        profile_user_id = request.query_params.get("profile_user_id")
        if profile_user_id:
            # El cruce usa el filtro ya guardado del perfil, no todas sus tareas.
            saved = {
                name.casefold()
                for name in ms.CategoryP.objects.filter(user_id=profile_user_id).values_list("name", flat=True)
            }
            matched = [category for category in qs if category.name.casefold() in saved]
            # Arrastramos también los subtemas de cada categoría coincidente.
            result = list(matched)
            seen_ids = {category.id for category in result}
            pending = list(matched)
            while pending:
                children = ms.NewCategory.objects.filter(parent__in=pending)
                pending = [child for child in children if child.id not in seen_ids]
                for child in pending:
                    seen_ids.add(child.id)
                    result.append(child)
            qs = result
        include_approval = request.query_params.get("include_approval") in ["true", "1", "True"]
        if not include_approval and not request.user.is_staff and not request.user.is_superuser:
            qs = qs.exclude(name__iexact=ms.APPROVED_TAG)
        serializer = NewCategorySerializer(qs, many=True, context={"request": request})
        return Response(serializer.data)
    if not (request.user.is_staff or request.user.is_superuser): return Response(status=403)
    serializer = NewCategorySerializer(data=request.data, context={"request": request})
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data, status=201)
    return Response(serializer.errors, status=400)

@api_view(["GET", "PUT", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def new_category_detail(request, pk):
    cat = ms.NewCategory.objects.filter(pk=pk).first()
    if cat is None:
        return Response(
            {"detail": f"La categoría {pk} no existe en esta base de datos."},
            status=404,
        )
    if request.method == "GET": return Response(NewCategorySerializer(cat, context={"request": request}).data)
    if not (request.user.is_staff or request.user.is_superuser): return Response(status=403)
    if request.method in ("PUT", "PATCH"):
        # PATCH: solo lo que viene (p. ej. la imagen o la descripción).
        serializer = NewCategorySerializer(
            cat, data=request.data, partial=request.method == "PATCH", context={"request": request},
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=400)
    cat.delete()
    return Response(status=204)

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def imagen_fija_list_create(request):
    if request.method == "GET":
        imagen = ms.ImagenFija.objects.filter(user=request.user).last()
        if not imagen: return Response(status=404)
        return Response(ImagenFijaSerializer(imagen).data)
    serializer = ImagenFijaSerializer(data=request.data, context={"request": request})
    if serializer.is_valid():
        serializer.save(user=request.user)
        return Response(serializer.data, status=201)
    return Response(serializer.errors, status=400)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def obtener_portadas_usuario(request, user_id):
    portadas = ms.Portada.objects.filter(user_id=user_id)
    return Response(PortadaSerializer(portadas, many=True, context={'request': request}).data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def obtener_imagen_fija_usuario(request, user_id):
    imagen = ms.ImagenFija.objects.filter(user_id=user_id).last()
    if not imagen: return Response(status=404)
    return Response(ImagenFijaSerializer(imagen).data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_user_details(request):
    # ✅ DEBUG: Imprime los datos del usuario en la terminal del backend
    # 1. Obtenemos los datos base del serializador
    user_data = UserSerializer(request.user, context={'request': request}).data
    # 2. Añadimos los campos de admin
    user_data['is_staff'] = request.user.is_staff
    user_data['is_superuser'] = request.user.is_superuser
    
    return Response(user_data) # 3. Enviamos la respuesta completa

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_current_user_profile(request):
    return Response(UserSerializer(request.user, context={'request': request}).data)

class PortadaListCreateView(generics.ListCreateAPIView):
    serializer_class = PortadaSerializer
    permission_classes = [IsAuthenticated]
    def get_queryset(self): return ms.Portada.objects.filter(user=self.request.user)
    def perform_create(self, serializer): serializer.save(user=self.request.user)

@api_view(["PUT", "DELETE"])
@permission_classes([IsAuthenticated])
def portada_update_delete(request, portada_id):
    portada = get_object_or_404(ms.Portada, id=portada_id, user=request.user)
    if request.method == "DELETE":
        portada.delete()
        return Response(status=204)
    serializer = PortadaSerializer(portada, data=request.data, partial=True)
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)
    return Response(serializer.errors, status=400)


class FeedItem:
    """
    Clase proxy para unificar Task y SharedTask en un solo tipo de objeto para el feed.
    Esto simplifica la serialización y el ordenamiento.
    """
    def __init__(self, item, priority, is_original):
        self.item = item
        self.priority = priority
        self.is_original = is_original
        self.created_at = item.created_at

class FeedView(generics.ListAPIView):
    """
    Vista unificada que combina Tasks y SharedTasks en un solo feed,
    ordenado por prioridad de usuario y luego por fecha.
    """
    # ✅ FIX: No usamos un serializer_class único, ya que manejamos dos tipos de objetos.
    # La serialización se hará manualmente en get_queryset.
    serializer_class = FeedItemSerializer
    permission_classes = [IsAuthenticatedOrReadOnly] # Cualquiera puede ver el feed
    pagination_class = StandardPagination

    def get_queryset(self):
        # ✅ LÓGICA DE FILTRO: Leemos el parámetro 'sort_by' para decidir qué feed mostrar.
        # El frontend enviará 'all' para el feed unificado.
        sort_by = self.request.query_params.get("sort_by")

        # Prioridad: admin > recomendado > verificado > regular
        # 1. Definir las prioridades para el ordenamiento
        user_priority = Case(
            When(Q(user__is_staff=True) | Q(user__is_superuser=True), then=Value(4)),
            When(user__profile__is_recommended=True, then=Value(3)),
            When(user__profile__is_verified=True, then=Value(2)),
            default=Value(1),
            output_field=IntegerField(),
        )
        shared_by_priority = Case(
            When(Q(shared_by__is_staff=True) | Q(shared_by__is_superuser=True), then=Value(4)),
            When(shared_by__profile__is_recommended=True, then=Value(3)),
            When(shared_by__profile__is_verified=True, then=Value(2)),
            default=Value(1),
            output_field=IntegerField(),
        )

        # ✅ COMPORTAMIENTO POR DEFECTO: Mostrar solo tareas originales.
        if sort_by != 'all':
            tasks = exclude_hidden_tasks(
                exclude_testimonials(exclude_unsaved_stories(ms.Task.objects.all())), viewer=self.request.user,
            ).annotate(
                priority=user_priority
            ).select_related('user__profile').prefetch_related(*TASK_SERIALIZER_PREFETCH)

        # ✅ COMPORTAMIENTO PARA "all": Mostrar el feed unificado.
        else:
            tasks = exclude_hidden_tasks(
                exclude_testimonials(exclude_unsaved_stories(ms.Task.objects.all())), viewer=self.request.user,
            ).annotate(priority=user_priority).select_related('user__profile')
            shared_tasks = exclude_hidden_tasks(
                ms.SharedTask.objects.all(), prefix="task__", viewer=self.request.user,
            ).annotate(priority=shared_by_priority).select_related('shared_by__profile', 'task__user__profile')

            task_items = [
                FeedItem(item=task, priority=task.priority, is_original=True)
                for task in tasks
            ]
            shared_task_items = [
                FeedItem(item=shared_task, priority=shared_task.priority, is_original=False)
                for shared_task in shared_tasks
            ]

            combined_list_unord = task_items + shared_task_items
            combined_list = sorted(
                combined_list_unord,
                key=lambda x: (x.priority, x.created_at),
                reverse=True
            )
            return combined_list
            
        # Aplicar ordenamiento de likes si se solicita
        if sort_by == 'likes':
            tasks = tasks.annotate(like_count=Count('likes')).order_by('-like_count', '-created_at')
        elif like_window_start := _like_window_start(sort_by):
            tasks = tasks.annotate(
                period_like_count=Count(
                    "likes",
                    filter=Q(likes__created_at__gte=like_window_start),
                    distinct=True,
                )
            ).order_by("-period_like_count", "-priority", "-created_at")
        else: # Orden por defecto (más recientes)
            tasks = tasks.order_by('-priority', '-created_at')

        # ✅ FIX: Devolvemos el queryset de Task directamente, el serializador se encargará del resto.
        return tasks

        


    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)

        # ✅ FIX: Si el queryset es de Tasks, usamos TaskSerializer.
        # Si es una lista de FeedItem, usamos FeedItemSerializer.
        if isinstance(queryset, list) and queryset and isinstance(queryset[0], FeedItem):
             # El feed unificado se ordena en memoria; solo se precargan las
             # relaciones de los items de la pagina actual (no de todo el feed).
             if page:
                 prefetch_related_objects(
                     [i.item for i in page if i.is_original], *TASK_SERIALIZER_PREFETCH
                 )
                 prefetch_related_objects(
                     [i.item for i in page if not i.is_original], *SHARED_TASK_PREFETCH
                 )
             serializer = self.get_serializer(page, many=True)
        elif page:
             # Si es un queryset de Task, usamos el serializador de Task.
             serializer = TaskSerializer(page, many=True, context={'request': request})
        else:
            serializer = self.get_serializer(page, many=True)

        return self.get_paginated_response(serializer.data)


# ============================================================================
# ACCIONES ADMIN
# ============================================================================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_verify_user(request, user_id):
    # Staff y superadmins pueden verificar perfiles.
    if not (request.user.is_staff or request.user.is_superuser): return Response(status=403)
    user = get_object_or_404(User, id=user_id)
    is_verified = bool(request.data.get("is_verified", True))
    with transaction.atomic():
        user.profile.is_verified = is_verified
        user.profile.save()
        # Queda registrado quién lo verificó (se puede ver en la actividad de ambos).
        if request.user.id != user.id:
            if is_verified:
                ms.UserVerification.objects.get_or_create(verifier=request.user, verified=user)
            else:
                ms.UserVerification.objects.filter(verified=user).delete()
    return Response({"is_verified": user.profile.is_verified})

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_recommend_user(request, user_id):
    if not request.user.is_superuser: return Response(status=403)
    user = get_object_or_404(User, id=user_id)
    is_recommended = bool(request.data.get("is_recommended", True))
    with transaction.atomic():
        user.profile.is_recommended = is_recommended
        user.profile.save()
        # Además de la insignia, queda registrado a quién recomienda este admin
        # (se ve en "Recomendaciones" del perfil de ambos).
        if request.user.id != user.id:
            if is_recommended:
                ms.UserRecommendation.objects.get_or_create(
                    recommender=request.user, recommended=user,
                )
            else:
                ms.UserRecommendation.objects.filter(
                    recommender=request.user, recommended=user,
                ).delete()
    return Response({"is_recommended": user.profile.is_recommended})

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def verify_admin(request):
    return Response({
        "is_staff": request.user.is_staff,
        "is_admin": request.user.is_staff or request.user.is_superuser,
    })


def _require_admin(request):
    if not (request.user.is_staff or request.user.is_superuser):
        raise PermissionDenied("Solo el equipo de administración puede realizar esta acción.")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_users(request):
    _require_admin(request)
    users = User.objects.select_related("profile").order_by("-date_joined")
    search = (request.query_params.get("search") or "").strip()
    if search:
        users = users.filter(
            Q(username__icontains=search)
            | Q(email__icontains=search)
            | Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
        )
    active = request.query_params.get("is_active")
    if active in {"true", "false"}:
        users = users.filter(is_active=(active == "true"))
    paginator = StandardPagination()
    page = paginator.paginate_queryset(users, request)
    data = AdminUserSerializer(page, many=True, context={"request": request}).data
    return paginator.get_paginated_response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_tasks(request):
    _require_admin(request)
    tasks = ms.Task.objects.select_related("user").prefetch_related(
        *TASK_SERIALIZER_PREFETCH
    )
    search = (request.query_params.get("search") or "").strip()
    if search:
        tasks = tasks.filter(
            Q(title__icontains=search)
            | Q(description__icontains=search)
            | Q(username__icontains=search)
        )
    paginator = StandardPagination()
    page = paginator.paginate_queryset(tasks.order_by("-created_at"), request)
    data = TaskSerializer(page, many=True, context={"request": request}).data
    return paginator.get_paginated_response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_set_user_active(request, user_id):
    if not request.user.is_superuser:
        raise PermissionDenied("Solo un superadministrador puede suspender o reactivar cuentas.")
    target = get_object_or_404(User, id=user_id)
    active = request.data.get("is_active")
    if not isinstance(active, bool):
        raise ValidationError({"is_active": "Debe ser un valor booleano."})
    if target.id == request.user.id and not active:
        raise ValidationError({"is_active": "No puedes suspender tu propia cuenta."})
    if target.is_superuser and target.id != request.user.id:
        raise ValidationError({"is_active": "No se puede suspender a otro superadministrador."})
    target.is_active = active
    target.save(update_fields=["is_active"])
    return Response({"id": str(target.id), "is_active": target.is_active})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_set_user_role(request, user_id):
    """Superadmin: hacer o quitar staff ({is_staff}) o admin ({is_superuser}) a otra cuenta.

    Un admin siempre es staff; al quitarle admin queda como staff."""
    if not request.user.is_superuser:
        raise PermissionDenied("Solo un superadministrador puede administrar roles.")
    target = get_object_or_404(User, id=user_id)
    if target.id == request.user.id:
        raise ValidationError({"detail": "No puedes cambiar tu propio rol."})
    changes = {}
    for field in ("is_staff", "is_superuser"):
        if field in request.data:
            value = request.data.get(field)
            if not isinstance(value, bool):
                raise ValidationError({field: "Debe ser un valor booleano."})
            changes[field] = value
    if not changes:
        raise ValidationError({"detail": "Indica is_staff o is_superuser."})
    if changes.get("is_superuser"):
        changes["is_staff"] = True
    elif changes.get("is_staff") is False and changes.get("is_superuser", target.is_superuser):
        raise ValidationError({"is_staff": "Primero quítale el rol de admin."})
    for field, value in changes.items():
        setattr(target, field, value)
    target.save(update_fields=list(changes))
    return Response({"id": str(target.id), "is_staff": target.is_staff, "is_superuser": target.is_superuser})


def _reported_target_exists(target_type, target_id):
    target_models = {
        ms.UserReport.TARGET_USER: User,
        ms.UserReport.TARGET_TASK: ms.Task,
        ms.UserReport.TARGET_COMMENT: ms.NewPeticionCommentPost,
        ms.UserReport.TARGET_SHARED_COMMENT: ms.SharedTaskComment,
        ms.UserReport.TARGET_SHARED_TASK: ms.SharedTask,
        ms.UserReport.TARGET_FORUM_POST: ms.Postt,
    }
    target_model = target_models.get(target_type)
    if not target_model:
        return False
    try:
        target_pk = target_model._meta.pk.to_python(target_id)
    except (DjangoValidationError, TypeError, ValueError):
        return False
    return target_model.objects.filter(pk=target_pk).exists()


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def user_reports(request):
    if request.method == "GET":
        _require_admin(request)
        reports = ms.UserReport.objects.select_related("reporter", "reviewed_by")
        status_filter = request.query_params.get("status")
        if status_filter in dict(ms.UserReport.STATUS_CHOICES):
            reports = reports.filter(status=status_filter)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(reports, request)
        data = UserReportSerializer(page, many=True, context={"request": request}).data
        return paginator.get_paginated_response(data)

    target_type = request.data.get("target_type")
    target_id = str(request.data.get("target_id") or "").strip()
    reason = str(request.data.get("reason") or "").strip()
    if target_type not in dict(ms.UserReport.TARGET_CHOICES):
        raise ValidationError({"target_type": "Tipo de contenido no válido."})
    if not target_id:
        raise ValidationError({"target_id": "Se requiere el identificador del contenido."})
    if not reason or len(reason) > 1000:
        raise ValidationError({"reason": "Describe el motivo (máximo 1000 caracteres)."})
    if not _reported_target_exists(target_type, target_id):
        raise ValidationError({"target_id": "El contenido reportado no existe."})
    report, created = ms.UserReport.objects.get_or_create(
        reporter=request.user,
        target_type=target_type,
        target_id=target_id,
        status=ms.UserReport.STATUS_PENDING,
        defaults={"reason": reason},
    )
    if not created:
        report.reason = reason
        report.save(update_fields=["reason", "updated_at"])
    return Response(
        UserReportSerializer(report, context={"request": request}).data,
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def admin_update_report(request, report_id):
    _require_admin(request)
    report = get_object_or_404(ms.UserReport, id=report_id)
    new_status = request.data.get("status")
    if new_status not in {ms.UserReport.STATUS_RESOLVED, ms.UserReport.STATUS_DISMISSED}:
        raise ValidationError({"status": "El estado debe ser resolved o dismissed."})
    note = str(request.data.get("resolution_note") or "").strip()
    if len(note) > 500:
        raise ValidationError({"resolution_note": "Máximo 500 caracteres."})
    report.status = new_status
    report.reviewed_by = request.user
    report.resolution_note = note
    report.save(update_fields=["status", "reviewed_by", "resolution_note", "updated_at"])
    return Response(UserReportSerializer(report, context={"request": request}).data)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
@transaction.atomic
def admin_delete_reported_content(request, report_id):
    _require_admin(request)
    report = get_object_or_404(ms.UserReport.objects.select_for_update(), id=report_id)
    target_models = {
        ms.UserReport.TARGET_TASK: ms.Task,
        ms.UserReport.TARGET_COMMENT: ms.NewPeticionCommentPost,
        ms.UserReport.TARGET_SHARED_COMMENT: ms.SharedTaskComment,
        ms.UserReport.TARGET_SHARED_TASK: ms.SharedTask,
        ms.UserReport.TARGET_FORUM_POST: ms.Postt,
    }
    target_model = target_models.get(report.target_type)
    if target_model is None:
        raise ValidationError({
            "target_type": "Las cuentas se suspenden o reactivan; no se borran desde la moderación."
        })
    try:
        target_pk = target_model._meta.pk.to_python(report.target_id)
    except (DjangoValidationError, TypeError, ValueError):
        target_pk = None
    target = target_model.objects.filter(pk=target_pk).first() if target_pk is not None else None
    if target is None:
        raise ValidationError({"target_id": "El contenido ya no existe."})

    target.delete()
    report.status = ms.UserReport.STATUS_RESOLVED
    report.reviewed_by = request.user
    report.resolution_note = "Contenido eliminado por moderación."
    report.save(update_fields=["status", "reviewed_by", "resolution_note", "updated_at"])
    return Response(UserReportSerializer(report, context={"request": request}).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@transaction.atomic
def admin_suspend_reported_user(request, report_id):
    if not request.user.is_superuser:
        raise PermissionDenied("Solo un superadministrador puede suspender cuentas.")
    report = get_object_or_404(ms.UserReport.objects.select_for_update(), id=report_id)
    if report.target_type != ms.UserReport.TARGET_USER:
        raise ValidationError({"target_type": "La denuncia no corresponde a una cuenta."})
    target = get_object_or_404(User.objects.select_for_update(), id=report.target_id)
    if target.id == request.user.id or target.is_superuser:
        raise ValidationError({"target_id": "No se puede suspender esta cuenta."})
    target.is_active = False
    target.save(update_fields=["is_active"])
    report.status = ms.UserReport.STATUS_RESOLVED
    report.reviewed_by = request.user
    report.resolution_note = "Cuenta suspendida por moderación."
    report.save(update_fields=["status", "reviewed_by", "resolution_note", "updated_at"])
    return Response(UserReportSerializer(report, context={"request": request}).data)


# ============================================================================
# ACCIONES ADMIN ADICIONALES (FIX PARA URLS)
# ============================================================================

def _requested_like(request):
    """`liked` del body: true (por defecto) da el like, false lo quita, como en la web."""
    value = request.data.get("liked", True) if hasattr(request.data, "get") else True
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", ""}
    return bool(value)


def _app_account_or_error(request):
    """(cuenta de la App, None) o (None, respuesta de error) si no hay permiso o no existe."""
    if not (request.user.is_staff or request.user.is_superuser):
        return None, Response({"detail": "Forbidden"}, status=403)
    app_account = get_app_account()
    if app_account is None:
        return None, Response(
            {"detail": f"No existe la cuenta de la App ({app_account_email() or 'APP_ACCOUNT_EMAIL vacío'})."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return app_account, None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_app_like_task(request, task_id):
    """Staff o superadmin: dar o quitar su like "como la App" a una tarea. Body: {liked}.

    El like queda a su nombre marcado `as_app` (sale en la pestaña App de la lista). Al darlo,
    se avisa al resto del equipo para que también lo dé."""
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Forbidden"}, status=403)

    task = get_object_or_404(visible_tasks_for(request.user), id=task_id)
    if _requested_like(request):
        like, _ = ensure_visible_like(request.user, task)
        if not like.as_app:
            like.as_app = True
            like.save(update_fields=["as_app"])
        app_like_requests.notify_app_like_request(task, request.user)
    else:
        ms.Like.objects.filter(user=request.user, task=task, as_app=True).delete()

    return Response({
        "task_id": task.id,
        "liked": ms.Like.objects.filter(user=request.user, task=task, as_app=True).exists(),
        "likes_count": task.likes.count(),
    })

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_app_like_profile(request, profile_id):
    """Staff o superadmin: dar o quitar el like de la App (APP_ACCOUNT_EMAIL) a un perfil. Body: {liked}."""
    app_account, error = _app_account_or_error(request)
    if error:
        return error

    profile_user = get_object_or_404(User, id=profile_id)
    if _requested_like(request):
        ms.LikeP.objects.get_or_create(user=app_account, profile=profile_user)
    else:
        ms.LikeP.objects.filter(user=app_account, profile=profile_user).delete()

    return Response({
        "profile_id": profile_user.id,
        "liked": ms.LikeP.objects.filter(user=app_account, profile=profile_user).exists(),
        "likes_count": profile_user.likes_received.count(),
        "app_account_id": app_account.id,
    })

# ============================================================================
# ETIQUETADO DE PERSONAS EN PUBLICACIONES
# ============================================================================

@api_view(["POST"])
@permission_classes([IsAuthenticated])
@transaction.atomic
def sync_task_tags(request, task_id):
    """Sincroniza las personas etiquetadas en una tarea. Solo el autor (o staff)."""
    task = get_object_or_404(ms.Task, id=task_id)
    if task.user_id != request.user.id and not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Solo el autor puede editar las etiquetas."}, status=403)

    user_ids = request.data.get("user_ids", [])
    if not isinstance(user_ids, list):
        return Response({"detail": "user_ids debe ser una lista."}, status=400)

    # El autor no tiene sentido etiquetándose a sí mismo
    try:
        desired_ids = {
            str(UUID(str(uid)))
            for uid in user_ids
            if str(uid) != str(request.user.id)
        }
    except (TypeError, ValueError, AttributeError):
        return Response({"detail": "Cada user_id debe ser un UUID válido."}, status=400)
    users_map = {str(u.id): u for u in User.objects.filter(id__in=desired_ids)}
    if desired_ids - set(users_map):
        return Response({"detail": "Uno o más usuarios no existen."}, status=400)

    existing_tags = {str(tag.user_id): tag for tag in task.tagged_users.all()}

    # Altas (la señal post_save dispara la notificación al etiquetado)
    for uid in desired_ids - set(existing_tags):
        user = users_map.get(uid)
        if user:
            ms.TaskTag.objects.get_or_create(
                task=task, user=user, defaults={"tagged_by": request.user}
            )

    # Bajas
    removed = set(existing_tags) - desired_ids
    if removed:
        task.tagged_users.filter(user_id__in=removed).delete()

    tagged = [
        {
            "id": str(tag.user.id),
            "username": getattr(tag.user, "username", None),
            "first_name": getattr(tag.user, "first_name", "") or "",
            "last_name": getattr(tag.user, "last_name", "") or "",
        }
        for tag in task.tagged_users.select_related("user").all()
    ]
    return Response({"task_id": str(task.id), "tagged_users": tagged})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@transaction.atomic
def toggle_task_approval(request, task_id):
    """Solo administradores pueden aprobar o retirar la aprobación de aportaciones."""
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Solo administradores."}, status=403)

    task = get_object_or_404(ms.Task, id=task_id)
    approve_value = request.data.get("approve", True)
    if isinstance(approve_value, bool):
        approve = approve_value
    elif isinstance(approve_value, str) and approve_value.strip().casefold() in {"true", "false"}:
        approve = approve_value.strip().casefold() == "true"
    elif isinstance(approve_value, int) and approve_value in (0, 1):
        approve = bool(approve_value)
    else:
        return Response(
            {"approve": "Debe ser un valor booleano."},
            status=400,
        )

    cats = [c.strip() for c in (task.categories or "").split(",") if c.strip()]
    category_keys = {category.casefold() for category in cats}
    is_podcast = ms.PODCAST_CATEGORY.casefold() in category_keys
    is_petition = (task.pch or "").strip().casefold() in {"peticion", "peticiones"}
    if not (is_podcast or is_petition):
        return Response(
            {"detail": "Solo se pueden aprobar peticiones o propuestas de podcast."},
            status=400,
        )
    approval_aliases = {"aprobada", "aprobadas", "aprovada", "aprovadas"}
    has_tag = bool(category_keys & approval_aliases)

    if approve and not has_tag:
        cats = [
            c for c in cats
            if c.casefold() != "procesando" and c.casefold() not in approval_aliases
        ]
        cats.append(ms.APPROVED_TAG)
    elif not approve and has_tag:
        cats = [c for c in cats if c.casefold() not in approval_aliases]
        if not any(c.casefold() == "procesando" for c in cats):
            cats.append("Procesando")

    if (approve and not has_tag) or (not approve and has_tag):
        task.categories = ", ".join(cats)
        task.save(update_fields=["categories"])

    kind = "podcast" if is_podcast else "petition"

    if approve and not has_tag:
        actor_name = request.user.username or request.user.email
        recipients = [task.user] + [
            tag.user for tag in task.tagged_users.select_related("user").all()
        ]
        seen = set()
        for recipient in recipients:
            if not recipient or recipient.id == request.user.id or recipient.id in seen:
                continue
            seen.add(recipient.id)
            if is_podcast:
                text = "🎉 ¡Felicidades! El podcast donde fuiste etiquetado fue aprobado. ¡A grabar se ha dicho! 🎙️"
                ntype = "podcast_approved"
            else:
                text = f"✅ Tu aportación fue aprobada por {actor_name}"
                ntype = "task_approved"
            create_notification(
                recipient=recipient,
                actor=request.user,
                notification_type=ntype,
                target_type="task",
                target_id=task.id,
                data={"text": text, "task_title": task.title[:80]},
                dedupe_key=f"approved:{kind}:{task.id}",
            )
        if is_podcast:
            for tag in task.tagged_users.select_related("user").all():
                invitation, _ = ms.PodcastInvitation.objects.get_or_create(
                    task=task,
                    user=tag.user,
                    defaults={"status": ms.PodcastInvitation.STATUS_PENDING},
                )
                if invitation.status == ms.PodcastInvitation.STATUS_PENDING:
                    Message.objects.filter(
                        sender=request.user,
                        receiver=tag.user,
                        content__startswith=f"🎙️ Invitación a podcast aprobada | tarea:{task.id}",
                    ).first() or Message.objects.create(
                        sender=request.user,
                        receiver=tag.user,
                        content=(
                            f"🎙️ Invitación a podcast aprobada | tarea:{task.id}\n"
                            f"↪ Publicación compartida: {task.title} [task:{task.id}]\n"
                            "🎙️ Invitación a podcast aprobada.\n"
                            "Fuiste invitado a participar. Abre la tarea para aceptar o rechazar la invitación."
                        ),
                    )
    elif not approve and has_tag:
        # Al quitar la aprobación, retiramos la felicitación
        retire_notification(dedupe_key=f"approved:{kind}:{task.id}")

    return Response({
        "task_id": str(task.id),
        "approved": approve,
        "categories": task.categories,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@transaction.atomic
def respond_podcast_invitation(request, task_id):
    task = get_object_or_404(ms.Task, id=task_id)
    task_categories = {
        category.strip().casefold()
        for category in (task.categories or "").split(",")
        if category.strip()
    }
    if (
        ms.PODCAST_CATEGORY.casefold() not in task_categories
        or not task_categories.intersection({"aprobada", "aprobadas", "aprovada", "aprovadas"})
    ):
        return Response(
            {"detail": "La invitación solo está activa para podcasts aprobados."},
            status=400,
        )
    invitation = get_object_or_404(
        ms.PodcastInvitation,
        task=task,
        user=request.user,
    )
    if invitation.status != ms.PodcastInvitation.STATUS_PENDING:
        return Response({"status": invitation.status, "detail": "La invitación ya fue respondida."}, status=400)

    accepted_value = request.data.get("accepted", True)
    if isinstance(accepted_value, bool):
        accepted = accepted_value
    elif isinstance(accepted_value, str) and accepted_value.strip().casefold() in {"true", "false"}:
        accepted = accepted_value.strip().casefold() == "true"
    elif isinstance(accepted_value, int) and accepted_value in (0, 1):
        accepted = bool(accepted_value)
    else:
        return Response({"accepted": "Debe ser un valor booleano."}, status=400)
    invitation.status = (
        ms.PodcastInvitation.STATUS_ACCEPTED
        if accepted else ms.PodcastInvitation.STATUS_DECLINED
    )
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=["status", "responded_at", "updated_at"])

    actor_name = request.user.username or request.user.email
    status_text = "aceptó" if accepted else "rechazó"
    admins = User.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).exclude(id=request.user.id)
    recipients = list(admins)
    if task.user and task.user_id != request.user.id and task.user_id not in {
        recipient.id for recipient in recipients
    }:
        recipients.append(task.user)
    for recipient in recipients:
        create_notification(
            recipient=recipient,
            actor=request.user,
            notification_type="podcast_invitation_response",
            target_type="task",
            target_id=task.id,
            data={"text": f"{actor_name} {status_text} la invitación al podcast.", "task_title": task.title[:80]},
            dedupe_key=f"podcast-response:{task.id}:{request.user.id}",
        )

    admin_recipient = User.objects.filter(email__iexact="owen@hotmail.com").first()
    if admin_recipient and admin_recipient.id != request.user.id:
        Message.objects.create(
            sender=request.user,
            receiver=admin_recipient,
            content=(
                f"↪ Publicación compartida: {task.title} [task:{task.id}]\n"
                f"🎙️ Invitación al podcast {status_text}.\n"
                f"El usuario {actor_name} respondió {status_text} la invitación."
            ),
        )

    return Response({"task_id": str(task.id), "status": invitation.status})


# ============================================================================
# FORO (Posts)
# ============================================================================

class PostListCreateView(generics.ListCreateAPIView):
    queryset = ms.Postt.objects.filter(parent__isnull=True)
    serializer_class = PosttSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardPagination
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        qs = ms.Postt.objects.filter(parent__isnull=True).select_related("user", "group").prefetch_related(*POST_PREFETCH).order_by('-created_at')
        # Foro de un grupo (?group=<id>) o el foro general (sin grupo).
        group_id = self.request.query_params.get('group')
        if group_id:
            try:
                qs = qs.filter(group_id=UUID(str(group_id)))
            except ValueError:
                qs = qs.none()
        else:
            qs = qs.filter(group__isnull=True)
        # Solo lo que puede ver: seguidores, suscriptores, invitados, grupos privados.
        qs = visible_forum_posts(qs, self.request.user)
        user_id = self.request.query_params.get('user')
        if user_id:
            # Los ids de usuario son UUID ("Foro de <usuario>").
            try:
                qs = qs.filter(user_id=UUID(str(user_id)))
            except ValueError:
                qs = qs.none()
        # Un #hashtag tocado en el foro: los posts que lo usan (en el post o en sus respuestas).
        raw_hashtag = (self.request.query_params.get('hashtag') or '').strip().lstrip('#')
        if raw_hashtag:
            hashtag = mentions.hashtag_in_search(f"#{raw_hashtag}")
            qs = qs.filter(mentions.hashtag_q(hashtag, "title", "content", "forum_replies__content")).distinct() if hashtag else qs.none()
        # Mismo filtro por niveles que las tareas: primero los de esa ruta exacta.
        return filter_by_category_path(qs, self.request.query_params.get('category'))

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class PostDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = ms.Postt.objects.all()
    serializer_class = PosttSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "id"

    def get_queryset(self):
        return super().get_queryset().select_related("user", "group").prefetch_related(*POST_PREFETCH)

    def get_object(self):
        post = super().get_object()
        if not can_view_post(post, self.request.user):
            raise NotFound()
        return post

    def perform_update(self, serializer):
        # 🛡️ SEGURIDAD: solo el autor (o staff) puede editar el post
        if serializer.instance.user != self.request.user and not self.request.user.is_staff:
            raise PermissionDenied("No autorizado para editar este post.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.user != self.request.user and not self.request.user.is_staff:
            raise PermissionDenied("No autorizado para eliminar este post.")
        instance.delete()


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_post_like(request, post_id):
    post = get_object_or_404(ms.Postt, id=post_id)
    if not can_view_post(post, request.user):
        raise NotFound()
    like, created = ms.LikePostt.objects.get_or_create(user=request.user, post=post)
    if not created:
        like.delete()
        return Response({"liked": False, "likes_count": post.likes.count()})
    return Response({"liked": True, "likes_count": post.likes.count()}, status=201)


# ============================================================================
# COMENTARIOS Y LIKES PARA TAREAS COMPARTIDAS
# ============================================================================

class SharedTaskDetailView(generics.RetrieveAPIView):
    """Obtiene el detalle de una tarea compartida con comentarios"""
class SharedTaskDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Obtiene, actualiza o elimina una tarea compartida."""
    queryset = ms.SharedTask.objects.all()
    serializer_class = SharedTaskDetailSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "id"

    def get_queryset(self):
        return super().get_queryset().select_related("task", "shared_by").prefetch_related("comments", "likes")

    def perform_destroy(self, instance):
        if instance.shared_by != self.request.user and not self.request.user.is_staff:
            raise PermissionDenied("No tienes permiso para eliminar esta publicación compartida.")
        instance.delete()

    def perform_update(self, serializer):
        if serializer.instance.shared_by != self.request.user and not self.request.user.is_staff:
            raise PermissionDenied("No tienes permiso para editar esta publicación compartida.")
        serializer.save()


class SharedTaskCommentListCreateView(generics.ListCreateAPIView):
    """Lista comentarios padre de una tarea compartida y permite crear nuevos"""
    serializer_class = SharedTaskCommentSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = CommentPagination
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        shared_task_id = self.kwargs.get("shared_task_id")
        return ms.SharedTaskComment.objects.filter(
            shared_task_id=shared_task_id, parent__isnull=True
        ).select_related("created_by__profile").prefetch_related("replies", "likes")

    def perform_create(self, serializer):
        shared_task_id = self.kwargs.get("shared_task_id")
        
        # 1. Guardar el comentario en la Tarea Compartida
        shared_comment = serializer.save(created_by=self.request.user, shared_task_id=shared_task_id)
        
        # 2. ⚡ SINCRONIZAR ("Como la función de likes"): Replicarlo en la tarea original
        shared_task = get_object_or_404(ms.SharedTask, id=shared_task_id)
        
        # ⚡ Respetar el ANIDADO buscando a su equivalente padre
        original_parent = None
        if shared_comment.parent:
            original_parent = ms.NewPeticionCommentPost.objects.filter(
                post=shared_task.task,
                created_by=shared_comment.parent.created_by,
                text=shared_comment.parent.text
            ).first()
            
        ms.NewPeticionCommentPost.objects.create(
            post=shared_task.task,
            created_by=self.request.user,
            text=shared_comment.text,
            parent=original_parent,
            # La copia lleva también la foto o video y la publicación guardada (mismo archivo).
            attachment=shared_comment.attachment.name or None,
            aportacion=shared_comment.aportacion,
        )


class SharedTaskCommentDetailsView(generics.RetrieveUpdateDestroyAPIView):
    """Obtiene, actualiza o elimina un comentario específico de tarea compartida"""
    queryset = ms.SharedTaskComment.objects.all()
    serializer_class = SharedTaskCommentSerializer
    permission_classes = [IsAuthenticated]
    lookup_url_kwarg = "comment_id"
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def perform_update(self, serializer):
        # 🛡️ SEGURIDAD: solo el creador del comentario (o staff) puede editarlo
        if serializer.instance.created_by != self.request.user and not self.request.user.is_staff:
            raise PermissionDenied("No tienes permiso para editar este comentario.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.created_by != self.request.user and not self.request.user.is_staff:
            raise PermissionDenied("No tienes permiso para eliminar este comentario.")
            
        # ⚡ Borrar también el clon en la tarea original si el usuario decide eliminar su comentario
        ms.NewPeticionCommentPost.objects.filter(
            post=instance.shared_task.task,
            created_by=instance.created_by,
            text=instance.text
        ).delete()
            
        instance.delete()


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_shared_task_like(request, shared_task_id):
    """Da o quita like a una tarea compartida y sincroniza el like con la tarea original."""
    shared_task = get_object_or_404(ms.SharedTask, id=shared_task_id)

    like_shared, created_shared = ms.LikeSharedTask.objects.get_or_create(
        user=request.user,
        shared_task=shared_task,
    )

    if not created_shared:
        like_shared.delete()
        ms.Like.objects.filter(user=request.user, task=shared_task.task).delete()
        liked = False
    else:
        ensure_visible_like(request.user, shared_task.task)
        liked = True

    return Response({
        "liked": liked,
        "likes_count_shared": shared_task.likes.count(),
        "likes_count_original": shared_task.task.likes.count(),
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_shared_task_comment_like(request, shared_task_id, comment_id):
    """Da o quita like a un comentario de tarea compartida"""
    comment = get_object_or_404(ms.SharedTaskComment, id=comment_id, shared_task_id=shared_task_id)
    
    like, created = ms.LikeSharedTaskComment.objects.get_or_create(
        user=request.user, comment=comment
    )
    
    # ⚡ Sincronizar: Likear el clon original
    original_clone = ms.NewPeticionCommentPost.objects.filter(
        post=comment.shared_task.task, created_by=comment.created_by, text=comment.text
    ).first()
    
    if not created:
        like.delete()
        if original_clone:
            ms.LikeCommentPost.objects.filter(user=request.user, comment=original_clone).delete()
        return Response({
            "liked": False,
            "likes_count": comment.likes.count()
        })
        
    if original_clone:
        ms.LikeCommentPost.objects.get_or_create(user=request.user, comment=original_clone)
    
    return Response({
        "liked": True,
        "likes_count": comment.likes.count()
    }, status=201)