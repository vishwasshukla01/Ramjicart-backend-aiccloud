import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes"}


def env_list(name, default=""):
    return [value.strip() for value in os.environ.get(name, default).split(",") if value.strip()]


SECRET_KEY = os.environ.get("SECRET_KEY") or os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY must be configured in the environment.")

DEBUG = env_bool("DEBUG", env_bool("DJANGO_DEBUG", False))
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1"))
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")
CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS", os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "")
)

database_url = os.environ.get("DATABASE") or os.environ.get("DATABASE_URL")
if database_url:
    database_host = urlparse(database_url).hostname
    DATABASES = {
        "default": dj_database_url.parse(
            database_url,
            conn_max_age=600,
            ssl_require=not DEBUG
            and database_host not in {"localhost", "127.0.0.1", "::1"},
        )
    }
elif DEBUG:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    raise ImproperlyConfigured("DATABASE must be set when DEBUG is disabled.")

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
GOOGLE_MAPS_MAP_ID = os.environ.get("GOOGLE_MAPS_MAP_ID", "").strip()
FIREBASE_AUTH_ENABLED = env_bool("FIREBASE_AUTH_ENABLED")
FIREBASE_WEB_CONFIG = {
    "apiKey": os.environ.get("FIREBASE_API_KEY", "").strip(),
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", "").strip(),
    "projectId": os.environ.get("FIREBASE_PROJECT_ID", "").strip(),
    "appId": os.environ.get("FIREBASE_APP_ID", "").strip(),
}
FIREBASE_JS_SDK_VERSION = os.environ.get("FIREBASE_JS_SDK_VERSION", "13.1.0").strip()
FIREBASE_SERVICE_ACCOUNT_FILE = os.environ.get(
    "FIREBASE_SERVICE_ACCOUNT_FILE",
    os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
).strip()
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
if FIREBASE_AUTH_ENABLED:
    missing_firebase_config = [key for key, value in FIREBASE_WEB_CONFIG.items() if not value]
    if missing_firebase_config:
        raise ImproperlyConfigured("Firebase Authentication web configuration is incomplete.")
    if not FIREBASE_SERVICE_ACCOUNT_FILE and not FIREBASE_SERVICE_ACCOUNT_JSON:
        raise ImproperlyConfigured("Firebase requires service-account JSON or a private file.")
    if FIREBASE_SERVICE_ACCOUNT_FILE and not Path(FIREBASE_SERVICE_ACCOUNT_FILE).is_file():
        raise ImproperlyConfigured("FIREBASE_SERVICE_ACCOUNT_FILE does not exist.")

DELIVERY_DISPATCH_RADIUS_KM = float(os.environ.get("DELIVERY_DISPATCH_RADIUS_KM", "10"))
if DELIVERY_DISPATCH_RADIUS_KM <= 0:
    raise ImproperlyConfigured("DELIVERY_DISPATCH_RADIUS_KM must be greater than zero.")

CASHFREE_ENABLED = env_bool("CASHFREE_ENABLED")
CASHFREE_APP_ID = os.environ.get("CASHFREE_APP_ID", "").strip()
CASHFREE_SECRET_KEY = os.environ.get("CASHFREE_SECRET_KEY", "").strip()
CASHFREE_ENVIRONMENT = os.environ.get("CASHFREE_ENVIRONMENT", "sandbox").strip().lower()
CASHFREE_API_VERSION = os.environ.get("CASHFREE_API_VERSION", "2025-01-01").strip()
CASHFREE_RETURN_URL = os.environ.get("CASHFREE_RETURN_URL", "").strip()
CASHFREE_WEBHOOK_URL = os.environ.get("CASHFREE_WEBHOOK_URL", "").strip()
if CASHFREE_ENVIRONMENT not in {"sandbox", "production"}:
    raise ImproperlyConfigured("CASHFREE_ENVIRONMENT must be sandbox or production.")
if CASHFREE_ENABLED and (not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY):
    raise ImproperlyConfigured("Cashfree requires CASHFREE_APP_ID and CASHFREE_SECRET_KEY.")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "axes",
    "orders.apps.OrdersConfig",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 0.25
AXES_USE_ATTEMPT_EXPIRATION = True
AXES_LOCKOUT_PARAMETERS = ["ip_address"]
AXES_IPWARE_META_PRECEDENCE_ORDER = ["HTTP_X_FORWARDED_FOR", "REMOTE_ADDR"]
AXES_IPWARE_PROXY_ORDER = "left-most"
AXES_HTTP_RESPONSE_CODE = 429
AXES_ENABLE_RETRY_AFTER_HEADER = True
AXES_LOCKOUT_CALLABLE = "ramjicart.security.login_lockout_response"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "orders.api.exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "login": "5/minute",
        "register": "10/hour",
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=10),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "UPDATE_LAST_LOGIN": True,
}

CORS_ALLOW_CREDENTIALS = False
CORS_ALLOW_HEADERS = [
    "accept",
    "authorization",
    "content-type",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]

ROOT_URLCONF = "ramjicart.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]
WSGI_APPLICATION = "ramjicart.wsgi.application"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin-allow-popups"

SPECTACULAR_SETTINGS = {
    "TITLE": "DeliveryCart API",
    "DESCRIPTION": "DeliveryCart backend API.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
