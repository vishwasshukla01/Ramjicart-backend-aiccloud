from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0011_alter_order_delivery_latitude_and_more")]

    operations = [
        migrations.AddField(
            model_name="accountprofile",
            name="firebase_uid",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=128,
                null=True,
                unique=True,
            ),
        ),
    ]
