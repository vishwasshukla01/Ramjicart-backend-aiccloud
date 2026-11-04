from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import migrations, models


def copy_delivery_platform_fee_to_pickup(apps, schema_editor):
    pricing_settings = apps.get_model("orders", "PricingSettings")
    for settings_row in pricing_settings.objects.all().iterator():
        settings_row.pickup_platform_fee = settings_row.delivery_platform_fee
        settings_row.save(update_fields=["pickup_platform_fee"])


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0005_restaurant_city_restaurant_order_notifications_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pricingsettings",
            name="delivery_platform_fee",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("10.00"),
                help_text="Customer platform fee charged on delivery orders.",
                max_digits=8,
                validators=[MinValueValidator(Decimal("0"))],
            ),
        ),
        migrations.AddField(
            model_name="pricingsettings",
            name="pickup_platform_fee",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("10.00"),
                help_text="Customer platform fee charged on pickup orders.",
                max_digits=8,
                validators=[MinValueValidator(Decimal("0"))],
            ),
        ),
        migrations.RunPython(
            copy_delivery_platform_fee_to_pickup,
            migrations.RunPython.noop,
        ),
    ]