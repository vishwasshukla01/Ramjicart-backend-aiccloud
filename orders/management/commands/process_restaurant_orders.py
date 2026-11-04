from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from orders.models import Order
from orders.views import _request_order_refund, _restore_order_stock


class Command(BaseCommand):
    help = "Cancel expired restaurant orders and retry payment refunds."

    def handle(self, *args, **options):
        now = timezone.now()
        cancel_after = now - timedelta(minutes=15)
        cancelled = 0

        pending_ids = list(
            Order.objects.filter(status=Order.Status.PLACED)
            .values_list("pk", flat=True)
        )
        for order_id in pending_ids:
            should_cancel = False
            with transaction.atomic():
                order = Order.objects.select_for_update().filter(
                    pk=order_id, status=Order.Status.PLACED
                ).first()
                if order is None:
                    continue
                accepted_window_start = order.paid_at or order.created_at
                if accepted_window_start <= cancel_after:
                    order.status = Order.Status.CANCELLED
                    order.rejection_reason = "Automatically cancelled because the restaurant did not accept within 15 minutes."
                    order.save(update_fields=["status", "rejection_reason"])
                    _restore_order_stock(order)
                    should_cancel = True
            if should_cancel:
                order.refresh_from_db()
                _request_order_refund(order)
                cancelled += 1

        retry_ids = Order.objects.filter(
            status__in=[Order.Status.CANCELLED, Order.Status.PAYMENT_FAILED],
            refund_status__in=["pending", "failed"],
            gateway_payment_status="success",
        ).exclude(cashfree_order_id="").values_list("pk", flat=True)
        for order in Order.objects.filter(pk__in=retry_ids).select_related("restaurant"):
            _request_order_refund(order)

        retention_cutoff = now - timedelta(days=15)
        deletable_orders = Order.objects.filter(
            created_at__lt=retention_cutoff,
            status__in=[
                Order.Status.COMPLETED,
                Order.Status.CANCELLED,
                Order.Status.PAYMENT_FAILED,
            ],
        ).exclude(gateway_payment_status="pending").exclude(
            refund_status__in=["pending", "failed", "manual_review", "processing", "queued", "initiated"]
        )
        deleted_count, _ = deletable_orders.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Restaurant order check complete: {cancelled} auto-cancel(s); "
                f"{deleted_count} old record(s) removed."
            )
        )
