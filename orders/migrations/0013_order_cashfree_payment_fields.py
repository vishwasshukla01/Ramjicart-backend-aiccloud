from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0012_accountprofile_firebase_uid")]

    operations = [
        migrations.AddField(
            model_name="order",
            name="gateway_amount",
            field=models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=10),
        ),
        migrations.AddField(
            model_name="order",
            name="gateway_payment_status",
            field=models.CharField(default="not_required", max_length=24),
        ),
        migrations.AddField(
            model_name="order",
            name="cashfree_order_id",
            field=models.CharField(blank=True, max_length=45, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="order",
            name="cashfree_payment_session_id",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="order",
            name="cashfree_payment_id",
            field=models.CharField(blank=True, max_length=80),
        ),
    ]
