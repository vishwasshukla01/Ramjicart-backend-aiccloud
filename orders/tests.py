import base64
import hashlib
import hmac
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import AccountProfile, Category, Food, Order, OrderItem, PricingSettings, Restaurant


class DeliveryCartAPITests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.customer = user_model.objects.create_user(
            username="customer@example.test",
            email="customer@example.test",
            first_name="Test Customer",
            password="SafePassword!2026",
        )
        AccountProfile.objects.create(
            user=self.customer,
            role=AccountProfile.Role.CUSTOMER,
            phone="9000000000",
        )
        self.restaurant = Restaurant.objects.create(
            name="Test Kitchen",
            email="restaurant@example.test",
            approved=True,
            accepting_orders=True,
            commission_percentage=Decimal("5.00"),
            max_commission_percentage=Decimal("10.00"),
        )
        self.category = Category.objects.create(name="Meals")
        self.food = Food.objects.create(
            restaurant=self.restaurant,
            category=self.category,
            name="Test meal",
            price=Decimal("100.00"),
            stock_quantity=Decimal("5"),
            variants=[
                {"id": "large", "name": "Large", "price": "120.00"},
            ],
            addons=[
                {"id": "cheese", "name": "Extra cheese", "price": "15.00"},
            ],
            image="https://images.example.test/meal.png",
        )
        PricingSettings.objects.create(
            delivery_fee=Decimal("20.00"),
            delivery_platform_fee=Decimal("10.00"),
            pickup_platform_fee=Decimal("10.00"),
            minimum_commission=Decimal("1.00"),
        )
        self.client = APIClient()

    def login_customer(self):
        response = self.client.post(
            "/api/auth/login",
            {"email": self.customer.email, "password": "SafePassword!2026"},
            format="json",
        )
        body = response.json()
        self.assertEqual(response.status_code, 200, body)
        self.assertIn("accessToken", body)
        self.assertIn("refreshToken", body)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {body['accessToken']}"
        )
        return body

    def order_payload(self, **overrides):
        payload = {
            "orderType": "delivery",
            "deliveryAddress": "12 Main Road, Delhi",
            "items": [
                {
                    "foodId": self.food.id,
                    "variantId": "large",
                    "addonIds": ["cheese"],
                    "quantity": 2,
                    "price": 0.01,
                }
            ],
        }
        payload.update(overrides)
        return payload

    def test_auth_login_me_logout_and_refresh_revocation(self):
        tokens = self.login_customer()
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["role"], AccountProfile.Role.CUSTOMER)

        logout = self.client.post(
            "/api/auth/logout",
            {"refreshToken": tokens["refreshToken"]},
            format="json",
        )
        self.assertEqual(logout.status_code, 200)
        refresh = self.client.post(
            "/api/auth/refresh",
            {"refresh": tokens["refreshToken"]},
            format="json",
        )
        self.assertEqual(refresh.status_code, 401)
        self.assertEqual(set(refresh.json()), {"detail"})

    def test_register_creates_customer_without_session_authentication(self):
        response = self.client.post(
            "/api/auth/register",
            {
                "fullname": "New Customer",
                "email": "new@example.test",
                "mobile": "9333333333",
                "password": "GoodPass!2026",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["redirect"], "/login.html")
        self.assertNotIn("sessionid", response.cookies)

    def test_public_menu_returns_absolute_image_url(self):
        response = self.client.get("/api/customer/foods")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()[0]["image"], "https://images.example.test/meal.png"
        )
        self.assertEqual(response.json()[0]["variants"][0]["id"], "large")
        self.assertEqual(response.json()[0]["addons"][0]["id"], "cheese")

    def test_api_errors_use_detail_and_cart_endpoint_is_removed(self):
        unauthorized = self.client.get("/api/customer/orders/history")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(set(unauthorized.json()), {"detail"})

        cart = self.client.post("/api/customer/cart", {"items": []}, format="json")
        self.assertEqual(cart.status_code, 404)
        self.assertEqual(set(cart.json()), {"detail"})

    def test_quote_and_order_prices_are_calculated_from_database(self):
        self.login_customer()
        payload = self.order_payload()

        quote = self.client.post("/api/customer/checkout/quote", payload, format="json")
        quote_data = quote.json()
        self.assertEqual(quote.status_code, 200, quote_data)
        self.assertEqual(quote_data["itemTotal"], 270.0)
        self.assertEqual(quote_data["customerTotal"], 300.0)

        created = self.client.post("/api/customer/orders", payload, format="json")
        created_data = created.json()
        self.assertEqual(created.status_code, 201, created_data)
        order = Order.objects.get(pk=created_data["orderId"])
        line = OrderItem.objects.get(order=order)
        self.assertEqual(order.item_total, Decimal("270.00"))
        self.assertEqual(line.unit_price, Decimal("135.00"))
        self.assertEqual(line.variant_id, "large")
        self.assertEqual(line.addons[0]["id"], "cheese")
        self.assertEqual(self.food.__class__.objects.get(pk=self.food.pk).stock_quantity, 3)

    def test_order_rejects_unknown_addons_and_overall_stock_excess(self):
        self.login_customer()
        invalid_addon = self.order_payload(
            items=[
                {
                    "foodId": self.food.id,
                    "variantId": "large",
                    "addonIds": ["not-a-real-addon"],
                    "quantity": 1,
                }
            ]
        )
        response = self.client.post(
            "/api/customer/orders", invalid_addon, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.json())
        self.assertEqual(Order.objects.count(), 0)

        duplicate_lines = self.order_payload(
            items=[
                {"foodId": self.food.id, "variantId": "large", "quantity": 3},
                {"foodId": self.food.id, "variantId": "large", "quantity": 3},
            ]
        )
        response = self.client.post(
            "/api/customer/orders", duplicate_lines, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(
            Food.objects.get(pk=self.food.pk).stock_quantity, Decimal("5")
        )

    @override_settings(CASHFREE_ENABLED=True, CASHFREE_SECRET_KEY="test-webhook-secret")
    def test_cashfree_webhook_requires_and_verifies_signature(self):
        body = json.dumps({"data": {"order": {}, "payment": {}}}).encode()
        invalid = self.client.generic(
            "POST",
            "/api/payments/cashfree/webhook",
            body,
            content_type="application/json",
            HTTP_X_WEBHOOK_TIMESTAMP="123",
            HTTP_X_WEBHOOK_SIGNATURE="invalid",
        )
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(set(invalid.json()), {"detail"})

        timestamp = "123"
        signature = base64.b64encode(
            hmac.new(
                b"test-webhook-secret",
                timestamp.encode() + body,
                hashlib.sha256,
            ).digest()
        ).decode()
        verified = self.client.generic(
            "POST",
            "/api/payments/cashfree/webhook",
            body,
            content_type="application/json",
            HTTP_X_WEBHOOK_TIMESTAMP=timestamp,
            HTTP_X_WEBHOOK_SIGNATURE=signature,
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json(), {"received": True})

    def test_docs_and_health_endpoints_are_available(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/docs/").status_code, 200)
        schema = self.client.get("/api/schema/")
        self.assertEqual(schema.status_code, 200)
        self.assertIn(b"/api/auth/login", schema.content)

    def test_restaurant_certificate_routes_resolve_and_return_absolute_urls(self):
        user_model = get_user_model()
        owner = user_model.objects.create_user(
            username="certificate-owner@example.test",
            email="certificate-owner@example.test",
            first_name="Test Restaurant",
            password="SafePassword!2026",
        )
        restaurant = Restaurant.objects.create(
            owner=owner,
            name="Test Restaurant",
            email="certificate-restaurant@example.test",
            approved=True,
            certificate_issued_at=timezone.now(),
        )
        AccountProfile.objects.create(
            user=owner,
            role=AccountProfile.Role.RESTAURANT,
            approved=True,
        )
        access_token = RefreshToken.for_user(owner).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

        response = self.client.get("/api/restaurant/certificate")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(
            response.json()["downloadUrl"],
            f"http://testserver{reverse('restaurant-certificate-download')}",
        )
        self.assertEqual(
            response.json()["verificationUrl"],
            "http://testserver"
            + reverse(
                "verify-certificate",
                kwargs={"registration_number": restaurant.registration_number},
            ),
        )
        download = self.client.get(reverse("restaurant-certificate-download"))
        self.assertEqual(download.status_code, 200)
        self.assertTrue(download.content.startswith(b"%PDF"))

        verification = self.client.get(
            reverse(
                "verify-certificate",
                kwargs={"registration_number": restaurant.registration_number},
            )
        )
        self.assertEqual(verification.status_code, 200)
        self.assertTrue(verification.json()["verified"])
