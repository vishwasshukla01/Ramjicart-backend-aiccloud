import json

from django.http import HttpResponse
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.response import Response
from rest_framework.views import APIView, exception_handler as drf_exception_handler
from rest_framework_simplejwt.views import TokenRefreshView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return response
    detail = response.data.get("detail") if isinstance(response.data, dict) else None
    if detail is None:
        detail = " ".join(
            str(value)
            for value in (response.data.values() if isinstance(response.data, dict) else [response.data])
        )
    response.data = {"detail": str(detail)}
    return response


class LegacyAPIView(APIView):
    throttle_classes = [ScopedRateThrottle]
    handler = None
    throttle_scope = None

    def _invoke(self, request, *args, **kwargs):
        django_request = request._request
        django_request._dont_enforce_csrf_checks = True
        response = self.handler(django_request, *args, **kwargs)
        if not isinstance(response, HttpResponse):
            return response
        if response.get("Content-Type", "").startswith("application/json"):
            try:
                payload = json.loads(response.content)
                payload = self._absolute_image_urls(payload, request)
                response.content = json.dumps(payload).encode("utf-8")
                if response.has_header("Content-Length"):
                    response["Content-Length"] = str(len(response.content))
            except (TypeError, ValueError, UnicodeDecodeError):
                raise APIException("The server returned an invalid JSON response.")
        return response

    @staticmethod
    def _absolute_image_urls(value, request, key=""):
        if isinstance(value, dict):
            return {
                child_key: LegacyAPIView._absolute_image_urls(
                    child_value, request, child_key.lower()
                )
                for child_key, child_value in value.items()
            }
        elif isinstance(value, list):
            return [
                LegacyAPIView._absolute_image_urls(child, request, key)
                for child in value
            ]
        elif isinstance(value, str) and key in {"image", "logo", "photo"} and value:
            if value.startswith(("http://", "https://", "data:")):
                return value
            return request.build_absolute_uri(value)
        return value

    @extend_schema(responses=OpenApiTypes.ANY)
    def get(self, request, *args, **kwargs):
        return self._invoke(request, *args, **kwargs)

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.ANY)
    def post(self, request, *args, **kwargs):
        return self._invoke(request, *args, **kwargs)

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.ANY)
    def put(self, request, *args, **kwargs):
        return self._invoke(request, *args, **kwargs)

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.ANY)
    def patch(self, request, *args, **kwargs):
        return self._invoke(request, *args, **kwargs)

    @extend_schema(responses=OpenApiTypes.ANY)
    def delete(self, request, *args, **kwargs):
        return self._invoke(request, *args, **kwargs)

    def options(self, request, *args, **kwargs):
        return super().options(request, *args, **kwargs)


def legacy_api(handler, methods, throttle_scope=None, no_auth=False):
    endpoint = type(
        f"{handler.__name__}API",
        (LegacyAPIView,),
        {
            "handler": staticmethod(handler),
            "http_method_names": [method.lower() for method in methods] + ["options"],
            "throttle_scope": throttle_scope,
            "authentication_classes": [] if no_auth else APIView.authentication_classes,
        },
    )
    endpoint = extend_schema_view(
        **{
            method.lower(): extend_schema(
                operation_id=f"{handler.__name__}_{method.lower()}"
            )
            for method in methods
        }
    )(endpoint)
    return endpoint.as_view()


class AuthTokenRefreshView(TokenRefreshView):
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code >= 400:
            return response
        return Response(
            {
                "accessToken": response.data["access"],
                "refreshToken": response.data.get(
                    "refresh", request.data.get("refresh")
                ),
                "tokenType": "Bearer",
            },
            status=response.status_code,
        )


def error_response(message, code=status.HTTP_400_BAD_REQUEST):
    return Response({"detail": str(message)}, status=code)
