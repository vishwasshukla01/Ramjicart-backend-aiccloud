import mimetypes
import os

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.contrib import admin
from django.http import FileResponse, Http404, JsonResponse
from django.urls import include, path
from django.utils._os import safe_join
from django.views.decorators.http import require_safe
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView


def health(_request):
    return JsonResponse({"status": "ok"})


def api_unknown(_request, unmatched):
    return JsonResponse({"detail": "Not found."}, status=404)


def api_not_found(request, _exception):
    if request.path.startswith("/api/"):
        return JsonResponse({"detail": "Not found."}, status=404)
    from django.views.defaults import page_not_found

    return page_not_found(request, _exception)


@require_safe
def uploaded_media(request, path):
    if not path.startswith(("food-images/", "restaurant-logos/")):
        raise Http404
    try:
        file_path = safe_join(str(settings.MEDIA_ROOT), path)
    except (ValueError, SuspiciousFileOperation):
        raise Http404
    if not os.path.isfile(file_path):
        raise Http404
    content_type, _encoding = mimetypes.guess_type(file_path)
    response = FileResponse(
        open(file_path, "rb"),
        content_type=content_type or "application/octet-stream",
    )
    response["Cache-Control"] = "public, max-age=86400"
    return response


urlpatterns = [
    path("media/<path:path>", uploaded_media, name="uploaded-media"),
    path("admin/", admin.site.urls),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="api-docs"),
    path("api/", include("orders.urls")),
    path("api/<path:unmatched>", api_unknown),
    path("", health, name="health"),
]

handler404 = "ramjicart.urls.api_not_found"
