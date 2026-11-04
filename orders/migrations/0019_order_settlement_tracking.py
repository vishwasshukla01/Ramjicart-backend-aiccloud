from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0018_order_acceptance_refunds")]
    operations = [
        migrations.AddField(
            model_name="order",
            name="settlement_status",
            field=models.CharField(
                choices=[("unsettled", "Unsettled"), ("settled", "Settled"), ("failed", "Failed")],
                default="unsettled",
                max_length=12,
            ),
        ),
        migrations.AddField(model_name="order", name="settled_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="order", name="settlement_reference", field=models.CharField(blank=True, max_length=120)),
    ]
