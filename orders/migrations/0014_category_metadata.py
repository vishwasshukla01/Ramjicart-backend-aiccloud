import django.core.validators
from decimal import Decimal
from django.db import migrations, models
from django.utils.text import slugify


def seed_categories(apps, schema_editor):
    Category = apps.get_model("orders", "Category")
    defaults = [
        ("Groceries", "groceries", "🛒", "#e8f5e9"),
        ("Fast Food", "fast-food", "🍔", "#fff0e6"),
        ("Drinks & Juice", "drinks-juice", "🥤", "#e8f4ff"),
        ("Fruits", "fruits", "🍎", "#fff1ef"),
        ("Vegetables", "vegetables", "🥦", "#edf7e9"),
        ("Stationery", "stationery", "✏️", "#f1edff"),
        ("Other", "other", "🧺", "#f2f4f7"),
    ]
    for sort_order, (name, slug, icon, color) in enumerate(defaults, 1):
        category, _ = Category.objects.get_or_create(name=name)
        category.slug = slug
        category.icon = icon
        category.color = color
        category.is_active = True
        category.sort_order = sort_order
        category.save()
    reserved = {row[1] for row in defaults}
    for category in Category.objects.exclude(name__in=[row[0] for row in defaults]):
        base_slug = slugify(category.name) or f"category-{category.pk}"
        category.slug = base_slug if base_slug not in reserved else f"{base_slug}-{category.pk}"
        reserved.add(category.slug)
        category.save()


class Migration(migrations.Migration):
    dependencies = [("orders", "0013_order_cashfree_payment_fields")]
    operations = [
        migrations.AddField(model_name="category", name="slug", field=models.SlugField(blank=True, db_index=False, max_length=120)),
        migrations.AddField(model_name="category", name="icon", field=models.CharField(blank=True, max_length=32)),
        migrations.AddField(model_name="category", name="color", field=models.CharField(default="#fff3e8", max_length=20)),
        migrations.AddField(model_name="category", name="is_active", field=models.BooleanField(default=True)),
        migrations.AddField(model_name="category", name="sort_order", field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="restaurant", name="rating", field=models.DecimalField(blank=True, decimal_places=1, max_digits=2, null=True, validators=[django.core.validators.MinValueValidator(Decimal("0")), django.core.validators.MaxValueValidator(Decimal("5"))])),
        migrations.AddField(model_name="restaurant", name="delivery_time", field=models.CharField(default="30–45 min", max_length=40)),
        migrations.RunPython(seed_categories, migrations.RunPython.noop),
        migrations.AlterField(model_name="category", name="slug", field=models.SlugField(blank=True, max_length=120, unique=True)),
    ]
