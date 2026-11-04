import orders.models
from django.db import migrations, models
from django.utils import timezone


def populate_certificate_fields(apps, schema_editor):
    Restaurant = apps.get_model("orders", "Restaurant")
    AccountProfile = apps.get_model("orders", "AccountProfile")
    for restaurant in Restaurant.objects.all().iterator():
        year = restaurant.created_at.year if restaurant.created_at else timezone.now().year
        restaurant.registration_number = f"HC-{year}-{restaurant.pk:06d}"
        profile_approved = (
            restaurant.owner_id
            and AccountProfile.objects.filter(
                user_id=restaurant.owner_id,
                role="restaurant",
                approved=True,
            ).exists()
        )
        if restaurant.approved and profile_approved:
            restaurant.certificate_issued_at = restaurant.created_at or timezone.now()
        restaurant.save(
            update_fields=["registration_number", "certificate_issued_at"]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0006_pickup_platform_fee"),
    ]

    operations = [
        migrations.AddField(
            model_name="restaurant",
            name="registration_number",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=32,
                null=True,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="restaurant",
            name="certificate_issued_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(
            populate_certificate_fields,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="restaurant",
            name="registration_number",
            field=models.CharField(
                blank=True,
                default=orders.models.generate_registration_number,
                editable=False,
                max_length=32,
                unique=True,
            ),
        ),
    ]
