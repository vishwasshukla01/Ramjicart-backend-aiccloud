from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0020_daily_order_numbers_and_item_variants")]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, db_index=True),
        ),
    ]
