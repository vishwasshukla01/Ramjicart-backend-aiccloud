import uuid

from django.db import migrations, models


def assign_menu_option_ids(apps, schema_editor):
    Food = apps.get_model("orders", "Food")
    for food in Food.objects.all().iterator():
        changed_fields = []
        for field in ("variants", "addons"):
            options = getattr(food, field) or []
            updated = []
            changed = False
            for option in options:
                if isinstance(option, dict):
                    option = dict(option)
                    if not option.get("id"):
                        option["id"] = str(uuid.uuid4())
                        changed = True
                updated.append(option)
            if changed:
                setattr(food, field, updated)
                changed_fields.append(field)
        if changed_fields:
            food.save(update_fields=changed_fields)


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0022_decimal_product_quantities"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderitem",
            name="variant_id",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="addons",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(assign_menu_option_ids, migrations.RunPython.noop),
    ]
