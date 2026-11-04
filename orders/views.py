import base64
import csv
import hashlib
import hmac
import json
import logging
import math
import secrets
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core.validators import URLValidator
from django.db import transaction
from django.db import IntegrityError
from django.db.models import F, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_http_methods, require_POST
import qrcode

from .models import Category, DailyOrderSequence, Food, Order, OrderItem, PricingSettings, Restaurant
from .models import AccountProfile
from .pricing import money, quote_order


MAX_ORDER_ITEM_QUANTITY = 99
LIVE_LOCATION_MAX_AGE = timedelta(minutes=2)
logger = logging.getLogger(__name__)
@require_GET
def customer_restaurant_detail(request, restaurant_id):
    restaurant = get_object_or_404(Restaurant, id=restaurant_id, approved=True)
    return JsonResponse({
        "id": restaurant.id,
        "name": restaurant.name,
        "address": restaurant.address,
        "city": restaurant.city,
        "pincode": restaurant.pincode,
        "rating": float(restaurant.rating) if restaurant.rating is not None else 4.2,
        "deliveryTime": restaurant.delivery_time or "30–45 min",
        "acceptingOrders": restaurant.accepting_orders,
        "logo": _restaurant_logo_url(restaurant),
    })
def _account_profile(request):
    if not request.user.is_authenticated:
        return None
    try:
        return request.user.account_profile
    except AccountProfile.DoesNotExist:
        return None


def _profile_is_approved(profile):
    if not profile.approved:
        return False
    if profile.role == AccountProfile.Role.RESTAURANT:
        try:
            return profile.user.restaurant.approved
        except Restaurant.DoesNotExist:
            return False
    return True


def _require_role(request, *roles):
    if not request.user.is_authenticated:
        return _error("Please log in first.", 401)
    profile = _account_profile(request)
    if profile is None or profile.role not in roles:
        return _error("Your account is not allowed to perform this action.", 403)
    if profile.role != AccountProfile.Role.CUSTOMER and not _profile_is_approved(profile):
        return _error("Your account is waiting for administrator approval.", 403)
    return None


def _role_redirect(profile):
    if profile.role == AccountProfile.Role.RESTAURANT:
        return "/resturenthomepage.html"
    if profile.role == AccountProfile.Role.DELIVERY_PARTNER:
        return "/resturentdeliverypage.html"
    return "/"


def _json_body(request):
    try:
        value = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("Request body must be valid JSON.")
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object.")
    return value


def _error(message, status=400):
    return JsonResponse({"detail": str(message)}, status=status)


def _token_response(user, profile, **extra):
    refresh = RefreshToken.for_user(user)
    return JsonResponse(
        {
            "accessToken": str(refresh.access_token),
            "refreshToken": str(refresh),
            "tokenType": "Bearer",
            "role": profile.role,
            "redirect": _role_redirect(profile),
            **extra,
        }
    )


class CashfreeAPIError(RuntimeError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


@require_GET
def public_client_config(_request):
    return JsonResponse(
        {
            "googleMapsApiKey": settings.GOOGLE_MAPS_API_KEY,
            "googleMapsMapId": settings.GOOGLE_MAPS_MAP_ID,
            "firebaseEnabled": settings.FIREBASE_AUTH_ENABLED,
            "firebaseSdkVersion": settings.FIREBASE_JS_SDK_VERSION,
            "firebase": (
                settings.FIREBASE_WEB_CONFIG
                if settings.FIREBASE_AUTH_ENABLED
                else None
            ),
            "deliveryDispatchRadiusKm": settings.DELIVERY_DISPATCH_RADIUS_KM,
            "cashfreeEnabled": settings.CASHFREE_ENABLED,
            "cashfreeMode": settings.CASHFREE_ENVIRONMENT,
        }
    )


def _cashfree_api(method, path, payload=None):
    if not settings.CASHFREE_ENABLED:
        raise RuntimeError("Cashfree payments are not enabled.")
    base_url = (
        "https://sandbox.cashfree.com/pg"
        if settings.CASHFREE_ENVIRONMENT == "sandbox"
        else "https://api.cashfree.com/pg"
    )
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    api_request = Request(
        base_url + path,
        data=body,
        method=method,
        headers={
            "x-client-id": settings.CASHFREE_APP_ID,
            "x-client-secret": settings.CASHFREE_SECRET_KEY,
            "x-api-version": settings.CASHFREE_API_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(api_request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        logger.warning("Cashfree API returned HTTP %s: %s", error.code, detail[:1000])
        raise CashfreeAPIError("Cashfree could not start or verify this payment.", error.code) from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        logger.exception("Cashfree API request failed")
        raise RuntimeError("Cashfree is temporarily unavailable. Please try again.") from error


def _mark_cashfree_order_paid(order_id, payment_id, amount, currency="INR"):
    try:
        order = Order.objects.get(cashfree_order_id=order_id)
        paid_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (Order.DoesNotExist, InvalidOperation, TypeError, ValueError):
        return False
    if currency != "INR" or paid_amount != order.gateway_amount:
        logger.warning("Cashfree amount/currency mismatch for order %s", order_id)
        return False
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status in {Order.Status.PLACED, Order.Status.PAID}:
            return "paid" if order.gateway_payment_status == "success" else False
        if order.status not in {
            Order.Status.PAYMENT_PENDING,
            Order.Status.PAYMENT_FAILED,
            Order.Status.CANCELLED,
        }:
            return False
        order.gateway_payment_status = "success"
        order.cashfree_payment_id = str(payment_id or "")[:80]
        order.paid_at = timezone.now()
        late_payment = order.status in {
            Order.Status.PAYMENT_FAILED,
            Order.Status.CANCELLED,
        }
        if not late_payment:
            order.status = Order.Status.PLACED
        order.save(
            update_fields=[
                "status",
                "gateway_payment_status",
                "cashfree_payment_id",
                "paid_at",
            ]
        )
    return "refund" if late_payment else "paid"


def _firebase_identity(data, require_verified):
    if not settings.FIREBASE_AUTH_ENABLED:
        return None, _error("Firebase sign-in is not enabled on this server.", 503)
    token = str(data.get("firebaseIdToken", "")).strip()
    if not token:
        return None, _error("Firebase sign-in token is required.", 401)
    if not settings.FIREBASE_SERVICE_ACCOUNT_FILE and not settings.FIREBASE_SERVICE_ACCOUNT_JSON:
        return None, _error("Firebase server credentials are not configured.", 503)
    try:
        import firebase_admin
        from firebase_admin import auth as firebase_auth
        from firebase_admin import credentials

        try:
            firebase_app = firebase_admin.get_app()
        except ValueError:
            if settings.FIREBASE_SERVICE_ACCOUNT_JSON:
                credential_data = json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON)
                firebase_credential = credentials.Certificate(credential_data)
            else:
                firebase_credential = credentials.Certificate(
                    settings.FIREBASE_SERVICE_ACCOUNT_FILE
                )
            firebase_app = firebase_admin.initialize_app(firebase_credential)

        claims = firebase_auth.verify_id_token(
            token,
            check_revoked=True,
            app=firebase_app,
        )
    except ImportError:
        logger.error("Firebase Admin SDK is not installed.")
        return None, _error("Firebase authentication is unavailable on this server.", 503)
    except Exception:
        logger.exception("Firebase ID-token verification failed.")
        return None, _error("Firebase sign-in could not be verified.", 401)

    email = str(claims.get("email", "")).strip().lower()
    if not claims.get("uid") or not email:
        return None, _error("Firebase account is missing a verified email.", 401)
    if require_verified and not claims.get("email_verified"):
        return None, _error("Verify your email before signing in.", 403)
    return {"uid": claims["uid"], "email": email}, None


def _firebase_registration_identity(data):
    identity, error = _firebase_identity(data, require_verified=False)
    if error:
        return None, error
    supplied_email = str(data.get("email", "")).strip().lower()
    if supplied_email != identity["email"]:
        return None, _error("Firebase email does not match the registration email.", 403)
    return identity, None


def _login_firebase_user(request, identity, expected_role=""):
    user_model = get_user_model()
    profile = AccountProfile.objects.select_related("user").filter(
        firebase_uid=identity["uid"]
    ).first()
    if profile is None:
        user = user_model.objects.filter(email__iexact=identity["email"]).first()
        if user is None:
            return _error("No account is registered for this Firebase email.", 404)
        profile = _account_profile_for_user(user)
        if profile is None:
            return _error("This account is not configured. Contact support.", 403)
        if profile.firebase_uid and profile.firebase_uid != identity["uid"]:
            return _error("This email is linked to a different Firebase account.", 403)
        profile.firebase_uid = identity["uid"]
        profile.save(update_fields=["firebase_uid"])
    else:
        user = profile.user

    if profile.user.email.lower() != identity["email"]:
        return _error("Firebase email does not match the registered account.", 403)
    if expected_role and profile.role != expected_role:
        return _error("This account cannot sign in from this page.", 403)
    if profile.role != AccountProfile.Role.CUSTOMER and not _profile_is_approved(profile):
        return _error(
            "Your account is waiting for administrator approval before login.",
            403,
        )
    return _token_response(user, profile, message="Login successful.")


def _coordinates_from_data(data):
    raw_latitude = data.get("latitude")
    raw_longitude = data.get("longitude")
    if raw_latitude in (None, "") and raw_longitude in (None, ""):
        return None, None
    if raw_latitude in (None, "") or raw_longitude in (None, ""):
        raise ValueError("Provide both latitude and longitude coordinates.")
    try:
        latitude = Decimal(str(raw_latitude))
        longitude = Decimal(str(raw_longitude))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Provide valid latitude and longitude coordinates.")
    if (
        not latitude.is_finite()
        or not longitude.is_finite()
        or not Decimal("-90") <= latitude <= Decimal("90")
        or not Decimal("-180") <= longitude <= Decimal("180")
    ):
        raise ValueError("Coordinates are outside valid ranges.")
    return latitude.quantize(Decimal("0.000001")), longitude.quantize(
        Decimal("0.000001")
    )


def _distance_km(latitude_a, longitude_a, latitude_b, longitude_b):
    latitude_a, longitude_a, latitude_b, longitude_b = (
        math.radians(float(value))
        for value in (latitude_a, longitude_a, latitude_b, longitude_b)
    )
    latitude_delta = latitude_b - latitude_a
    longitude_delta = longitude_b - longitude_a
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude_a)
        * math.cos(latitude_b)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 6371 * 2 * math.asin(math.sqrt(min(1, haversine)))


def _grocery_options(food):
    return food.variants or []


def _order_summary(items):
    item_total = money(
        sum((item[-1] * item[1] for item in items), Decimal("0"))
    )
    restaurant_ids = {item[0].restaurant_id for item in items}
    restaurant = (
        Restaurant.objects.get(id=next(iter(restaurant_ids)))
        if len(restaurant_ids) == 1
        else None
    )
    return item_total, restaurant


@csrf_protect
@require_POST
def auth_register(request):
    try:
        data = _json_body(request)
    except ValueError as error:
        return _error(str(error))
    name = str(data.get("fullname", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    phone = str(data.get("mobile", "")).strip()
    password = str(data.get("password", ""))
    if not name or len(name) > 150 or not phone or len(phone) > 30:
        return _error("Enter a valid name and phone number.")
    try:
        validate_email(email)
    except ValidationError:
        return _error("Enter a valid email address.")
    if len(email) > 150:
        return _error("Email address must be 150 characters or fewer.")
    firebase_identity = None
    if settings.FIREBASE_AUTH_ENABLED:
        firebase_identity, error = _firebase_registration_identity(data)
        if error:
            return error
    if get_user_model().objects.filter(email__iexact=email).exists():
        return _error("An account with this email already exists.", 409)
    user = get_user_model()(username=email, email=email, first_name=name)
    if not firebase_identity:
        try:
            validate_password(password, user=user)
        except ValidationError as error:
            return _error(" ".join(error.messages))
    with transaction.atomic():
        if firebase_identity:
            user.set_unusable_password()
        else:
            user.set_password(password)
        user.save()
        AccountProfile.objects.create(
            user=user,
            role=AccountProfile.Role.CUSTOMER,
            phone=phone,
            firebase_uid=firebase_identity["uid"] if firebase_identity else None,
        )
    return JsonResponse(
        {
            "message": "Registration successful. Please log in.",
            "redirect": "/login.html",
        },
        status=201,
    )


@csrf_protect
@require_POST
def restaurant_register(request):
    try:
        data = _json_body(request)
    except ValueError as error:
        return _error(str(error))
    name = str(data.get("restaurantName", "")).strip()
    owner_name = str(data.get("ownerName", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    phone = str(data.get("phone", "")).strip()
    address = str(data.get("address", "")).strip()
    nearby_landmark = str(data.get("nearbyLandmark", "")).strip()
    market = str(data.get("market", "")).strip()
    city = str(data.get("city", "")).strip()
    pincode = str(data.get("pincode", "")).strip()
    password = str(data.get("password", ""))
    try:
        latitude, longitude = _coordinates_from_data(data)
    except ValueError as error:
        return _error(str(error))
    if (
        not name
        or len(name) > 160
        or not owner_name
        or not phone
        or not address
        or not city
        or not pincode
    ):
        return _error("Complete all restaurant, address, city, PIN code, and owner details.")
    if len(phone) > 30 or len(address) > 2000:
        return _error("Restaurant details are too long.")
    if len(city) > 100 or len(nearby_landmark) > 160 or len(market) > 160:
        return _error("Restaurant location details are too long.")
    if len(pincode) != 6 or not pincode.isdigit():
        return _error("Enter a valid 6-digit restaurant PIN code.")
    try:
        validate_email(email)
    except ValidationError:
        return _error("Enter a valid email address.")
    if len(email) > 150:
        return _error("Email address must be 150 characters or fewer.")
    firebase_identity = None
    if settings.FIREBASE_AUTH_ENABLED:
        firebase_identity, error = _firebase_registration_identity(data)
        if error:
            return error
    if get_user_model().objects.filter(email__iexact=email).exists():
        return _error("An account with this email already exists.", 409)
    user = get_user_model()(username=email, email=email, first_name=owner_name)
    if not firebase_identity:
        try:
            validate_password(password, user=user)
        except ValidationError as error:
            return _error(" ".join(error.messages))
    with transaction.atomic():
        if firebase_identity:
            user.set_unusable_password()
        else:
            user.set_password(password)
        user.save()
        AccountProfile.objects.create(
            user=user,
            role=AccountProfile.Role.RESTAURANT,
            phone=phone,
            firebase_uid=firebase_identity["uid"] if firebase_identity else None,
            approved=False,
        )
        Restaurant.objects.create(
            owner=user,
            name=name,
            owner_name=owner_name,
            email=email,
            phone=phone,
            address=address,
            nearby_landmark=nearby_landmark,
            market=market,
            city=city,
            pincode=pincode,
            latitude=latitude,
            longitude=longitude,
            approved=False,
        )
    return JsonResponse(
        {
            "message": (
                "Restaurant registration submitted and is waiting for admin "
                "approval. Your partnership certificate will be available "
                "after approval."
            ),
            "redirect": "/resturentloginpage.html",
        },
        status=201,
    )


@csrf_protect
@require_POST
def delivery_register(request):
    try:
        data = _json_body(request)
    except ValueError as error:
        return _error(str(error))
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    phone = str(data.get("phone", "")).strip()
    password = str(data.get("password", ""))
    if not name or len(name) > 150 or not phone or len(phone) > 30:
        return _error("Enter a valid name and phone number.")
    try:
        validate_email(email)
    except ValidationError:
        return _error("Enter a valid email address.")
    if len(email) > 150:
        return _error("Email address must be 150 characters or fewer.")
    firebase_identity = None
    if settings.FIREBASE_AUTH_ENABLED:
        firebase_identity, error = _firebase_registration_identity(data)
        if error:
            return error
    if get_user_model().objects.filter(email__iexact=email).exists():
        return _error("An account with this email already exists.", 409)
    user = get_user_model()(username=email, email=email, first_name=name)
    if not firebase_identity:
        try:
            validate_password(password, user=user)
        except ValidationError as error:
            return _error(" ".join(error.messages))
    with transaction.atomic():
        if firebase_identity:
            user.set_unusable_password()
        else:
            user.set_password(password)
        user.save()
        AccountProfile.objects.create(
            user=user,
            role=AccountProfile.Role.DELIVERY_PARTNER,
            phone=phone,
            firebase_uid=firebase_identity["uid"] if firebase_identity else None,
            approved=False,
        )
    return JsonResponse(
        {
            "message": "Delivery partner account created. Admin approval is required before accepting orders.",
            "redirect": "/resturentloginpage.html",
        },
        status=201,
    )


@require_GET
def restaurant_certificate(request):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    if restaurant.certificate_issued_at is None:
        return _error("Your certificate will be available after admin approval.", 403)

    verification_url = request.build_absolute_uri(
        reverse(
            "verify-certificate",
            kwargs={"registration_number": restaurant.registration_number},
        )
    )
    return JsonResponse(
        {
            "restaurantName": restaurant.name,
            "registrationNumber": restaurant.registration_number,
            "issuedOn": timezone.localtime(
                restaurant.certificate_issued_at
            ).date().isoformat(),
            "verificationUrl": verification_url,
            "downloadUrl": request.build_absolute_uri(
                reverse("restaurant-certificate-download")
            ),
        }
    )


@require_GET
def restaurant_certificate_download(request):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    if restaurant.certificate_issued_at is None:
        return _error("Your certificate will be available after admin approval.", 403)

    try:
        from reportlab.lib.colors import HexColor
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.pdfgen import canvas
    except ImportError:
        return _error("Certificate PDF download is temporarily unavailable.", 503)

    pdf_buffer = BytesIO()
    page_width, page_height = landscape(A4)
    pdf = canvas.Canvas(pdf_buffer, pagesize=(page_width, page_height), pageCompression=1)
    pdf.setTitle(f"Ramjicart Partnership Certificate - {restaurant.name}")
    pdf.setAuthor("Ramjicart")

    navy = HexColor("#10233f")
    orange = HexColor("#f36b21")
    cyan = HexColor("#42b9ed")
    muted = HexColor("#718096")
    left, bottom = 22, 22
    frame_width, frame_height = page_width - 44, page_height - 44
    pdf.setFillColor(HexColor("#fffdf9"))
    pdf.rect(left, bottom, frame_width, frame_height, stroke=0, fill=1)
    pdf.setStrokeColor(navy)
    pdf.setLineWidth(6)
    pdf.rect(left, bottom, frame_width, frame_height, stroke=1, fill=0)
    pdf.setStrokeColor(orange)
    pdf.setLineWidth(0.8)
    pdf.rect(left + 9, bottom + 9, frame_width - 18, frame_height - 18, stroke=1, fill=0)

    static_dir = settings.BASE_DIR / "static" / "admin-assets"
    logo_path = static_dir / "logo (2).png"
    photo_path = static_dir / "headshot_with_R_logo_icon.png"
    signature_path = static_dir / "WhatsApp_Image_2026-10-21_at_9.07.20_PM-removebg-preview.png"

    brand_x = page_width / 2 - 91
    pdf.drawImage(ImageReader(str(logo_path)), brand_x, page_height - 76, 46, 46, preserveAspectRatio=True, anchor="c", mask="auto")
    pdf.setFillColor(navy)
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(brand_x + 56, page_height - 52, "RAMJICART")
    pdf.setFillColor(orange)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(brand_x + 56, page_height - 66, "RAMJICART")

    pdf.setFillColor(navy)
    pdf.setFont("Times-Bold", 28)
    pdf.drawCentredString(page_width / 2, page_height - 112, "Certificate of Authorized Partnership")
    pdf.setFillColor(orange)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(page_width / 2, page_height - 130, "R E S T A U R A N T   P A R T N E R   C E R T I F I C A T E")

    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 11)
    pdf.drawCentredString(page_width / 2, page_height - 164, "This is to certify that")
    name = str(restaurant.name).encode("cp1252", "replace").decode("cp1252")
    name_size = 27
    while name_size > 16 and stringWidth(name, "Times-Bold", name_size) > page_width - 160:
        name_size -= 1
    pdf.setFillColor(orange)
    pdf.setFont("Times-Bold", name_size)
    pdf.drawCentredString(page_width / 2, page_height - 196, name)

    paragraph = (
        "is an approved restaurant partner of Ramjicart, the food delivery platform operated by Ramjicart. "
        "This certificate is issued by Vishal Shukla, Chief Executive Officer of Ramjicart."
    )
    words, line, lines = paragraph.split(), "", []
    for word in words:
        candidate = f"{line} {word}".strip()
        if stringWidth(candidate, "Helvetica", 11) > page_width - 190 and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    pdf.setFillColor(navy)
    pdf.setFont("Helvetica", 11)
    for index, paragraph_line in enumerate(lines[:3]):
        pdf.drawCentredString(page_width / 2, page_height - 220 - index * 16, paragraph_line)

    issued_on = timezone.localtime(restaurant.certificate_issued_at).date()
    detail_y, detail_h, detail_w = page_height - 326, 52, 310
    for x, label, value, accent in (
        (88, "REGISTRATION NO.", restaurant.registration_number, orange),
        (page_width - 88 - detail_w, "DATE OF ISSUE", f"{issued_on.day} {issued_on:%B %Y}", cyan),
    ):
        pdf.setFillColor(HexColor("#ffffff"))
        pdf.setStrokeColor(HexColor("#d8e0e9"))
        pdf.roundRect(x, detail_y, detail_w, detail_h, 4, stroke=1, fill=1)
        pdf.setFillColor(accent)
        pdf.rect(x, detail_y, 5, detail_h, stroke=0, fill=1)
        pdf.setFillColor(muted)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(x + 16, detail_y + 33, label)
        pdf.setFillColor(navy)
        pdf.setFont("Helvetica-Bold", 13)
        display_value = str(value).encode("cp1252", "replace").decode("cp1252")
        pdf.drawString(x + 16, detail_y + 14, display_value)

    photo_x, photo_y, photo_size = 78, 82, 70
    pdf.saveState()
    photo_clip = pdf.beginPath()
    photo_clip.ellipse(photo_x, photo_y, photo_x + photo_size, photo_y + photo_size)
    pdf.clipPath(photo_clip, stroke=0, fill=0)
    pdf.drawImage(ImageReader(str(photo_path)), photo_x, photo_y, photo_size, photo_size, preserveAspectRatio=True, anchor="c", mask="auto")
    pdf.restoreState()
    pdf.setStrokeColor(orange)
    pdf.setLineWidth(2)
    pdf.circle(photo_x + photo_size / 2, photo_y + photo_size / 2, photo_size / 2, stroke=1, fill=0)

    signature_x = 160
    pdf.drawImage(ImageReader(str(signature_path)), signature_x, 106, 142, 48, preserveAspectRatio=True, anchor="c", mask="auto")
    pdf.setStrokeColor(navy)
    pdf.setLineWidth(0.8)
    pdf.line(signature_x, 102, signature_x + 142, 102)
    pdf.setFillColor(navy)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(signature_x, 85, "Vishal Shukla")
    pdf.setFillColor(muted)
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawString(signature_x, 73, "CHIEF EXECUTIVE OFFICER - RAMJICART")

    badge_x, badge_y = page_width / 2, 116
    pdf.setFillColor(navy)
    pdf.setStrokeColor(orange)
    pdf.setLineWidth(5)
    pdf.circle(badge_x, badge_y, 37, stroke=1, fill=1)
    pdf.setStrokeColor(HexColor("#42c4f5"))
    pdf.setLineWidth(4)
    pdf.line(badge_x - 17, badge_y - 1, badge_x - 5, badge_y - 13)
    pdf.line(badge_x - 5, badge_y - 13, badge_x + 19, badge_y + 15)
    pdf.setFillColor(orange)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawCentredString(badge_x, badge_y - 53, "VERIFIED")

    verification_url = request.build_absolute_uri(
        reverse("verify-certificate", kwargs={"registration_number": restaurant.registration_number})
    )
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(verification_url)
    qr.make(fit=True)
    qr_buffer = BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(qr_buffer, format="PNG")
    qr_buffer.seek(0)
    qr_size, qr_x, qr_y = 90, page_width - 178, 91
    pdf.drawImage(ImageReader(qr_buffer), qr_x, qr_y, qr_size, qr_size, preserveAspectRatio=True, anchor="c")
    pdf.setFillColor(navy)
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawCentredString(qr_x + qr_size / 2, qr_y - 12, "SCAN TO VERIFY")
    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 7)
    pdf.drawCentredString(qr_x + qr_size / 2, qr_y - 23, "Live certificate verification")

    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 8)
    pdf.drawCentredString(page_width / 2, 43, "Scan the QR code to confirm this certificate's authenticity and registration status.")
    pdf.showPage()
    pdf.save()

    response = HttpResponse(pdf_buffer.getvalue(), content_type="application/pdf")
    safe_registration = "".join(character for character in restaurant.registration_number if character.isalnum() or character in "-_")
    response["Content-Disposition"] = f'attachment; filename="ramjicart-certificate-{safe_registration}.pdf"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def verify_certificate(request, registration_number):
    restaurant = get_object_or_404(
        Restaurant, registration_number=registration_number
    )
    profile = None
    if restaurant.owner_id:
        profile = AccountProfile.objects.filter(
            user_id=restaurant.owner_id,
            role=AccountProfile.Role.RESTAURANT,
        ).first()
    verified = bool(
        restaurant.approved
        and restaurant.certificate_issued_at
        and profile
        and profile.approved
    )
    return JsonResponse(
        {
            "verified": verified,
            "registrationNumber": restaurant.registration_number,
            "restaurantName": restaurant.name,
            "issuedOn": (
                timezone.localtime(restaurant.certificate_issued_at).date().isoformat()
                if restaurant.certificate_issued_at
                else None
            ),
        }
    )


@csrf_protect
@require_POST
def auth_login(request):
    try:
        data = _json_body(request)
    except ValueError as error:
        return _error(str(error))
    if settings.FIREBASE_AUTH_ENABLED and data.get("firebaseIdToken"):
        identity, error = _firebase_identity(data, require_verified=True)
        if error:
            return error
        return _login_firebase_user(
            request,
            identity,
            expected_role=str(data.get("expectedRole", "")).strip(),
        )
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    user = authenticate(request, username=email, password=password)
    if user is None:
        return _error("Email or password is incorrect.", 401)
    profile = _account_profile_for_user(user)
    if profile is None:
        return _error("This account is not configured. Contact support.", 403)
    if settings.FIREBASE_AUTH_ENABLED and profile.firebase_uid:
        return _error(
            "This account uses Firebase sign-in. Use its Firebase password or Google sign-in.",
            401,
        )
    if profile.role != AccountProfile.Role.CUSTOMER and not _profile_is_approved(profile):
        return _error(
            "Your account is waiting for administrator approval before login.",
            403,
        )
    return _token_response(user, profile, message="Login successful.")


def _account_profile_for_user(user):
    try:
        return user.account_profile
    except AccountProfile.DoesNotExist:
        return None


@csrf_protect
@require_POST
def auth_logout(request):
    if not request.user.is_authenticated:
        return _error("Please log in first.", 401)
    try:
        token = str(_json_body(request).get("refreshToken", "")).strip()
        if not token:
            return _error("A refresh token is required.")
        RefreshToken(token).blacklist()
    except TokenError:
        return _error("Refresh token is invalid or has already been revoked.", 401)
    except ValueError as error:
        return _error(str(error))
    return JsonResponse({"message": "Logged out."})


@require_GET
def auth_me(request):
    if not request.user.is_authenticated:
        return _error("Please log in first.", 401)
    profile = _account_profile(request)
    if profile is None:
        return _error("Account profile is not configured.", 403)
    return JsonResponse(
        {
            "name": request.user.first_name or request.user.email,
            "email": request.user.email,
            "phone": profile.phone,
            "role": profile.role,
            "approved": profile.approved,
        }
    )


@require_GET
def categories(_request):
    categories = Category.objects.filter(is_active=True).order_by("sort_order", "name").values("id", "name", "slug", "icon", "color", "is_active", "sort_order")
    data = [
        category
        for category in categories
        if not _is_non_veg_category(category["name"])
    ]
    return JsonResponse(data, safe=False)


@require_GET
def category_detail(_request, slug):
    category = get_object_or_404(Category, slug=slug, is_active=True)
    foods = Food.objects.filter(
        category=category, available=True, restaurant__approved=True,
    ).select_related("restaurant", "category").order_by("restaurant__name", "name")
    restaurants = {}
    for food in foods:
        restaurant = food.restaurant
        entry = restaurants.setdefault(restaurant.id, {
            "id": restaurant.id, "name": restaurant.name,
            "photo": _restaurant_logo_url(restaurant),
            "open": restaurant.accepts_orders_now(),
            "deliveryTime": restaurant.delivery_time,
            "rating": float(restaurant.rating) if restaurant.rating is not None else None,
            "items": [],
        })
        entry["items"].append({
            "id": food.id, "name": food.name, "description": food.description,
            "photo": food.image, "price": float(food.price),
            "variants": _grocery_options(food),
            "addons": food.addons or [],
            "quantityUnit": food.quantity_unit,
            "veg": not _is_non_veg_category(category.name),
        })
    return JsonResponse({
        "category": {"id": category.id, "name": category.name, "slug": category.slug,
                      "icon": category.icon, "color": category.color,
                      "isActive": category.is_active, "sortOrder": category.sort_order},
        "shops": list(restaurants.values()),
    })


def _non_veg_category_ids():
    return [
        category_id
        for category_id, name in Category.objects.values_list("id", "name")
        if _is_non_veg_category(name)
    ]


def _is_non_veg_category(name):
    normalized = "".join(character for character in str(name).lower() if character.isalpha())
    return normalized.startswith("nonveg") or normalized.startswith("nonvegetarian")


def _order_public_number(order):
    return str(order.id).split("-")[0].upper()


def _accepting_restaurant_ids():
    return [
        restaurant.id
        for restaurant in Restaurant.objects.filter(approved=True, accepting_orders=True)
        if restaurant.accepts_orders_now()
    ]


@require_GET
def popular_foods(_request):
    confirmed_statuses = [
        Order.Status.PLACED,
        Order.Status.PAID,
        Order.Status.ACCEPTED,
        Order.Status.PREPARING,
        Order.Status.READY_FOR_PICKUP,
        Order.Status.ASSIGNED,
        Order.Status.PICKED_UP,
        Order.Status.OUT_FOR_DELIVERY,
        Order.Status.COMPLETED,
    ]
    base_query = Food.objects.filter(
        available=True,
        restaurant__approved=True,
        restaurant_id__in=_accepting_restaurant_ids(),
    ).filter(Q(stock_quantity__isnull=True) | Q(stock_quantity__gt=0)).exclude(category_id__in=_non_veg_category_ids()).select_related(
        "restaurant", "category"
    )
    ranked_foods = list(
        base_query.annotate(
            order_count=Sum(
                "orderitem__quantity",
                filter=Q(orderitem__order__status__in=confirmed_statuses),
            )
        )
        .filter(order_count__gt=0)
        .order_by("-order_count", "name")[:4]
    )
    foods = ranked_foods
    if len(foods) < 4:
        ranked_ids = [food.id for food in foods]
        foods += list(
            base_query.exclude(id__in=ranked_ids)
            .order_by("name")[: 4 - len(foods)]
        )

    return JsonResponse(
        [
            {
                "id": food.id,
                "name": food.name,
                "description": food.description,
                "image": food.image,
                "price": float(food.price),
                "restaurantId": food.restaurant_id,
                "restaurantName": food.restaurant.name,
                "categoryId": food.category_id,
                "orderCount": int(getattr(food, "order_count", 0) or 0),
                "variants": _grocery_options(food),
                "addons": food.addons or [],
            }
            for food in foods
        ],
        safe=False,
    )


@require_GET
def customer_restaurants(request):
    city = request.GET.get("city", "").strip()
    pincode = request.GET.get("pincode", "").strip()
    nearby_landmark = request.GET.get("nearbyLandmark", "").strip()
    market = request.GET.get("market", "").strip()
    try:
        latitude, longitude = _coordinates_from_data(request.GET)
    except ValueError as error:
        return _error(str(error))
    has_coordinates = latitude is not None
    if not has_coordinates and (not city or not pincode):
        return _error(
            "Share your location or enter your city and 6-digit pincode."
        )
    if pincode and (not pincode.isdigit() or len(pincode) != 6):
        return _error("Enter a valid 6-digit pincode.")
    if len(city) > 100 or len(nearby_landmark) > 160 or len(market) > 160:
        return _error("Location details are too long.")

    restaurants = Restaurant.objects.filter(
        approved=True,
        id__in=_accepting_restaurant_ids(),
        foods__available=True,
    ).filter(Q(foods__stock_quantity__isnull=True) | Q(foods__stock_quantity__gt=0)).distinct().prefetch_related("foods__category")
    if not has_coordinates:
        restaurants = restaurants.filter(
            Q(city__iexact=city) | Q(pincode=pincode)
        )

    restaurant_list = list(restaurants)

    def restaurant_distance(restaurant):
        if (
            has_coordinates
            and restaurant.latitude is not None
            and restaurant.longitude is not None
        ):
            return _distance_km(
                latitude,
                longitude,
                restaurant.latitude,
                restaurant.longitude,
            )
        return None

    def location_match(restaurant):
        landmark_match = bool(
            nearby_landmark
            and restaurant.nearby_landmark.casefold()
            == nearby_landmark.casefold()
        )
        market_match = bool(
            market and restaurant.market.casefold() == market.casefold()
        )
        pincode_match = bool(pincode and restaurant.pincode == pincode)
        city_match = bool(city and restaurant.city.casefold() == city.casefold())
        return landmark_match, market_match, pincode_match, city_match

    def location_sort_key(restaurant):
        distance = restaurant_distance(restaurant)
        if distance is not None:
            return 0, distance, restaurant.name.casefold()
        matches = location_match(restaurant)
        supplied = (nearby_landmark, market, pincode, city)
        priority = tuple(
            -int(match)
            for match, value in zip(matches, supplied)
            if value
        )
        return (1, *priority, restaurant.name.casefold())

    restaurant_list.sort(key=location_sort_key)

    return JsonResponse(
        {
            "restaurants": [
                {
                    "proximityMatch": (
                        f"{restaurant_distance(restaurant):.1f} km away"
                        if restaurant_distance(restaurant) is not None
                        else "Nearby landmark"
                        if location_match(restaurant)[0]
                        else "Same market"
                        if location_match(restaurant)[1]
                        else "Same PIN code"
                        if location_match(restaurant)[2]
                        else "Same city"
                        if location_match(restaurant)[3]
                        else "Location not set"
                    ),
                    "id": restaurant.id,
                    "name": restaurant.name,
                    "address": restaurant.address,
                    "city": restaurant.city,
                    "pincode": restaurant.pincode,
                    "nearbyLandmark": restaurant.nearby_landmark,
                    "market": restaurant.market,
                    "latitude": (
                        float(restaurant.latitude)
                        if restaurant.latitude is not None
                        else None
                    ),
                    "longitude": (
                        float(restaurant.longitude)
                        if restaurant.longitude is not None
                        else None
                    ),
                    "distanceKm": (
                        round(restaurant_distance(restaurant), 2)
                        if restaurant_distance(restaurant) is not None
                        else None
                    ),
                    "logo": _restaurant_logo_url(restaurant),
                    "categories": sorted(
                        {
                            food.category.name
                            for food in restaurant.foods.all()
                            if food.available
                            and food.category
                            and not _is_non_veg_category(food.category.name)
                        }
                    ),
                }
                for restaurant in restaurant_list
            ]
        }
    )


@require_GET
def foods(request):
    queryset = Food.objects.filter(
        available=True,
        restaurant__approved=True,
        restaurant_id__in=_accepting_restaurant_ids(),
    ).filter(Q(stock_quantity__isnull=True) | Q(stock_quantity__gt=0)).exclude(category_id__in=_non_veg_category_ids()).select_related(
        "restaurant", "category"
    )
    try:
        latitude, longitude = _coordinates_from_data(request.GET)
    except ValueError as error:
        return _error(str(error))
    has_coordinates = latitude is not None
    category_id = request.GET.get("categoryId")
    if category_id:
        try:
            category_id = int(category_id)
        except ValueError:
            return _error("categoryId must be a number.")
        queryset = queryset.filter(category_id=category_id)
    restaurant_id = request.GET.get("restaurantId")
    if restaurant_id:
        try:
            restaurant_id = int(restaurant_id)
        except ValueError:
            return _error("restaurantId must be a number.")
        queryset = queryset.filter(restaurant_id=restaurant_id)
    search = request.GET.get("search", "").strip()
    if search:
        queryset = queryset.filter(
            Q(name__icontains=search)
            | Q(description__icontains=search)
            | Q(restaurant__name__icontains=search)
        )
    food_list = list(queryset)
    if has_coordinates:
        food_list.sort(
            key=lambda food: (
                0
                if food.restaurant.latitude is not None
                and food.restaurant.longitude is not None
                else 1,
                _distance_km(
                    latitude,
                    longitude,
                    food.restaurant.latitude,
                    food.restaurant.longitude,
                )
                if food.restaurant.latitude is not None
                and food.restaurant.longitude is not None
                else 0,
                food.name.casefold(),
            )
        )
    data = [
        {
            "id": food.id,
            "name": food.name,
            "description": food.description,
            "image": food.image,
            "price": float(food.price),
            "variants": _grocery_options(food),
            "addons": food.addons or [],
            "quantityUnit": food.quantity_unit,
            "restaurantId": food.restaurant_id,
            "restaurantName": food.restaurant.name,
            "categoryId": food.category_id,
            "restaurantLatitude": (
                float(food.restaurant.latitude)
                if food.restaurant.latitude is not None
                else None
            ),
            "restaurantLongitude": (
                float(food.restaurant.longitude)
                if food.restaurant.longitude is not None
                else None
            ),
            "restaurantDistanceKm": (
                round(
                    _distance_km(
                        latitude,
                        longitude,
                        food.restaurant.latitude,
                        food.restaurant.longitude,
                    ),
                    2,
                )
                if has_coordinates
                and food.restaurant.latitude is not None
                and food.restaurant.longitude is not None
                else None
            ),
        }
        for food in food_list
    ]
    return JsonResponse(data, safe=False)


def _order_items(data):
    submitted_items = data.get("items")
    if not isinstance(submitted_items, list) or not 1 <= len(submitted_items) <= 50:
        raise ValueError("Provide between 1 and 50 order items.")

    items = []
    restaurant_id = None
    requested_quantities = {}
    for submitted in submitted_items:
        if not isinstance(submitted, dict):
            raise ValueError("Each item must be an object.")
        try:
            food_id = int(submitted.get("foodId", submitted.get("itemId", submitted.get("id"))))
            quantity = Decimal(str(submitted.get("quantity")))
        except (TypeError, ValueError, InvalidOperation):
            raise ValueError("Each item needs a valid foodId and quantity.")
        if (
            food_id <= 0
            or not quantity.is_finite()
            or quantity != quantity.quantize(Decimal("0.01"))
            or not Decimal("0.05") <= quantity <= MAX_ORDER_ITEM_QUANTITY
        ):
            raise ValueError("Item quantities must be between 0.05 and 99.")

        food = Food.objects.filter(
            id=food_id,
            available=True,
            restaurant__approved=True,
            restaurant_id__in=_accepting_restaurant_ids(),
        ).filter(Q(stock_quantity__isnull=True) | Q(stock_quantity__gt=0)).exclude(
            category_id__in=_non_veg_category_ids()
        ).select_related("restaurant", "category").first()
        if food is None:
            raise ValidationError("An item is unavailable.")
        if restaurant_id is not None and restaurant_id != food.restaurant_id:
            raise ValidationError("An order can contain items from only one restaurant.")
        restaurant_id = food.restaurant_id
        if food.stock_quantity is not None and quantity > food.stock_quantity:
            raise ValidationError(f"Only {food.stock_quantity} item(s) are currently in stock.")
        if food.quantity_unit == "piece" and quantity != quantity.to_integral_value():
            raise ValidationError("Piece quantities must be whole numbers.")

        variant_id = str(submitted.get("variantId", "")).strip()
        variants = food.variants or []
        variant = next(
            (option for option in variants if str(option.get("id", "")) == variant_id),
            None,
        ) if variant_id else None
        if variant_id and variant is None:
            raise ValidationError("Choose a valid item variant.")
        if variants and not variant_id:
            raise ValidationError("Choose an item variant.")
        variant_name = str(variant.get("name", "")) if variant else ""
        unit_price = Decimal(str(variant["price"])) if variant else food.price

        addon_ids = submitted.get("addonIds", [])
        if not isinstance(addon_ids, list) or len(addon_ids) > 20:
            raise ValueError("addonIds must be a list of at most 20 ids.")
        addon_ids = [str(addon_id).strip() for addon_id in addon_ids]
        if len(set(addon_ids)) != len(addon_ids):
            raise ValueError("An add-on may only be selected once.")
        available_addons = {
            str(option.get("id", "")): option for option in (food.addons or [])
        }
        if any(addon_id not in available_addons for addon_id in addon_ids):
            raise ValidationError("An add-on is unavailable.")
        selected_addons = [
            {
                "id": addon_id,
                "name": available_addons[addon_id]["name"],
                "price": str(money(available_addons[addon_id]["price"])),
            }
            for addon_id in addon_ids
        ]
        unit_price = money(
            unit_price
            + sum((Decimal(addon["price"]) for addon in selected_addons), Decimal("0"))
        )
        items.append(
            (food, quantity, variant_name, variant_id, selected_addons, unit_price)
        )
        requested_quantities[food.pk] = requested_quantities.get(
            food.pk, Decimal("0")
        ) + quantity

    for food_id, quantity in requested_quantities.items():
        food = next(item[0] for item in items if item[0].pk == food_id)
        if food.stock_quantity is not None and quantity > food.stock_quantity:
            raise ValidationError(f"Only {food.stock_quantity} item(s) are currently in stock.")

    return items


def _quote_for_items(data, order_type):
    items = _order_items(data)
    if not items:
        raise ValidationError("Your order is empty or contains unavailable food.")
    item_total, restaurant = _order_summary(items)
    if restaurant is None:
        raise ValidationError("An order can contain food from only one restaurant.")
    return items, restaurant, quote_order(restaurant, item_total, order_type)


@csrf_protect
@require_POST
def checkout_quote(request):
    role_error = _require_role(request, AccountProfile.Role.CUSTOMER)
    if role_error:
        return role_error
    try:
        data = _json_body(request)
        order_type = data.get("orderType", "")
        _, _, quote = _quote_for_items(data, order_type)
    except ValueError as error:
        return _error(str(error))
    except ValidationError as error:
        return _error("; ".join(error.messages))
    except PricingSettings.DoesNotExist:
        return _error("Marketplace pricing is not configured.", 503)
    return JsonResponse({key: float(value) if isinstance(value, Decimal) else value
                         for key, value in quote.items()})


@csrf_protect
@require_POST
def create_order(request):
    role_error = _require_role(request, AccountProfile.Role.CUSTOMER)
    if role_error:
        return role_error
    try:
        data = _json_body(request)
        order_type = data.get("orderType", "")
        items, restaurant, quote = _quote_for_items(data, order_type)
    except ValueError as error:
        return _error(str(error))
    except ValidationError as error:
        return _error("; ".join(error.messages))
    except PricingSettings.DoesNotExist:
        return _error("Pricing could not be loaded. Please try again.", 503)

    delivery_address = str(data.get("deliveryAddress", "")).strip()
    try:
        delivery_latitude, delivery_longitude = _coordinates_from_data(data)
    except ValueError as error:
        return _error(str(error))
    profile = _account_profile(request)
    customer_name = request.user.get_full_name().strip() or request.user.email
    customer_phone = profile.phone
    customer_email = request.user.email
    if not customer_phone:
        return _error("Add a phone number to your account before ordering.")
    if order_type == Order.OrderType.DELIVERY and not delivery_address:
        return _error("A delivery address is required for delivery orders.")
    if len(delivery_address) > 2000:
        return _error("Delivery address is too long.")
    confirmation_code = f"{secrets.randbelow(10000):04d}"

    try:
        with transaction.atomic():
            quantity_by_food = {}
            for food, quantity, *_options in items:
                quantity_by_food[food.pk] = quantity_by_food.get(
                    food.pk, Decimal("0")
                ) + quantity
            locked_foods = {}
            for food_id, requested_quantity in quantity_by_food.items():
                locked_food = Food.objects.select_for_update().get(pk=food_id)
                if not locked_food.available or locked_food.stock_quantity == 0 or (
                    locked_food.stock_quantity is not None
                    and locked_food.stock_quantity < requested_quantity
                ):
                    raise ValidationError(f"{locked_food.name} no longer has enough stock.")
                locked_foods[food_id] = locked_food
            locked_items = [
                (locked_foods[food.pk], quantity, variant_name, variant_id, addons, unit_price)
                for food, quantity, variant_name, variant_id, addons, unit_price in items
            ]
            order_day = timezone.localdate()
            try:
                sequence = DailyOrderSequence.objects.select_for_update().get(day=order_day)
            except DailyOrderSequence.DoesNotExist:
                try:
                    with transaction.atomic():
                        sequence = DailyOrderSequence.objects.create(day=order_day, last_number=0)
                except IntegrityError:
                    sequence = DailyOrderSequence.objects.select_for_update().get(day=order_day)
            sequence.last_number += 1
            sequence.save(update_fields=["last_number"])
            order = Order.objects.create(
                order_day=order_day,
                daily_order_number=sequence.last_number,
                customer=request.user,
                customer_name=customer_name,
                customer_email=customer_email,
                customer_phone=customer_phone,
                delivery_address=delivery_address,
                delivery_latitude=(
                    delivery_latitude
                    if order_type == Order.OrderType.DELIVERY
                    else None
                ),
                delivery_longitude=(
                    delivery_longitude
                    if order_type == Order.OrderType.DELIVERY
                    else None
                ),
                restaurant=restaurant,
                order_type=order_type,
                item_total=quote["itemTotal"],
                commission_percentage=quote["commissionPercentage"],
                commission_amount=quote["commissionAmount"],
                delivery_fee=quote["deliveryFee"],
                platform_fee=quote["platformFee"],
                customer_total=quote["customerTotal"],
                restaurant_payout=quote["restaurantPayout"],
                gateway_amount=quote["gatewayAmount"],
                confirmation_code=confirmation_code,
                status=(
                    Order.Status.PAYMENT_PENDING
                    if settings.CASHFREE_ENABLED
                    else Order.Status.PLACED
                ),
                gateway_payment_status=(
                    "pending" if settings.CASHFREE_ENABLED else "not_required"
                ),
            )
            OrderItem.objects.bulk_create(
                [
                    OrderItem(
                        order=order,
                        food=food,
                        food_name=(f"{food.name} ({variant_name})" if variant_name else food.name)[:160],
                        variant_name=variant_name,
                        variant_id=variant_id,
                        addons=addons,
                        unit_price=unit_price,
                        quantity=quantity,
                        line_total=money(unit_price * quantity),
                    )
                    for food, quantity, variant_name, variant_id, addons, unit_price in locked_items
                ]
            )
            for food_id, quantity in quantity_by_food.items():
                food = locked_foods[food_id]
                if food.stock_quantity is not None:
                    food.stock_quantity -= quantity
                    if food.stock_quantity == 0:
                        food.available = False
                    food.save(update_fields=["stock_quantity", "available"])
    except ValidationError as error:
        return _error(" ".join(error.messages), 409)

    payment_session_id = ""
    if settings.CASHFREE_ENABLED:
        # Use the local UUID as Cashfree's order ID so webhook/return lookups
        # are unambiguous and retries never create a second local order.
        order.cashfree_order_id = str(order.id)
        order.save(update_fields=["cashfree_order_id"])
        return_url = settings.CASHFREE_RETURN_URL or (
            request.build_absolute_uri("/orderpage.html")
            + "?id={order_id}&payment=return"
        )
        webhook_url = settings.CASHFREE_WEBHOOK_URL or request.build_absolute_uri(
            reverse("cashfree-webhook")
        )
        try:
            cashfree_order = _cashfree_api(
                "POST",
                "/orders",
                {
                    "order_id": str(order.id),
                    "order_amount": float(order.gateway_amount),
                    "order_currency": "INR",
                    "customer_details": {
                        "customer_id": f"ramjicart_{request.user.pk}",
                        "customer_name": customer_name[:100],
                        "customer_email": customer_email,
                        "customer_phone": customer_phone,
                    },
                    "order_meta": {
                        "return_url": return_url,
                        "notify_url": webhook_url,
                    },
                    "order_note": f"Ramjicart order {str(order.id)[:8]}",
                },
            )
        except RuntimeError as error:
            order.gateway_payment_status = "error"
            order.status = Order.Status.PAYMENT_FAILED
            order.save(update_fields=["gateway_payment_status", "status"])
            _restore_order_stock(order)
            return _error(str(error), 502)
        payment_session_id = str(cashfree_order.get("payment_session_id", ""))
        if not payment_session_id:
            logger.error("Cashfree create order response has no payment session ID")
            order.gateway_payment_status = "error"
            order.status = Order.Status.PAYMENT_FAILED
            order.save(update_fields=["gateway_payment_status", "status"])
            _restore_order_stock(order)
            return _error("Cashfree did not return a payment session. Please try again.", 502)
        order.cashfree_payment_session_id = payment_session_id
        order.save(update_fields=["cashfree_payment_session_id"])

    return JsonResponse(
        {
            "orderId": str(order.id),
            "orderNumber": _order_public_number(order),
            "dailyOrderNumber": order.daily_order_number,
            "customerName": customer_name,
            "customerEmail": customer_email,
            "customerPhone": customer_phone,
            "confirmationCode": "" if settings.CASHFREE_ENABLED else confirmation_code,
            "status": order.status,
            "paymentRequired": settings.CASHFREE_ENABLED,
            "paymentSessionId": payment_session_id,
            "cashfreeMode": settings.CASHFREE_ENVIRONMENT,
            "quote": {key: float(value) if isinstance(value, Decimal) else value
                      for key, value in quote.items()},
        },
        status=201,
    )


@require_GET
def order_detail(request, order_id):
    role_error = _require_role(request, AccountProfile.Role.CUSTOMER)
    if role_error:
        return role_error
    order = get_object_or_404(Order, id=order_id, customer=request.user)
    order = (
        Order.objects.select_related("restaurant", "delivery_partner")
        .prefetch_related("items")
        .get(id=order.id)
    )
    data = {
        "id": str(order.id),
        "orderNumber": _order_public_number(order),
        "dailyOrderNumber": order.daily_order_number,
        "status": order.status,
        "rejectionReason": order.rejection_reason,
        "preparingMinutes": order.preparing_minutes,
        "refundStatus": order.refund_status,
        "statusText": {
            Order.Status.PLACED: "Order sent to restaurant",
            Order.Status.PAYMENT_PENDING: "Waiting for payment",
            Order.Status.PAID: "Order confirmed",
            Order.Status.ACCEPTED: "Restaurant accepted your order",
            Order.Status.PREPARING: "Your food is being prepared",
            Order.Status.READY_FOR_PICKUP: "Ready for pickup",
            Order.Status.ASSIGNED: "Delivery partner assigned",
            Order.Status.PICKED_UP: "Picked up",
            Order.Status.OUT_FOR_DELIVERY: "Your food is on the way!",
            Order.Status.COMPLETED: "Order delivered",
            Order.Status.CANCELLED: "Order cancelled",
            Order.Status.PAYMENT_FAILED: "Payment failed",
        }[order.status],
        "orderType": order.order_type,
        "deliveryAddress": order.delivery_address,
        "deliveryCoordinates": (
            {
                "latitude": float(order.delivery_latitude),
                "longitude": float(order.delivery_longitude),
            }
            if order.delivery_latitude is not None
            and order.delivery_longitude is not None
            else None
        ),
        "itemTotal": float(order.item_total),
        "commission": float(order.commission_amount),
        "deliveryFee": float(order.delivery_fee),
        "platformFee": float(order.platform_fee),
        "discount": 0,
        "restaurantPayout": float(order.restaurant_payout),
        "total": float(order.customer_total),
        "confirmationCode": (
            order.confirmation_code
            if order.status not in {
                Order.Status.PAYMENT_PENDING,
                Order.Status.PAYMENT_FAILED,
            }
            else ""
        ),
        "orderTypeText": "Pickup (customer is on the way)" if order.order_type == "pickup" else "Home delivery",
        "paymentMethod": "Pay at restaurant" if order.order_type == "pickup" else "Pay on delivery",
        "estimatedArrival": "Pickup" if order.order_type == "pickup" else "To be confirmed",
        "timeline": _order_timeline(order.status),
        "restaurant": {
            "id": order.restaurant_id,
            "name": order.restaurant.name,
            "details": order.restaurant.address,
            "image": _restaurant_logo_url(order.restaurant),
            "location": (
                {
                    "latitude": float(order.restaurant.latitude),
                    "longitude": float(order.restaurant.longitude),
                }
                if order.restaurant.latitude is not None
                and order.restaurant.longitude is not None
                else None
            ),
        },
        "deliveryLocation": (
            _latest_delivery_location(order.delivery_partner)
            if order.delivery_partner_id
            and order.status in {
                Order.Status.ASSIGNED,
                Order.Status.PICKED_UP,
                Order.Status.OUT_FOR_DELIVERY,
            }
            else None
        ),
        "items": [
            {
                "name": item.food_name,
                "price": float(item.unit_price),
                "quantity": float(item.quantity),
                "image": item.food.image,
                "variantId": item.variant_id,
                "addons": item.addons,
            }
            for item in order.items.all()
        ],
    }
    return JsonResponse(data)


@require_GET
def cashfree_payment_status(request, order_id):
    role_error = _require_role(request, AccountProfile.Role.CUSTOMER)
    if role_error:
        return role_error
    order = get_object_or_404(Order, id=order_id, customer=request.user)
    if not settings.CASHFREE_ENABLED:
        return JsonResponse({"enabled": False, "status": order.gateway_payment_status})
    if order.status in {Order.Status.PLACED, Order.Status.PAID}:
        return JsonResponse({"enabled": True, "status": "success", "orderStatus": order.status})
    if not order.cashfree_order_id:
        return _error("Cashfree order was not initialized.", 409)
    try:
        cashfree_order = _cashfree_api(
            "GET", f"/orders/{order.cashfree_order_id}"
        )
        if cashfree_order.get("order_status") == "PAID":
            payments = _cashfree_api(
                "GET", f"/orders/{order.cashfree_order_id}/payments"
            )
            for payment in payments if isinstance(payments, list) else []:
                if payment.get("payment_status") != "SUCCESS":
                    continue
                payment_result = _mark_cashfree_order_paid(
                    order.cashfree_order_id,
                    payment.get("cf_payment_id"),
                    payment.get("payment_amount"),
                    payment.get("payment_currency", "INR"),
                )
                if payment_result == "refund":
                    late_order = Order.objects.get(pk=order.pk)
                    _request_order_refund(late_order)
                    late_order.refresh_from_db()
                    return JsonResponse({
                        "enabled": True,
                        "status": "refund_pending",
                        "orderStatus": late_order.status,
                        "refundStatus": late_order.refund_status,
                    })
                if payment_result == "paid":
                    return JsonResponse({
                        "enabled": True,
                        "status": "success",
                        "orderStatus": Order.Status.PLACED,
                    })
        cashfree_status = str(cashfree_order.get("order_status", "pending")).lower()
        if cashfree_status in {"expired", "terminated"}:
            with transaction.atomic():
                failed_order = Order.objects.select_for_update().filter(
                    pk=order.pk, status=Order.Status.PAYMENT_PENDING
                ).first()
                if failed_order:
                    failed_order.status = Order.Status.PAYMENT_FAILED
                    failed_order.gateway_payment_status = cashfree_status
                    failed_order.save(update_fields=["status", "gateway_payment_status"])
                    _restore_order_stock(failed_order)
            return JsonResponse({
                "enabled": True,
                "status": "failed",
                "orderStatus": Order.Status.PAYMENT_FAILED,
            })
        return JsonResponse({
            "enabled": True,
            "status": cashfree_status,
            "orderStatus": order.status,
            "paymentSessionId": (
                order.cashfree_payment_session_id
                if cashfree_status == "active"
                else ""
            ),
        })
    except RuntimeError as error:
        return _error(str(error), 502)


@csrf_exempt
@require_POST
def cashfree_webhook(request):
    if not settings.CASHFREE_ENABLED:
        return _error("Cashfree payments are not enabled.", 503)
    timestamp = request.headers.get("x-webhook-timestamp", "")
    signature = request.headers.get("x-webhook-signature", "")
    signed_payload = timestamp.encode("utf-8") + request.body
    expected = base64.b64encode(
        hmac.new(
            settings.CASHFREE_SECRET_KEY.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    if not timestamp or not signature or not hmac.compare_digest(expected, signature):
        return _error("Invalid Cashfree webhook signature.", 401)
    try:
        payload = json.loads(request.body.decode("utf-8"))
        data = payload.get("data", {})
        order_data = data.get("order", {})
        payment_data = data.get("payment", {})
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return _error("Invalid Cashfree webhook payload.")
    if payment_data.get("payment_status") == "SUCCESS":
        payment_result = _mark_cashfree_order_paid(
            order_data.get("order_id"),
            payment_data.get("cf_payment_id"),
            payment_data.get("payment_amount"),
            payment_data.get("payment_currency", "INR"),
        )
        if not payment_result:
            return _error("Cashfree payment could not be matched to an order.", 409)
        if payment_result == "refund":
            late_order = Order.objects.get(cashfree_order_id=order_data.get("order_id"))
            _request_order_refund(late_order)
    return JsonResponse({"received": True})


def _restaurant_logo_url(restaurant):
    return restaurant.logo.url if restaurant.logo else ""


def _restaurant_for_request(request):
    role_error = _require_role(request, AccountProfile.Role.RESTAURANT)
    if role_error:
        return None, role_error
    try:
        restaurant = request.user.restaurant
        if not restaurant.approved:
            return None, _error("Restaurant is waiting for administrator approval.", 403)
        return restaurant, None
    except Restaurant.DoesNotExist:
        return None, _error("Restaurant profile is not configured.", 404)


def _restaurant_order_data(order):
    display_status = {
        Order.Status.PLACED: "pending",
        Order.Status.ACCEPTED: "confirmed",
        Order.Status.PREPARING: "preparing",
        Order.Status.READY_FOR_PICKUP: "ready",
        Order.Status.ASSIGNED: "assigned",
        Order.Status.PICKED_UP: "picked_up",
        Order.Status.OUT_FOR_DELIVERY: "on_the_way",
        Order.Status.COMPLETED: "completed",
        Order.Status.CANCELLED: "cancelled",
        Order.Status.PAYMENT_PENDING: "pending",
        Order.Status.PAID: "confirmed",
        Order.Status.PAYMENT_FAILED: "cancelled",
    }[order.status]
    return {
        "id": str(order.id),
        "orderNumber": _order_public_number(order),
        "dailyOrderNumber": order.daily_order_number,
        "date": timezone.localtime(order.created_at).strftime("%d %b %Y, %I:%M %p"),
        "status": display_status,
        "rejectionReason": order.rejection_reason,
        "preparingMinutes": order.preparing_minutes,
        "acceptedAt": order.accepted_at.isoformat() if order.accepted_at else None,
        "refundStatus": order.refund_status,
        "orderType": order.order_type,
        "orderTypeText": "Pickup (customer is on the way)" if order.order_type == "pickup" else "Delivery",
        "customerName": order.customer_name,
        "customerPhone": order.customer_phone,
        "address": order.delivery_address or "Customer will collect at restaurant",
        "restaurantLocation": (
            {
                "latitude": float(order.restaurant.latitude),
                "longitude": float(order.restaurant.longitude),
            }
            if order.restaurant.latitude is not None
            and order.restaurant.longitude is not None
            else None
        ),
        "deliveryCoordinates": (
            {
                "latitude": float(order.delivery_latitude),
                "longitude": float(order.delivery_longitude),
            }
            if order.delivery_latitude is not None
            and order.delivery_longitude is not None
            else None
        ),
        "deliveryLatitude": (
            float(order.delivery_latitude)
            if order.delivery_latitude is not None
            else None
        ),
        "deliveryLongitude": (
            float(order.delivery_longitude)
            if order.delivery_longitude is not None
            else None
        ),
        "paymentMethod": "Pay at restaurant" if order.order_type == "pickup" else "Pay on delivery",
        "total": float(order.customer_total),
        "items": [
            {
                "name": item.food_name,
                "quantity": float(item.quantity),
                "price": float(item.line_total),
                "variantId": item.variant_id,
                "addons": item.addons,
            }
            for item in order.items.all()
        ],
    }


def _restore_order_stock(order):
    quantities = (
        order.items.filter(food__stock_quantity__isnull=False)
        .values("food_id")
        .annotate(quantity=Sum("quantity"))
    )
    for row in quantities:
        Food.objects.filter(pk=row["food_id"]).update(
            stock_quantity=F("stock_quantity") + row["quantity"],
            available=True,
        )


def _request_order_refund(order):
    if order.gateway_payment_status != "success" or not order.cashfree_order_id:
        order.refund_status = "not_required"
        order.save(update_fields=["refund_status"])
        return
    if not settings.CASHFREE_ENABLED:
        order.refund_status = "manual_review"
        order.refund_error = "Cashfree is disabled; refund requires manual handling."
        order.save(update_fields=["refund_status", "refund_error"])
        return
    refund_id = order.refund_id or f"rc{order.id.hex[:26]}01"
    try:
        create_refund = not bool(order.refund_id)
        if order.refund_id:
            refund_path = f"/orders/{order.cashfree_order_id}/refunds/{refund_id}"
            try:
                result = _cashfree_api("GET", refund_path)
            except CashfreeAPIError as error:
                if error.status_code != 404:
                    raise
                create_refund = True
            else:
                status = str(result.get("refund_status", "pending")).lower()
                if status == "failed" and order.refund_attempts < 3:
                    order.refund_attempts += 1
                    refund_id = f"rc{order.id.hex[:24]}{order.refund_attempts + 1:02d}"
                    create_refund = True
                elif status == "failed":
                    order.refund_status = "manual_review"
                    order.refund_error = "Cashfree rejected three refund attempts; review this order manually."
                    order.save(update_fields=["refund_status", "refund_error"])
                    return
                else:
                    order.refund_status = status[:16]
                    order.refund_error = ""
                    order.refund_requested_at = timezone.now()
                    order.save(update_fields=["refund_status", "refund_error", "refund_requested_at"])
                    return
        order.refund_id = refund_id
        order.refund_status = "pending"
        order.save(update_fields=["refund_id", "refund_status"])
        if create_refund:
            result = _cashfree_api(
                "POST",
                f"/orders/{order.cashfree_order_id}/refunds",
                {
                "refund_amount": float(order.gateway_amount),
                "refund_id": refund_id,
                "refund_note": f"Order {str(order.id)[:8]} cancelled: {order.rejection_reason or 'restaurant did not accept in time'}",
                "refund_speed": "STANDARD",
                },
            )
        order.refund_status = str(result.get("refund_status", "pending")).lower()[:16]
        order.refund_error = ""
        order.refund_requested_at = timezone.now()
        order.save(update_fields=["refund_id", "refund_status", "refund_error", "refund_requested_at"])
    except RuntimeError as error:
        order.refund_id = refund_id
        order.refund_status = "failed"
        order.refund_error = str(error)[:2000]
        order.save(update_fields=["refund_id", "refund_status", "refund_error"])
        logger.error("Refund initiation failed for order %s", order.pk)


@csrf_protect
@require_http_methods(["GET", "PUT"])
def restaurant_profile(request):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    if request.method == "PUT":
        try:
            data = _json_body(request)
        except ValueError as body_error:
            return _error(str(body_error))
        name = str(data.get("restaurantName", "")).strip()
        owner_name = str(data.get("ownerName", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        phone = str(data.get("phone", "")).strip()
        address = str(data.get("address", "")).strip()
        nearby_landmark = str(data.get("nearbyLandmark", "")).strip()
        market = str(data.get("market", "")).strip()
        city = str(data.get("city", "")).strip()
        pincode = str(data.get("pincode", "")).strip()
        coordinates_supplied = "latitude" in data or "longitude" in data
        try:
            latitude, longitude = _coordinates_from_data(data)
        except ValueError as coordinate_error:
            return _error(str(coordinate_error))
        if not name or not owner_name or not phone or not address or not city or not pincode:
            return _error(
                "Restaurant name, owner, phone, address, city, and PIN code are required."
            )
        if len(name) > 160 or len(phone) > 30 or len(address) > 2000:
            return _error("Restaurant profile fields are too long.")
        if len(city) > 100 or len(nearby_landmark) > 160 or len(market) > 160:
            return _error("Restaurant location details are too long.")
        if len(pincode) != 6 or not pincode.isdigit():
            return _error("Enter a valid 6-digit restaurant PIN code.")
        try:
            validate_email(email)
        except ValidationError:
            return _error("Enter a valid account email address.")
        user_model = get_user_model()
        if user_model.objects.filter(email__iexact=email).exclude(
            pk=request.user.pk
        ).exists():
            return _error("This email address is already in use.", 409)
        restaurant.name = name
        restaurant.owner_name = owner_name
        restaurant.email = email
        restaurant.phone = phone
        restaurant.address = address
        restaurant.nearby_landmark = nearby_landmark
        restaurant.market = market
        restaurant.city = city
        restaurant.pincode = pincode
        if coordinates_supplied:
            restaurant.latitude = latitude
            restaurant.longitude = longitude
        request.user.email = email
        request.user.username = email
        request.user.first_name = owner_name
        with transaction.atomic():
            request.user.save(update_fields=["email", "username", "first_name"])
            restaurant.save()
    return JsonResponse(
        {
            "id": restaurant.id,
            "name": restaurant.name,
            "restaurantName": restaurant.name,
            "ownerName": restaurant.owner_name,
            "email": restaurant.email,
            "phone": restaurant.phone,
            "address": restaurant.address,
            "nearbyLandmark": restaurant.nearby_landmark,
            "market": restaurant.market,
            "logo": _restaurant_logo_url(restaurant),
            "approved": restaurant.approved,
            "acceptOrders": restaurant.accepting_orders,
            "openNow": restaurant.accepts_orders_now(),
            "openingHoursEnabled": restaurant.opening_hours_enabled,
            "opensAt": restaurant.opens_at.strftime("%H:%M") if restaurant.opens_at else "",
            "closesAt": restaurant.closes_at.strftime("%H:%M") if restaurant.closes_at else "",
            "city": restaurant.city,
            "pincode": restaurant.pincode,
            "latitude": (
                float(restaurant.latitude)
                if restaurant.latitude is not None
                else None
            ),
            "longitude": (
                float(restaurant.longitude)
                if restaurant.longitude is not None
                else None
            ),
        }
    )


@csrf_protect
@require_http_methods(["PUT", "POST"])
def restaurant_profile_logo(request):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    uploaded = request.FILES.get("logo")
    if uploaded is None:
        return _error("Choose an image file.")
    if uploaded.size <= 0 or uploaded.size > 3 * 1024 * 1024:
        return _error("Logo image must be smaller than 3 MB.")
    extension = uploaded.name.rsplit(".", 1)[-1].lower() if "." in uploaded.name else ""
    signature = uploaded.read(12)
    uploaded.seek(0)
    signatures = {
        "png": signature.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpg": signature.startswith(b"\xff\xd8\xff"),
        "jpeg": signature.startswith(b"\xff\xd8\xff"),
        "gif": signature[:6] in {b"GIF87a", b"GIF89a"},
        "webp": signature.startswith(b"RIFF") and signature[8:12] == b"WEBP",
    }
    valid_signature = signatures.get(extension, False)
    if not valid_signature:
        return _error("Logo must be a valid PNG, JPEG, GIF, or WebP image.")
    restaurant.logo = uploaded
    restaurant.save(update_fields=["logo"])
    return JsonResponse({"logo": _restaurant_logo_url(restaurant)})


@csrf_protect
@require_http_methods(["GET", "PUT"])
def restaurant_settings(request):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    if request.method == "GET":
        return JsonResponse(
            {
                "acceptOrders": restaurant.accepting_orders,
                "openingHoursEnabled": restaurant.opening_hours_enabled,
                "opensAt": restaurant.opens_at.strftime("%H:%M") if restaurant.opens_at else "",
                "closesAt": restaurant.closes_at.strftime("%H:%M") if restaurant.closes_at else "",
                "orderNotifications": restaurant.order_notifications,
                "orderSound": restaurant.order_sound,
                "email": request.user.email,
            }
        )
    try:
        data = _json_body(request)
    except ValueError as body_error:
        return _error(str(body_error))
    email = str(data.get("email", request.user.email)).strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        return _error("Enter a valid account email address.")
    if get_user_model().objects.filter(email__iexact=email).exclude(
        pk=request.user.pk
    ).exists():
        return _error("This email address is already in use.", 409)
    new_password = str(data.get("newPassword", ""))
    if new_password:
        current_password = str(data.get("currentPassword", ""))
        if not request.user.check_password(current_password):
            return _error("Current password is incorrect.", 403)
        try:
            validate_password(new_password, user=request.user)
        except ValidationError as validation_error:
            return _error(" ".join(validation_error.messages))
        request.user.set_password(new_password)
    request.user.email = email
    request.user.username = email
    restaurant.email = email
    for key in ("acceptOrders", "orderNotifications", "orderSound", "openingHoursEnabled"):
        if key in data and not isinstance(data[key], bool):
            return _error(f"{key} must be true or false.")
    restaurant.accepting_orders = data.get("acceptOrders", restaurant.accepting_orders)
    restaurant.order_notifications = data.get("orderNotifications", True)
    restaurant.order_sound = data.get("orderSound", True)
    if "openingHoursEnabled" in data:
        enabled = data["openingHoursEnabled"]
        if enabled:
            try:
                opens_at = datetime.strptime(str(data.get("opensAt", "")), "%H:%M").time()
                closes_at = datetime.strptime(str(data.get("closesAt", "")), "%H:%M").time()
            except ValueError:
                return _error("Opening and closing times are required in HH:MM format.")
            restaurant.opening_hours_enabled = True
            restaurant.opens_at = opens_at
            restaurant.closes_at = closes_at
        else:
            restaurant.opening_hours_enabled = False
    with transaction.atomic():
        request.user.save()
        restaurant.save(
            update_fields=[
                "email",
                "accepting_orders",
                "opening_hours_enabled",
                "opens_at",
                "closes_at",
                "order_notifications",
                "order_sound",
            ]
        )
    if new_password:
        from django.contrib.auth import update_session_auth_hash

        update_session_auth_hash(request, request.user)
    return JsonResponse({"message": "Restaurant settings updated."})
@require_GET
def restaurant_dashboard(request):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    orders = Order.objects.filter(restaurant=restaurant)
    today = timezone.localdate()
    return JsonResponse(
        {
            "totalProducts": restaurant.foods.count(),
            "totalOrders": orders.count(),
            "pendingOrders": orders.filter(status=Order.Status.PLACED).count(),
            "completedOrders": orders.filter(status=Order.Status.COMPLETED).count(),
            "todaysEarning": float(
                sum(
                    orders.filter(
                        status=Order.Status.COMPLETED,
                        created_at__date=today,
                    ).values_list("restaurant_payout", flat=True),
                    Decimal("0"),
                )
            ),
            "notificationCount": orders.filter(status=Order.Status.PLACED).count(),
        }
    )


@require_GET
def restaurant_reports(request):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    today = timezone.localdate()
    try:
        start_date = date.fromisoformat(request.GET.get("startDate", (today - timedelta(days=6)).isoformat()))
        end_date = date.fromisoformat(request.GET.get("endDate", today.isoformat()))
    except ValueError:
        return _error("Choose valid report dates in YYYY-MM-DD format.")
    if end_date < start_date:
        return _error("End date must be on or after the start date.")
    if (end_date - start_date).days > 366:
        return _error("Choose a date range of 367 days or less.")
    orders = Order.objects.filter(
        restaurant=restaurant,
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    ).exclude(status__in=[Order.Status.PAYMENT_PENDING, Order.Status.PAYMENT_FAILED]).order_by("-created_at")
    completed = orders.filter(status=Order.Status.COMPLETED)
    totals = completed.aggregate(sales=Sum("item_total"))
    rows = [
        {
            "orderNumber": _order_public_number(order),
            "dailyOrderNumber": order.daily_order_number,
            "customerName": order.customer_name,
            "date": timezone.localtime(order.created_at).strftime("%Y-%m-%d %H:%M"),
            "itemSales": float(order.item_total),
        }
        for order in orders
    ]
    output_format = request.GET.get("format", "").lower()
    if output_format == "pdf":
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        pdf_buffer = BytesIO()
        document = SimpleDocTemplate(
            pdf_buffer,
            pagesize=A4,
            rightMargin=18 * mm,
            leftMargin=18 * mm,
            topMargin=18 * mm,
            bottomMargin=18 * mm,
            title=f"Ramjicart order report {start_date} to {end_date}",
        )
        styles = getSampleStyleSheet()
        content = [
            Paragraph("Ramjicart Order Report", styles["Title"]),
            Paragraph(f"Date range: {start_date:%d-%m-%Y} to {end_date:%d-%m-%Y}", styles["Normal"]),
            Spacer(1, 8 * mm),
        ]
        summary_table = Table(
            [["Orders", str(orders.count()), "Completed", str(completed.count()), "Item sales", f"Rs. {float(totals['sales'] or 0):.2f}"]],
            colWidths=[22 * mm, 18 * mm, 28 * mm, 18 * mm, 25 * mm, 32 * mm],
        )
        summary_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f8f4")),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#172033")),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#dce8e1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        content.extend([summary_table, Spacer(1, 8 * mm)])
        table_data = [["Order", "Name", "Date", "Item sales"]]
        table_data.extend([[row["orderNumber"], row["customerName"], row["date"], f"Rs. {row['itemSales']:.2f}"] for row in rows])
        report_table = Table(table_data, colWidths=[35 * mm, 45 * mm, 65 * mm, 35 * mm], repeatRows=1)
        report_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#198754")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dce2df")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7faf8")]),
            ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        content.append(report_table)
        document.build(content)
        response = HttpResponse(pdf_buffer.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="ramjicart-order-report-{start_date}-{end_date}.pdf"'
        return response
    if output_format == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="ramjicart-report-{start_date}-{end_date}.csv"'
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow(["Order", "Name", "Date", "Item sales"])
        for row in rows:
            values = [row[key] for key in ("orderNumber", "customerName", "date", "itemSales")]
            writer.writerow([f"'{value}" if isinstance(value, str) and value[:1] in "=+-@" else value for value in values])
        return response
    return JsonResponse({
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "orderCount": orders.count(),
        "completedOrders": completed.count(),
        "itemSales": float(totals["sales"] or 0),
        "orders": rows,
    })


def _product_payload(food):
    return {
        "id": food.id,
        "name": food.name,
        "category": food.category.name if food.category else "",
        "categoryId": food.category_id,
        "description": food.description,
        "image": food.image,
        "price": float(food.price),
        "available": food.available,
        "stockQuantity": food.stock_quantity,
        "variants": food.variants,
        "quantityUnit": food.quantity_unit,
        "addons": food.addons,
    }


def _parse_menu_options(value, label, allow_empty=True):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            parsed = []
            for line in value.splitlines():
                if not line.strip():
                    continue
                if ":" not in line:
                    raise ValueError(f"{label}: use Name:Price on each line.")
                name, price = line.rsplit(":", 1)
                parsed.append({"name": name.strip(), "price": price.strip()})
            value = parsed
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError(f"{label} must contain at most 20 options.")
    result = []
    names = set()
    for option in value:
        if not isinstance(option, dict):
            raise ValueError(f"Each {label.lower()} option must have a name and price.")
        name = str(option.get("name", "")).strip()
        try:
            price = Decimal(str(option.get("price", "")))
        except (InvalidOperation, TypeError):
            raise ValueError(f"Enter a valid price for each {label.lower()} option.")
        if not name or len(name) > 80 or not price.is_finite() or price < 0:
            raise ValueError(f"Each {label.lower()} option needs a name and a non-negative price.")
        if name.casefold() in names:
            raise ValueError(f"{label} names must be unique.")
        names.add(name.casefold())
        option_id = str(option.get("id", "")).strip() or str(uuid.uuid4())
        if len(option_id) > 80:
            raise ValueError(f"{label} option id is too long.")
        result.append(
            {"id": option_id, "name": name, "price": str(money(price))}
        )
    return result


@csrf_protect
@require_http_methods(["GET", "POST"])
def restaurant_products(request):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    if request.method == "GET":
        products = restaurant.foods.select_related("category").order_by("name")
        return JsonResponse([_product_payload(food) for food in products], safe=False)
    try:
        data = _json_body(request) if request.content_type == "application/json" else request.POST
    except ValueError as error:
        return _error(str(error))
    name = data.get("productName", "").strip()
    category_name = data.get("productCategory", "").strip()
    quantity_unit = data.get("quantityUnit", "").strip()
    description = data.get("productDescription", "").strip()
    image = data.get("productImage", "").strip()
    raw_stock = data.get("stockQuantity", "").strip()
    try:
        stock_quantity = Decimal(raw_stock) if raw_stock else None
        if stock_quantity is not None and (not stock_quantity.is_finite() or stock_quantity < 0 or stock_quantity != stock_quantity.quantize(Decimal("0.01"))):
            raise ValueError
    except (TypeError, ValueError, InvalidOperation):
        return _error("Stock quantity must be a whole number of zero or more.")
    try:
        variants = _parse_menu_options(data.get("variants", ""), "Variants")
        addons = _parse_menu_options(data.get("addons", ""), "Add-ons")
    except ValueError as error:
        return _error(str(error))
    try:
        price = Decimal(data.get("productPrice", ""))
    except (InvalidOperation, TypeError):
        return _error("Enter a valid product price.")
    if not name or len(name) > 160 or not category_name or len(description) > 2000:
        return _error("Enter a product name, category, and a description under 2000 characters.")
    if quantity_unit not in {"kg", "piece"}:
        return _error("Choose kg or piece as the quantity unit.")
    if not price.is_finite() or price <= 0:
        return _error("Product price must be greater than zero.")
    if image:
        try:
            URLValidator(schemes=["http", "https"])(image)
        except ValidationError:
            return _error("Product image must be a valid HTTP or HTTPS URL.")
    with transaction.atomic():
        category = Category.objects.filter(name__iexact=category_name).first()
        if category is None:
            category = Category.objects.create(name=category_name)
        food = Food(
            restaurant=restaurant,
            category=category,
            name=name,
            description=description,
            image=image,
            price=price,
            stock_quantity=stock_quantity,
            available=stock_quantity != 0,
            variants=variants,
            quantity_unit=quantity_unit,
            addons=addons,
        )
        try:
            food.full_clean()
        except ValidationError as validation_error:
            return _error("; ".join(validation_error.messages))
        food.save()
    return JsonResponse(_product_payload(food), status=201)


@csrf_protect
@require_http_methods(["PUT", "DELETE"])
def restaurant_product_detail(request, food_id):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    food = get_object_or_404(Food, id=food_id, restaurant=restaurant)
    if request.method == "DELETE":
        if OrderItem.objects.filter(food=food).exists():
            food.available = False
            food.save(update_fields=["available"])
            return JsonResponse({"message": "This product is part of order history, so it was marked unavailable instead of deleted."})
        food.delete()
        return JsonResponse({"message": "Product deleted."})
    try:
        data = _json_body(request)
    except ValueError as error:
        return _error(str(error))
    name = str(data.get("name", food.name)).strip()
    description = str(data.get("description", food.description)).strip()
    image = str(data.get("image", food.image)).strip()
    if not name or len(name) > 160 or len(description) > 2000:
        return _error("Product name and description are required and within the allowed length.")
    if image:
        try:
            URLValidator(schemes=["http", "https"])(image)
        except ValidationError:
            return _error("Product image must be a valid HTTP or HTTPS URL.")
    try:
        price = Decimal(str(data.get("price", food.price)))
        if not price.is_finite() or price <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError):
        return _error("Product price must be greater than zero.")
    category = food.category
    if "categoryId" in data:
        category = get_object_or_404(Category, id=data["categoryId"], is_active=True)
    stock_quantity = food.stock_quantity
    if "stockQuantity" in data:
        try:
            raw_stock = data["stockQuantity"]
            stock_quantity = Decimal(str(raw_stock)) if raw_stock not in (None, "") else None
            if stock_quantity is not None and (not stock_quantity.is_finite() or stock_quantity < 0 or stock_quantity != stock_quantity.quantize(Decimal("0.01"))):
                raise ValueError
        except (TypeError, ValueError, InvalidOperation):
            return _error("Stock quantity must be a whole number of zero or more.")
    try:
        variants = _parse_menu_options(data.get("variants", food.variants), "Variants")
        addons = _parse_menu_options(data.get("addons", food.addons), "Add-ons")
    except ValueError as error:
        return _error(str(error))
    food.name = name
    food.description = description
    food.image = image
    food.price = price
    food.category = category
    quantity_unit = str(data.get("quantityUnit", "kg" if category and category.slug in {"groceries", "fruits", "vegetables"} else "piece"))
    if quantity_unit not in {"kg", "piece"}:
        return _error("Choose kg or piece as the quantity unit.")
    food.quantity_unit = quantity_unit
    if "available" in data and not isinstance(data["available"], bool):
        return _error("Availability must be true or false.")
    food.stock_quantity = stock_quantity
    food.available = data.get("available", food.available)
    if food.stock_quantity == 0:
        if data.get("available") is True:
            return _error("Set stock above zero before marking this product available.")
        food.available = False
    food.variants = variants
    food.addons = addons
    try:
        food.full_clean()
    except ValidationError as error:
        return _error(" ".join(error.messages))
    food.save()
    return JsonResponse(_product_payload(food))


def restaurant_orders(request):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    orders = (
        Order.objects.filter(restaurant=restaurant)
        .exclude(status__in=[Order.Status.PAYMENT_PENDING, Order.Status.PAYMENT_FAILED])
        .prefetch_related("items")
        .order_by("-created_at")
    )
    return JsonResponse(
        {"orders": [_restaurant_order_data(order) for order in orders]}
    )


@csrf_protect
@require_http_methods(["PUT", "POST"])
def restaurant_update_order(request, order_id):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    order = get_object_or_404(Order, id=order_id, restaurant=restaurant)
    try:
        data = _json_body(request)
        requested_status = str(data.get("status", "")).lower()
    except ValueError as body_error:
        return _error(str(body_error))
    transitions = {
        "confirmed": (Order.Status.PLACED, Order.Status.ACCEPTED),
        "accepted": (Order.Status.PLACED, Order.Status.ACCEPTED),
        "preparing": (Order.Status.ACCEPTED, Order.Status.PREPARING),
        "ready": (Order.Status.PREPARING, Order.Status.READY_FOR_PICKUP),
        "cancelled": (Order.Status.PLACED, Order.Status.CANCELLED),
        "rejected": (Order.Status.PLACED, Order.Status.CANCELLED),
    }
    transition = transitions.get(requested_status)
    if transition is None:
        return _error("Unsupported restaurant order action.")
    allowed_current, new_status = transition
    if order.status != allowed_current:
        return _error("This order can no longer be changed to that status.", 409)
    if requested_status in {"cancelled", "rejected"}:
        reason = str(data.get("reason", "")).strip()
        if requested_status == "rejected" and len(reason) < 4:
            return _error("A short reason is required to reject an order.")
        if len(reason) > 300:
            return _error("Rejection reason must be 300 characters or fewer.")
        order.rejection_reason = reason or "Restaurant cancelled before acceptance."
    if new_status == Order.Status.ACCEPTED:
        order.accepted_at = timezone.now()
    order.status = new_status
    update_fields = ["status"]
    if order.rejection_reason:
        update_fields.append("rejection_reason")
    if new_status == Order.Status.ACCEPTED:
        update_fields.append("accepted_at")
    order.save(update_fields=update_fields)
    if new_status == Order.Status.CANCELLED:
        _restore_order_stock(order)
        _request_order_refund(order)
    return JsonResponse(
        {
            "message": f"Order status changed to {order.get_status_display()}.",
            "order": _restaurant_order_data(order),
        }
    )


@csrf_protect
@require_POST
def restaurant_confirm_pickup(request, order_id):
    restaurant, error = _restaurant_for_request(request)
    if error:
        return error
    order = get_object_or_404(Order, id=order_id, restaurant=restaurant)
    if order.order_type != Order.OrderType.PICKUP:
        return _error("Only pickup orders are confirmed by the restaurant.")
    if order.status != Order.Status.READY_FOR_PICKUP:
        return _error("Order must be ready before pickup can be confirmed.", 409)
    try:
        code = str(_json_body(request).get("confirmationCode", "")).strip()
    except ValueError as body_error:
        return _error(str(body_error))
    if len(code) != 4 or not code.isdigit():
        return _error("Enter the customer's 4-digit confirmation code.")
    return _confirm_order_with_code(
        order, code, "Pickup", {Order.Status.READY_FOR_PICKUP}
    )


def _delivery_partner_profile(request):
    role_error = _require_role(request, AccountProfile.Role.DELIVERY_PARTNER)
    if role_error:
        return None, role_error
    return _account_profile(request), None


@require_GET
def delivery_profile(request):
    profile, error = _delivery_partner_profile(request)
    if error:
        return error
    return JsonResponse(
        {
            "name": request.user.get_full_name() or request.user.email,
            "email": request.user.email,
            "phone": profile.phone,
            "approved": profile.approved,
            "location": _latest_delivery_location(profile),
            "dispatchRadiusKm": settings.DELIVERY_DISPATCH_RADIUS_KM,
        }
    )


@csrf_protect
@require_http_methods(["GET", "POST", "DELETE"])
def delivery_location(request):
    profile, error = _delivery_partner_profile(request)
    if error:
        return error
    if request.method == "GET":
        location = _latest_delivery_location(profile)
        return JsonResponse({"location": location, "sharing": location is not None})
    if request.method == "DELETE":
        profile.latitude = None
        profile.longitude = None
        profile.location_updated_at = None
        profile.save(update_fields=["latitude", "longitude", "location_updated_at"])
        return JsonResponse(
            {
                "message": "Location sharing stopped.",
                "sharing": False,
                "dispatchRadiusKm": settings.DELIVERY_DISPATCH_RADIUS_KM,
            }
        )
    try:
        latitude, longitude = _coordinates_from_data(_json_body(request))
    except ValueError as coordinate_error:
        return _error(str(coordinate_error))
    if latitude is None:
        return _error("Provide your current latitude and longitude.")
    profile.latitude = latitude
    profile.longitude = longitude
    profile.location_updated_at = timezone.now()
    profile.save(update_fields=["latitude", "longitude", "location_updated_at"])
    return JsonResponse(
        {
            "message": "Location updated.",
            "location": _latest_delivery_location(profile),
            "sharing": True,
            "dispatchRadiusKm": settings.DELIVERY_DISPATCH_RADIUS_KM,
        }
    )


@require_GET
def delivery_dashboard(request):
    profile, error = _delivery_partner_profile(request)
    if error:
        return error
    orders = Order.objects.filter(delivery_partner=profile)
    today = timezone.localdate()
    completed_today = orders.filter(
        status=Order.Status.COMPLETED, created_at__date=today
    ).count()
    recent_completed = (
        orders.filter(status=Order.Status.COMPLETED)
        .select_related("restaurant")
        .prefetch_related("items")
        .order_by("-created_at")[:20]
    )
    return JsonResponse(
        {
            "todayDeliveries": completed_today,
            "completedDeliveries": orders.filter(status=Order.Status.COMPLETED).count(),
            "earningsTracked": False,
            "recentCompleted": [
                _delivery_order_data(order) for order in recent_completed
            ],
        }
    )


def _latest_delivery_location(profile):
    if (
        profile.latitude is None
        or profile.longitude is None
        or profile.location_updated_at is None
        or profile.location_updated_at < timezone.now() - LIVE_LOCATION_MAX_AGE
    ):
        return None
    return {
        "latitude": float(profile.latitude),
        "longitude": float(profile.longitude),
        "updatedAt": profile.location_updated_at.isoformat(),
    }


def _delivery_order_data(order):
    return {
        "id": str(order.id),
        "orderNumber": _order_public_number(order),
        "dailyOrderNumber": order.daily_order_number,
        "date": timezone.localtime(order.created_at).strftime("%d %b %Y, %I:%M %p"),
        "statusCode": order.status,
        "status": order.get_status_display(),
        "orderType": order.order_type,
        "restaurantPayout": float(order.restaurant_payout),
        "restaurant": {
            "name": order.restaurant.name,
            "address": order.restaurant.address,
            "latitude": (
                float(order.restaurant.latitude)
                if order.restaurant.latitude is not None
                else None
            ),
            "longitude": (
                float(order.restaurant.longitude)
                if order.restaurant.longitude is not None
                else None
            ),
        },
        "customer": {
            "name": order.customer_name,
            "phone": order.customer_phone,
            "address": order.delivery_address,
            "latitude": (
                float(order.delivery_latitude)
                if order.delivery_latitude is not None
                else None
            ),
            "longitude": (
                float(order.delivery_longitude)
                if order.delivery_longitude is not None
                else None
            ),
        },
        "items": [
            {
                "name": item.food_name,
                "quantity": item.quantity,
                "total": float(item.line_total),
                "variantId": item.variant_id,
                "addons": item.addons,
            }
            for item in order.items.all()
        ],
        "location": (
            _latest_delivery_location(order.delivery_partner)
            if order.delivery_partner_id
            else None
        ),
    }


@require_GET
def delivery_available_orders(request):
    profile, error = _delivery_partner_profile(request)
    if error:
        return error
    orders = Order.objects.filter(
        order_type=Order.OrderType.DELIVERY,
        status=Order.Status.READY_FOR_PICKUP,
        delivery_partner__isnull=True,
    ).select_related("restaurant").prefetch_related("items")
    driver_location = _latest_delivery_location(profile)
    fresh_partner_locations = list(
        AccountProfile.objects.filter(
            role=AccountProfile.Role.DELIVERY_PARTNER,
            approved=True,
            latitude__isnull=False,
            longitude__isnull=False,
            location_updated_at__gte=timezone.now() - LIVE_LOCATION_MAX_AGE,
        ).values_list("latitude", "longitude")
    )
    offers = []
    eligible_count = 0
    radius_km = settings.DELIVERY_DISPATCH_RADIUS_KM
    for order in orders:
        distance = None
        if (
            driver_location
            and order.restaurant.latitude is not None
            and order.restaurant.longitude is not None
        ):
            distance = _distance_km(
                driver_location["latitude"],
                driver_location["longitude"],
                order.restaurant.latitude,
                order.restaurant.longitude,
            )
        eligible = distance is not None and distance <= radius_km
        eligible_count += int(eligible)
        nearby_partner_count = 0
        if order.restaurant.latitude is not None and order.restaurant.longitude is not None:
            nearby_partner_count = sum(
                1
                for partner_latitude, partner_longitude in fresh_partner_locations
                if _distance_km(
                    partner_latitude,
                    partner_longitude,
                    order.restaurant.latitude,
                    order.restaurant.longitude,
                ) <= radius_km
            )
        offer = _delivery_order_data(order)
        if not eligible:
            offer["customer"] = {
                "name": "Customer",
                "phone": "",
                "address": "Delivery details shown to a partner within range",
                "latitude": None,
                "longitude": None,
            }
            offer["items"] = []
        if eligible:
            eligibility_message = "Within dispatch range"
        elif not driver_location:
            eligibility_message = "Share location to see which offers are within dispatch range"
        elif order.restaurant.latitude is None or order.restaurant.longitude is None:
            eligibility_message = "Restaurant map location missing; offer remains open"
        elif nearby_partner_count:
            eligibility_message = "Open offer; a nearby partner can accept"
        else:
            eligibility_message = "Open offer; waiting for a partner within dispatch range"
        offer.update(
            {
                "eligible": eligible,
                "distanceKm": round(distance, 2) if distance is not None else None,
                "dispatchRadiusKm": radius_km,
                "nearbyPartnerCount": nearby_partner_count,
                "eligibilityMessage": eligibility_message,
            }
        )
        offers.append(offer)
    return JsonResponse(
        {
            "orders": offers,
            "eligibleCount": eligible_count,
            "dispatchRadiusKm": radius_km,
            "locationRequired": driver_location is None,
        }
    )


@require_GET
def delivery_current_order(request):
    profile, error = _delivery_partner_profile(request)
    if error:
        return error
    orders = list(
        Order.objects.filter(
            delivery_partner=profile,
            status__in=[
                Order.Status.ASSIGNED,
                Order.Status.PICKED_UP,
                Order.Status.OUT_FOR_DELIVERY,
            ],
        )
        .select_related("restaurant")
        .prefetch_related("items")
        .order_by("-created_at")
    )
    delivery_orders = [_delivery_order_data(order) for order in orders]
    return JsonResponse(
        {
            "orders": delivery_orders,
            "order": delivery_orders[0] if delivery_orders else None,
        }
    )


@csrf_protect
@require_http_methods(["PUT", "POST"])
def delivery_accept_order(request, order_id):
    profile, error = _delivery_partner_profile(request)
    if error:
        return error
    with transaction.atomic():
        profile = AccountProfile.objects.select_for_update().get(pk=profile.pk)
        order = get_object_or_404(Order.objects.select_for_update(), id=order_id)
        if order.order_type != Order.OrderType.DELIVERY:
            return _error("Pickup orders do not need a delivery partner.", 400)
        if (
            order.status != Order.Status.READY_FOR_PICKUP
            or order.delivery_partner_id is not None
        ):
            return _error("This delivery is no longer available.", 409)
        driver_location = _latest_delivery_location(profile)
        if driver_location is None:
            return _error("Share your current location before accepting deliveries.", 409)
        if order.restaurant.latitude is None or order.restaurant.longitude is None:
            return _error(
                "This restaurant has no saved map location yet. The order remains open.",
                409,
            )
        distance = _distance_km(
            driver_location["latitude"],
            driver_location["longitude"],
            order.restaurant.latitude,
            order.restaurant.longitude,
        )
        if distance > settings.DELIVERY_DISPATCH_RADIUS_KM:
            return _error(
                "This order is outside your dispatch range and remains open for a nearby partner.",
                409,
            )
        order.delivery_partner = profile
        order.status = Order.Status.ASSIGNED
        order.save(update_fields=["delivery_partner", "status"])
    return JsonResponse(
        {"message": "Delivery assigned to you.", "order": _delivery_order_data(order)}
    )


@csrf_protect
@require_http_methods(["PUT", "POST"])
def delivery_update_status(request, order_id):
    profile, error = _delivery_partner_profile(request)
    if error:
        return error
    order = get_object_or_404(Order, id=order_id, delivery_partner=profile)
    try:
        requested_status = str(_json_body(request).get("status", "")).upper()
    except ValueError as body_error:
        return _error(str(body_error))
    if requested_status == "PICKED_UP" and order.status == Order.Status.ASSIGNED:
        order.status = Order.Status.PICKED_UP
    elif requested_status == "ON_THE_WAY" and order.status == Order.Status.PICKED_UP:
        order.status = Order.Status.OUT_FOR_DELIVERY
    else:
        return _error("Invalid delivery status transition.", 409)
    order.save(update_fields=["status"])
    return JsonResponse({"message": "Delivery status updated.", "status": order.status})


@csrf_protect
@require_http_methods(["POST", "DELETE"])
def delivery_update_location(request, order_id):
    profile, error = _delivery_partner_profile(request)
    if error:
        return error
    order = get_object_or_404(
        Order,
        id=order_id,
        delivery_partner=profile,
        status__in=[Order.Status.PICKED_UP, Order.Status.OUT_FOR_DELIVERY],
    )
    if request.method == "DELETE":
        profile.latitude = None
        profile.longitude = None
        profile.location_updated_at = None
        profile.save(update_fields=["latitude", "longitude", "location_updated_at"])
        return JsonResponse({"message": "Location sharing stopped."})
    try:
        data = _json_body(request)
        latitude = Decimal(str(data.get("latitude")))
        longitude = Decimal(str(data.get("longitude")))
    except (ValueError, InvalidOperation, TypeError):
        return _error("Provide valid latitude and longitude coordinates.")
    if not latitude.is_finite() or not longitude.is_finite() or not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        return _error("Coordinates are outside valid ranges.")
    profile.latitude = latitude.quantize(Decimal("0.000001"))
    profile.longitude = longitude.quantize(Decimal("0.000001"))
    profile.location_updated_at = timezone.now()
    profile.save(update_fields=["latitude", "longitude", "location_updated_at"])
    return JsonResponse({"message": "Location updated.", "updatedAt": profile.location_updated_at.isoformat()})


@csrf_protect
@require_http_methods(["PUT", "POST"])
def delivery_complete_order(request, order_id):
    profile, error = _delivery_partner_profile(request)
    if error:
        return error
    order = get_object_or_404(Order, id=order_id, delivery_partner=profile)
    if order.status not in {Order.Status.PICKED_UP, Order.Status.OUT_FOR_DELIVERY}:
        return _error("Pick up the order before confirming delivery.", 409)
    try:
        code = str(_json_body(request).get("confirmationCode", "")).strip()
    except ValueError as body_error:
        return _error(str(body_error))
    if len(code) != 4 or not code.isdigit():
        return _error("Enter the customer's 4-digit confirmation code.")
    return _confirm_order_with_code(
        order,
        code,
        "Delivery",
        {Order.Status.PICKED_UP, Order.Status.OUT_FOR_DELIVERY},
    )


def _confirm_order_with_code(order, code, completion_label, allowed_statuses):
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(id=order.id)
        if locked_order.status not in allowed_statuses:
            return _error("Order is not ready for confirmation.", 409)
        if locked_order.confirmation_attempts >= 5:
            return _error("Too many incorrect codes. Ask the customer to contact support.", 429)
        if not locked_order.confirmation_code or not secrets.compare_digest(
            code, locked_order.confirmation_code
        ):
            Order.objects.filter(id=locked_order.id).update(
                confirmation_attempts=F("confirmation_attempts") + 1
            )
            return _error("Confirmation code is incorrect.", 400)
        locked_order.status = Order.Status.COMPLETED
        locked_order.save(update_fields=["status"])
        if locked_order.delivery_partner_id:
            AccountProfile.objects.filter(pk=locked_order.delivery_partner_id).update(
                latitude=None, longitude=None, location_updated_at=None
            )
    return JsonResponse(
        {
            "message": f"{completion_label} confirmed.",
            "status": Order.Status.COMPLETED,
        }
    )


def _order_timeline(status):
    cancelled = status == Order.Status.CANCELLED
    placed = status in {
        Order.Status.PAID,
        Order.Status.PLACED,
        Order.Status.ACCEPTED,
        Order.Status.PREPARING,
        Order.Status.READY_FOR_PICKUP,
        Order.Status.ASSIGNED,
        Order.Status.PICKED_UP,
        Order.Status.OUT_FOR_DELIVERY,
        Order.Status.COMPLETED,
    }
    preparing_done = status in {
        Order.Status.READY_FOR_PICKUP,
        Order.Status.ASSIGNED,
        Order.Status.PICKED_UP,
        Order.Status.OUT_FOR_DELIVERY,
        Order.Status.COMPLETED,
    }
    out_for_delivery = status in {
        Order.Status.ASSIGNED,
        Order.Status.PICKED_UP,
        Order.Status.OUT_FOR_DELIVERY,
    }
    delivered = status == Order.Status.COMPLETED
    return [
        {
            "status": (
                "Order cancelled"
                if cancelled
                else ("Order placed" if placed else "Payment pending")
            ),
            "completed": placed or cancelled,
            "active": status in {Order.Status.PAYMENT_PENDING, Order.Status.PLACED},
        },
        {
            "status": "Preparing",
            "completed": preparing_done,
            "active": status in {Order.Status.ACCEPTED, Order.Status.PREPARING},
        },
        {
            "status": "On the way",
            "completed": delivered,
            "active": out_for_delivery,
        },
        {
            "status": "Delivered",
            "completed": delivered,
            "active": False,
        },
    ]


@require_GET
def current_order(request):
    role_error = _require_role(request, AccountProfile.Role.CUSTOMER)
    if role_error:
        return role_error
    order = Order.objects.filter(customer=request.user).first()
    if order is None:
        return _error("No current order.", 404)
    return order_detail(request, order.id)


@require_GET
def customer_order_history(request):
    role_error = _require_role(request, AccountProfile.Role.CUSTOMER)
    if role_error:
        return role_error
    orders = (
        Order.objects.filter(customer=request.user)
        .select_related("restaurant")
        .prefetch_related("items")
        .order_by("-created_at")
    )
    return JsonResponse(
        {
            "orders": [
                {
                    "id": str(order.id),
                    "orderNumber": _order_public_number(order),
                    "dailyOrderNumber": order.daily_order_number,
                    "createdAt": timezone.localtime(order.created_at).strftime(
                        "%d %b %Y, %I:%M %p"
                    ),
                    "status": order.get_status_display(),
                    "restaurantName": order.restaurant.name,
                    "orderType": order.get_order_type_display(),
                    "total": float(order.customer_total),
                    "items": [
                        {
                            "name": item.food_name,
                            "quantity": item.quantity,
                            "variantId": item.variant_id,
                            "addons": item.addons,
                        }
                        for item in order.items.all()
                    ],
                }
                for order in orders
            ]
        }
    )


@csrf_protect
@require_http_methods(["POST", "PUT"])
def cancel_order(request, order_id):
    role_error = _require_role(request, AccountProfile.Role.CUSTOMER)
    if role_error:
        return role_error
    order = get_object_or_404(Order, id=order_id, customer=request.user)
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status != Order.Status.PLACED:
            return _error("Only new orders can be cancelled here.", 409)
        order.status = Order.Status.CANCELLED
        order.save(update_fields=["status"])
        _restore_order_stock(order)
    _request_order_refund(order)
    order.refresh_from_db()
    return JsonResponse({
        "message": "Order cancelled.",
        "status": order.status,
        "refundStatus": order.refund_status,
    })
