import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0017_restaurant_opening_hours")]
    operations = [
        migrations.AddField(model_name="order", name="rejection_reason", field=models.CharField(blank=True, max_length=300)),
        migrations.AddField(model_name="order", name="preparing_minutes", field=models.PositiveSmallIntegerField(blank=True, null=True, validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(240)])),
        migrations.AddField(model_name="order", name="accepted_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="order", name="reminder_sent_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="order", name="refund_status", field=models.CharField(default="not_required", max_length=16)),
        migrations.AddField(model_name="order", name="refund_id", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="order", name="refund_error", field=models.TextField(blank=True)),
        migrations.AddField(model_name="order", name="refund_requested_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="order", name="refund_attempts", field=models.PositiveSmallIntegerField(default=0)),
    ]
