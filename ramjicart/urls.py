from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
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


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="api-docs"),
    path("api/", include("orders.urls")),
    path("api/<path:unmatched>", api_unknown),
    path("", health, name="health"),
]

handler404 = "ramjicart.urls.api_not_found"
