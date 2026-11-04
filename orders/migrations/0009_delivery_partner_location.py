from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0008_restaurant_market_landmark")]

    operations = [
        migrations.AddField(
            model_name="accountprofile",
            name="latitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="accountprofile",
            name="longitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="accountprofile",
            name="location_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
