from django.http import JsonResponse


def login_lockout_response(request, response, credentials, *args, **kwargs):
    """Return the API's usual JSON shape when Axes temporarily blocks login."""
    return JsonResponse(
        {
            "detail": "Too many failed login attempts. Please wait 15 minutes and try again."
        },
        status=429,
    )
