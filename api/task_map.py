"""Tareas en el mapa: las que su autor compartió desde su ubicación.

Solo salen las que cualquiera puede ver: nada oculto, ni historias sin guardar, ni
testimonios, ni lo de grupos privados o en revisión (salvo para quien sí lo ve).
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import models as ms
from .serializers import file_to_abs_url
from .views import exclude_hidden_tasks, exclude_testimonials, exclude_unsaved_stories

# Las más recientes primero; el mapa no necesita más puntos que estos.
MAP_TASKS_LIMIT = 300


def _bbox(params):
    """`bbox=oeste,sur,este,norte` limita el área; si no viene o no es válido, todo el mapa."""
    try:
        west, south, east, north = (float(value) for value in params.get("bbox", "").split(","))
    except ValueError:
        return None
    return west, south, east, north


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def map_tasks(request):
    tasks = ms.Task.objects.filter(latitude__isnull=False, longitude__isnull=False)
    tasks = exclude_hidden_tasks(tasks, viewer=request.user)
    tasks = exclude_testimonials(exclude_unsaved_stories(tasks))
    box = _bbox(request.query_params)
    if box:
        west, south, east, north = box
        tasks = tasks.filter(latitude__gte=south, latitude__lte=north, longitude__gte=west, longitude__lte=east)
    tasks = tasks.select_related("user").order_by("-created_at")[:MAP_TASKS_LIMIT]
    return Response([
        {
            "id": str(task.pk),
            "title": task.title,
            "pch": task.pch,
            "latitude": task.latitude,
            "longitude": task.longitude,
            "image": file_to_abs_url(task.image, request) if task.image else None,
            "user": {"id": str(task.user_id), "username": task.user.username if task.user else ""},
        }
        for task in tasks
    ])
