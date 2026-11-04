from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0019_order_settlement_tracking")]

    operations = [
        migrations.AddField(
            model_name="order",
            name="order_day",
            field=models.DateField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="daily_order_number",
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.UniqueConstraint(
                fields=("order_day", "daily_order_number"),
                name="unique_daily_order_number",
            ),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="variant_name",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.CreateModel(
            name="DailyOrderSequence",
            fields=[
                ("day", models.DateField(primary_key=True, serialize=False)),
                ("last_number", models.PositiveIntegerField(default=0)),
            ],
        ),
    ]
