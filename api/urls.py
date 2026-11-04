from django.urls import path
from . import views
from . import notification_views
from . import throttling as ts
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

from . import account
from . import google_auth
from . import push_views
from . import subscription_views
from . import petition_support_views
from . import group_views
from . import user_badges
from . import testimonials
from . import profile_activity
from . import profile_visits
from . import task_stats
from . import task_map
from . import mentions
from . import save_collections
from . import task_save_views
# ─── Vistas JWT con throttling propio ────────────────────────────────────────
class ThrottledLoginView(TokenObtainPairView):
    throttle_classes = [ts.LoginThrottle]


class ThrottledRefreshView(TokenRefreshView):
    throttle_classes = [ts.RefreshThrottle]


urlpatterns = [
    # Notificaciones
    path('notifications/', notification_views.NotificationListView.as_view(), name='notification-list'),
    path('notifications/unread-count/', notification_views.unread_count, name='notification-unread-count'),
    path('notifications/mark-all-read/', notification_views.mark_all_read, name='notification-mark-all-read'),
    path('notifications/<uuid:notification_id>/mark-read/', notification_views.mark_read, name='notification-mark-read'),
    path('notifications/<uuid:notification_id>/seen/', notification_views.set_app_like_request_seen, name='notification-app-like-seen'),
    path('notifications/<uuid:notification_id>/', notification_views.delete_notification, name='notification-delete'),

    # Autenticación y Usuarios
    path('auth/register/', views.RegisterView.as_view(), name='register'),
    path('auth/login/', ThrottledLoginView.as_view(), name='token_obtain_pair'),
    path('auth/google/', google_auth.google_login, name='google-login'),
    path('auth/refresh/', ThrottledRefreshView.as_view(), name='token_refresh'),
    path('auth/logout/', views.LogoutView.as_view(), name='logout'),
    path('auth/password-reset/', account.password_reset_request, name='password-reset'),
    path('auth/password-reset/confirm/', account.password_reset_confirm, name='password-reset-confirm'),
    path('eliminar-cuenta/', account.account_deletion_page, name='account-deletion-page'),
    path('users/me/delete/', account.delete_account, name='user-delete'),
    path('users/me/', views.UserMeView.as_view(), name='user-me'),
    path('users/me/tasks/', views.UserMyTasksView.as_view(), name='user-my-tasks'),
    path('users/me/relations/', views.my_relations, name='user-my-relations'),
    path('users/badges/', user_badges.user_badges, name='user-badges'),
    path('users/<uuid:user_id>/testimonials/', testimonials.user_testimonials, name='user-testimonials'),
    path('testimonials/<uuid:pk>/', testimonials.testimonial_detail, name='testimonial-detail'),
    path('users/<uuid:user_id>/social/', views.user_social_stats, name='user-social-stats'),
    path('users/<uuid:user_id>/follow/', views.toggle_user_follow, name='user-follow-toggle'),
    path('users/in-category/', views.users_in_category, name='users-in-category'),
    path('users/<uuid:user_id>/followers/', views.user_followers, name='user-followers'),
    path('users/<uuid:user_id>/following/', views.user_following, name='user-following'),
    path('users/<uuid:user_id>/favorited-by/', views.user_favorited_by, name='user-favorited-by'),
    path('users/<uuid:user_id>/recommendations/', views.user_recommendations, name='user-recommendations'),
    path('users/<uuid:user_id>/activity/', profile_activity.user_activity, name='user-activity'),
    path('users/<uuid:user_id>/visit/', profile_visits.record_profile_visit, name='profile-visit-record'),
    path('users/<uuid:user_id>/visits/', profile_visits.profile_visits, name='profile-visits'),
    path('users/<uuid:user_id>/portadas/', views.obtener_portadas_usuario, name='user-portadas'),
    path('users/<uuid:user_id>/imagen-fija/', views.obtener_imagen_fija_usuario, name='user-imagen-fija'),
    path('user-details/', views.get_user_details, name='user-details'),
    path('profile/', views.get_current_user_profile, name='user-profile'),
    path('massaging/users/', views.list_users, name='list-users'),

    # Tareas (Reels) y Feed
    path('tasks/', views.TaskListCreateView.as_view(), name='task-list-create'),
    path('stories/', views.StoryListCreateView.as_view(), name='story-list-create'),
    path('stories/share-task/', views.share_task_to_story, name='story-share-task'),
    path('stories/<uuid:story_id>/view/', views.record_story_view, name='story-view'),
    path('stories/<uuid:story_id>/viewers/', views.story_viewers, name='story-viewers'),
    path('stories/<uuid:story_id>/save/', views.save_story_to_aportaciones, name='story-save'),
    path('feed/', views.FeedView.as_view(), name='feed'),
    path('tasks/map/', task_map.map_tasks, name='task-map'),
    path('hashtags/', mentions.hashtag_suggestions, name='hashtag-suggestions'),
    path('users/by-username/<str:username>/', mentions.user_by_username, name='user-by-username'),
    path('tasks/<uuid:id>/', views.TaskDetailView.as_view(), name='task-detail'),
    path('tasks/<uuid:task_id>/like/', views.toggle_task_like, name='task-like'),
    path('tasks/<uuid:task_id>/stats/', task_stats.task_stats, name='task-stats'),
    path('tasks/<uuid:task_id>/repost/', views.repost_task, name='task-repost'), # ✅ ¡AQUÍ ESTÁ!
    path('tasks/<uuid:task_id>/users-who-liked/', views.users_who_liked_task, name='task-likers'),
    path('tasks/<uuid:task_id>/users-who-shared/', views.users_who_shared_task, name='task-sharers'),
    path('tasks/<uuid:task_id>/tags/', views.sync_task_tags, name='task-tags'),
    path('tasks/<uuid:task_id>/support/', petition_support_views.petition_support_checkout, name='petition-support-checkout'),
    path('tasks/<uuid:task_id>/supporters/', petition_support_views.petition_supporters, name='petition-supporters'),
    path('petition-supports/', petition_support_views.my_petition_supports, name='petition-support-list'),
    path('petition-supports/users/<uuid:user_id>/', petition_support_views.user_petition_supports, name='petition-support-user-list'),
    path('petition-supports/checkout/done/', petition_support_views.petition_support_checkout_done, name='petition-support-checkout-done'),
    path('petition-supports/<uuid:pk>/sync/', petition_support_views.petition_support_sync, name='petition-support-sync'),
    path('tasks/<uuid:task_id>/approve/', views.toggle_task_approval, name='task-approve'),
    path('tasks/<uuid:task_id>/podcast-invitation/', views.respond_podcast_invitation, name='podcast-invitation-response'),

    # Comentarios de Tareas
    path('tasks/<uuid:task_id>/comments/', views.TaskCommentListCreateView.as_view(), name='task-comments'),
    path('comments/<int:comment_id>/', views.NewPeticionCommentDetailsView.as_view(), name='task-comment-detail'),
    path('comments/<int:comment_id>/like/', views.toggle_comment_like, name='comment-like'),
    path('comments/<int:comment_id>/users-who-liked/', views.users_who_liked_comment, name='comment-likers'),

    # Tareas Compartidas (Legado, para TasksScreen)
    path('shared-tasks/', views.SharedTaskListCreateView.as_view(), name='shared-task-list-create'),
    path('shared-tasks/<uuid:id>/', views.SharedTaskDetailView.as_view(), name='shared-task-detail'),
    path('shared-tasks/<uuid:shared_task_id>/like/', views.toggle_shared_task_like, name='shared-task-like'),
    path('shared-tasks/<uuid:shared_task_id>/users-who-liked/', views.users_who_liked_shared_task, name='shared-task-likers'),
    path('shared-tasks/<uuid:shared_task_id>/comments/', views.SharedTaskCommentListCreateView.as_view(), name='shared-task-comments'),
    path('shared-tasks/<uuid:shared_task_id>/comments/<int:comment_id>/', views.SharedTaskCommentDetailsView.as_view(), name='shared-task-comment-detail'),
    path('shared-tasks/<uuid:shared_task_id>/comments/<int:comment_id>/like/', views.toggle_shared_task_comment_like, name='shared-task-comment-like'),
    path('shared-tasks/comments/<int:comment_id>/users-who-liked/', views.users_who_liked_shared_comment, name='shared-task-comment-likers'),

    # Foro
    path('posts/', views.PostListCreateView.as_view(), name='post-list-create'),
    path('posts/<int:id>/', views.PostDetailView.as_view(), name='post-detail'),
    path('posts/<int:post_id>/like/', views.toggle_post_like, name='post-like'),

    # Perfiles y Likes de Perfil
    path('profiles/<uuid:profile_id>/like/', views.like_unlike_profile, name='profile-like'),
    path('profiles/<uuid:profile_id>/likes/', views.list_likes, name='profile-likers'),

    # Favoritos
    path('favoritos/agregar/', views.agregar_favorito, name='favorito-add'),
    path('favoritos/listar/', views.listar_favoritos, name='favorito-list'),
    path('favoritos/listar/<uuid:user_id>/', views.listar_favoritos, name='favorito-list-user'),
    path('pfavoritos/agregar/', views.agregar_pfavorito, name='pfavorito-add'),
    path('pfavoritos/listar/', views.listar_pfavoritos, name='pfavorito-list'),
    path('pfavoritos/listar/<uuid:user_id>/', views.listar_pfavoritos, name='pfavorito-list-user'),
    path('favorites/collection/', views.favorite_collection, name='favorite-collection'),
    path('favorites/collection/<uuid:user_id>/', views.favorite_collection, name='favorite-collection-user'),
    path('favorites/pin/', views.toggle_favorite_pin, name='favorite-pin'),
    # Guardar -> Favorito -> Top 5 (general y por categoría)
    path('tasks/<uuid:task_id>/save/', task_save_views.task_save, name='task-save'),
    path('favorites/category-pin/', task_save_views.task_category_pin, name='favorite-category-pin'),
    path('favorites/top/', task_save_views.favorites_top, name='favorites-top'),
    # Colecciones (álbumes) dentro de guardados
    path('collections/', save_collections.my_collections, name='collections'),
    path('collections/user/<uuid:user_id>/', save_collections.user_collections, name='collections-user'),
    path('collections/<uuid:collection_id>/', save_collections.collection_detail, name='collection-detail'),
    path('collections/<uuid:collection_id>/items/', save_collections.collection_item, name='collection-items'),
    path('collections/<uuid:collection_id>/items/<uuid:task_id>/', save_collections.collection_item, name='collection-item'),
    path('tasks/<uuid:task_id>/collections/', save_collections.task_collections, name='task-collections'),
    path('favorites/top/<uuid:user_id>/', task_save_views.favorites_top, name='favorites-top-user'),
    path('search/directory/', views.search_directory, name='directory-search'),

    # Categorías
    path('categories/', views.create_categoryp, name='category-p-list-create'),
    path('categories/<int:pk>/', views.create_categoryp, name='category-p-detail'),
    path('categories/reorder/', views.reorder_categoryp, name='category-p-reorder'),
    path('categories/user/<uuid:user_id>/', views.list_categoryp_for_user, name='category-p-list-for-user'),
    path('categories/visibility/', views.personal_filter_visibility, name='category-p-visibility'),
    path('new-categories/', views.new_category_list_create, name='new-category-list-create'),
    path('new-categories/<int:pk>/', views.new_category_detail, name='new-category-detail'),

    # Filtros guardados por perfil (CRUD propio, consulta aparte del feed)
    path('saved-filters/', views.saved_filters_list_create, name='saved-filter-list-create'),
    path('saved-filters/<uuid:pk>/', views.saved_filter_detail, name='saved-filter-detail'),

    # Admin
    path('verify-admin/', views.verify_admin, name='verify-admin'), # FIX: Movido para que coincida con el frontend
    path('admin/users/<uuid:user_id>/verify/', views.admin_verify_user, name='admin-verify-user'),
    path('admin/users/<uuid:user_id>/recommend/', views.admin_recommend_user, name='admin-recommend-user'),
    path('admin/users/', views.admin_users, name='admin-users'),
    path('admin/users/<uuid:user_id>/active/', views.admin_set_user_active, name='admin-user-active'),
    path('admin/users/<uuid:user_id>/role/', views.admin_set_user_role, name='admin-user-role'),
    path('admin/tasks/', views.admin_tasks, name='admin-tasks'),
    path('admin/reports/', views.user_reports, name='user-reports'),
    path('admin/reports/<int:report_id>/', views.admin_update_report, name='admin-report-update'),
    path('admin/reports/<int:report_id>/content/', views.admin_delete_reported_content, name='admin-report-delete-content'),
    path('admin/reports/<int:report_id>/suspend-user/', views.admin_suspend_reported_user, name='admin-report-suspend-user'),
    path('reports/', views.user_reports, name='user-report-create'),
    path('admin/tasks/<uuid:task_id>/like/', views.admin_app_like_task, name='admin-task-like'),
    path('admin/profiles/<uuid:profile_id>/like/', views.admin_app_like_profile, name='admin-profile-like'),

    path('push-tokens/', push_views.PushTokenView.as_view(), name='push-token-register'),  # ← agregar en urlpatterns

    # ─── Suscripciones (Stripe) ──────────────────────────────────────────────
    # Grupos (comunidades con publicaciones revisadas por sus admins)
    path('groups/', group_views.group_list_create, name='group-list'),
    path('groups/<uuid:group_id>/', group_views.group_detail, name='group-detail'),
    path('groups/<uuid:group_id>/join/', group_views.group_join, name='group-join'),
    path('groups/<uuid:group_id>/leave/', group_views.group_leave, name='group-leave'),
    path('groups/<uuid:group_id>/members/', group_views.group_members, name='group-members'),
    path('groups/<uuid:group_id>/members/<uuid:user_id>/', group_views.group_member_detail, name='group-member-detail'),
    path('groups/<uuid:group_id>/tasks/', group_views.group_tasks, name='group-tasks'),
    path('groups/<uuid:group_id>/tasks/pending/', group_views.group_pending_tasks, name='group-pending-tasks'),
    path('groups/<uuid:group_id>/tasks/<uuid:task_id>/review/', group_views.group_task_review, name='group-task-review'),
    path('subscriptions/', subscription_views.my_subscriptions, name='subscription-list'),
    path('subscriptions/checkout/', subscription_views.subscription_checkout, name='subscription-checkout'),
    path('subscriptions/checkout/done/', subscription_views.subscription_checkout_done, name='subscription-checkout-done'),
    path('subscriptions/webhook/', subscription_views.stripe_webhook, name='subscription-webhook'),
    path('subscriptions/users/<uuid:user_id>/', subscription_views.user_subscriptions, name='subscription-user-list'),
    path('subscriptions/status/<uuid:user_id>/', subscription_views.subscription_status, name='subscription-status'),
    path('subscriptions/<uuid:pk>/', subscription_views.subscription_detail, name='subscription-detail'),
    path('subscriptions/<uuid:pk>/sync/', subscription_views.subscription_sync, name='subscription-sync'),
]