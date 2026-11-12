from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0023_order_item_options_and_option_ids"),
    ]

    operations = [
        migrations.AddField(
            model_name="food",
            name="image_file",
            field=models.ImageField(blank=True, upload_to="food-images/"),
        ),
    ]
