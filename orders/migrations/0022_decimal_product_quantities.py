from django.db import migrations, models
import django.core.validators
from decimal import Decimal


def set_existing_grocery_units(apps, schema_editor):
    Food = apps.get_model("orders", "Food")
    Food.objects.filter(category__slug__in=["groceries", "fruits", "vegetables"]).update(quantity_unit="kg")

class Migration(migrations.Migration):
    dependencies = [("orders", "0021_order_created_at_index")]
    operations = [
        migrations.AlterField(model_name="food", name="stock_quantity", field=models.DecimalField(blank=True, decimal_places=2, max_digits=9, null=True)),
        migrations.AddField(model_name="food", name="quantity_unit", field=models.CharField(choices=[("piece", "Piece"), ("kg", "Kilogram")], default="piece", max_length=8)),
        migrations.RunPython(set_existing_grocery_units, migrations.RunPython.noop),
        migrations.AlterField(model_name="orderitem", name="quantity", field=models.DecimalField(decimal_places=2, max_digits=7, validators=[django.core.validators.MinValueValidator(Decimal("0.05"))])),
    ]
