from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError

from .models import PricingSettings


CENT = Decimal("0.01")


def money(amount):
    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)


def quote_order(restaurant, item_total, order_type):
    item_total = money(item_total)
    if item_total <= 0:
        raise ValidationError("Cart total must be greater than zero.")
    if order_type not in {"pickup", "delivery"}:
        raise ValidationError("Order type must be pickup or delivery.")
    if restaurant.commission_percentage > restaurant.max_commission_percentage:
        raise ValidationError("Restaurant commission exceeds its approved maximum.")

    settings_row = PricingSettings.current()
    commission = money(
        max(
            settings_row.minimum_commission,
            item_total * restaurant.commission_percentage / Decimal("100"),
        )
    )
    commission = min(commission, item_total)
    delivery_fee = settings_row.delivery_fee if order_type == "delivery" else Decimal("0")
    platform_fee = (
        settings_row.delivery_platform_fee
        if order_type == "delivery"
        else settings_row.pickup_platform_fee
    )
    customer_total = money(item_total + delivery_fee + platform_fee)
    restaurant_payout = money(item_total - commission)
    online_payment_amount = money(commission + delivery_fee + platform_fee)

    return {
        "orderType": order_type,
        "itemTotal": item_total,
        "commissionPercentage": restaurant.commission_percentage,
        "commissionAmount": commission,
        "deliveryFee": delivery_fee,
        "platformFee": platform_fee,
        "customerTotal": customer_total,
        "restaurantPayout": restaurant_payout,
        "gatewayAmount": online_payment_amount,
        "onlinePaymentAmount": online_payment_amount,
        "payLaterAmount": restaurant_payout,
        "payLaterRecipient": (
            "delivery partner" if order_type == "delivery" else "restaurant"
        ),
        "platformEarnings": money(
            commission + delivery_fee + platform_fee
        ),
    }
