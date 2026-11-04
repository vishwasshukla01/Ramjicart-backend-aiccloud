from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0016_food_stock_quantity")]
    operations = [
        migrations.AddField(model_name="restaurant", name="opening_hours_enabled", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="restaurant", name="opens_at", field=models.TimeField(blank=True, null=True)),
        migrations.AddField(model_name="restaurant", name="closes_at", field=models.TimeField(blank=True, null=True)),
    ]
