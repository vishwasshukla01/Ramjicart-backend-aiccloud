from django.contrib import admin

from .models import (
    AccountProfile,
    Category,
    Food,
    Order,
    OrderItem,
    PricingSettings,
    Restaurant,
)


@admin.register(AccountProfile)
class AccountProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "phone", "approved", "created_at")
    list_filter = ("role", "approved")
    search_fields = ("user__email", "user__first_name", "phone")
    readonly_fields = ("user", "role", "phone", "created_at")


@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "email",
        "registration_number",
        "approved",
        "certificate_issued_at",
        "accepting_orders",
        "rating",
        "delivery_time",
        "commission_percentage",
        "max_commission_percentage",
    )
    list_filter = ("approved", "accepting_orders")
    search_fields = (
        "name",
        "email",
        "phone",
        "nearby_landmark",
        "market",
        "city",
        "pincode",
    )
    fieldsets = (
        (
            "Restaurant",
            {
                "fields": (
                    "owner",
                    "name",
                    "owner_name",
                    "email",
                    "registration_number",
                    "certificate_issued_at",
                    "phone",
                    "address",
                    "nearby_landmark",
                    "market",
                    "city",
                    "pincode",
                    "latitude",
                    "longitude",
                    "logo",
                    "rating",
                    "delivery_time",
                )
            },
        ),
        (
            "Approval and commission",
            {
                "fields": (
                    "approved",
                    "accepting_orders",
                    "commission_percentage",
                    "max_commission_percentage",
                )
            },
        ),
    )
    readonly_fields = ("registration_number", "certificate_issued_at")


@admin.register(PricingSettings)
class PricingSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "delivery_fee",
        "pickup_platform_fee",
        "delivery_platform_fee",
        "minimum_commission",
        "updated_at",
    )
    fields = ("delivery_fee", "pickup_platform_fee", "delivery_platform_fee")

    def has_add_permission(self, request):
        return not PricingSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Food)
class FoodAdmin(admin.ModelAdmin):
    list_display = ("name", "restaurant", "category", "price", "available")
    list_filter = ("available", "restaurant", "category")
    search_fields = ("name", "restaurant__name")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "sort_order")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("sort_order", "name")


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    readonly_fields = ("food", "food_name", "unit_price", "quantity", "line_total")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "restaurant",
        "order_type",
        "status",
        "customer_total",
        "restaurant_payout",
        "settlement_status",
        "settled_at",
        "created_at",
    )
    list_filter = ("status", "order_type", "settlement_status", "restaurant")
    search_fields = ("id", "customer_name", "customer_phone")
    readonly_fields = (
        "customer",
        "customer_name",
        "customer_email",
        "customer_phone",
        "delivery_address",
        "delivery_latitude",
        "delivery_longitude",
        "restaurant",
        "order_type",
        "status",
        "item_total",
        "commission_percentage",
        "commission_amount",
        "delivery_fee",
        "platform_fee",
        "customer_total",
        "restaurant_payout",
        "created_at",
        "paid_at",
        "settled_at",
    )
    inlines = (OrderItemInline,)

    def save_model(self, request, obj, form, change):
        if obj.settlement_status == Order.SettlementStatus.SETTLED and obj.settled_at is None:
            from django.utils import timezone
            obj.settled_at = timezone.now()
        super().save_model(request, obj, form, change)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
