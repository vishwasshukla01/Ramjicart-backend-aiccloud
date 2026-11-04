from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0014_category_metadata")]
    operations = [
        migrations.AddField(
            model_name="food",
            name="variants",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="food",
            name="addons",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
