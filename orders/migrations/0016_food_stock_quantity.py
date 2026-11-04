from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0015_food_menu_options")]
    operations = [
        migrations.AddField(
            model_name="food",
            name="stock_quantity",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
