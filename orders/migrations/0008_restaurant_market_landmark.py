from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0007_restaurant_certificate"),
    ]

    operations = [
        migrations.AddField(
            model_name="restaurant",
            name="nearby_landmark",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="restaurant",
            name="market",
            field=models.CharField(blank=True, max_length=160),
        ),
    ]
