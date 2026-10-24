"""The five business rules and the small task workflow.

Rule outcomes are True (match), False (clear), or None (needs data).
Missing data is never evidence that an existing problem has cleared.
"""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import AuditEvent, Order, RuleSettings, Task


# Follow-up deadlines start when an exception is first detected.
# These are different from the order-age thresholds in RuleSettings.
RULES = {
    "unconfirmed": {
        "label": "Unconfirmed order", "priority": "Medium", "hours": 2,
        "action": "Contact the customer and confirm the order.",
    },
    "dispatch_delay": {
        "label": "Dispatch delay", "priority": "High", "hours": 2,
        "action": "Check the dispatch blockage and arrange dispatch.",
    },
    "shipment_delay": {
        "label": "Shipment delay", "priority": "Medium", "hours": 4,
        "action": "Check tracking with logistics and update the customer.",
    },
    "failed_delivery": {
        "label": "Failed delivery", "priority": "High", "hours": 0,
        "action": "Contact the customer or courier now and arrange another attempt.",
    },
    "high_value": {
        "label": "High-value overdue order", "priority": "High", "hours": 1,
        "action": "Escalate to the manager and prioritize delivery follow-up.",
    },
}


def get_rule_settings():
    settings, _ = RuleSettings.objects.get_or_create(pk=1)
    return settings


def add_audit(task, action, details, actor=None, now=None):
    return AuditEvent.objects.create(
        order=task.order, task=task, actor=actor,
        actor_name=actor.username if actor else "System",
        action=action, details=details, timestamp=now or timezone.now(),
    )


def delivery_is_late(order, now):
    if order.expected_delivery_at:
        return now > order.expected_delivery_at
    if order.expected_delivery_date:
        # A date-only promise remains valid through the whole business day.
        return timezone.localtime(now).date() > order.expected_delivery_date
    return None


def check_order(order, settings, now):
    """Return {rule: (result, explanation)} without changing the database."""
    results = {code: (False, "") for code in RULES}
    if order.status == "unconfirmed":
        if not order.ordered_at:
            results["unconfirmed"] = (None, "Unconfirmed rule: order time is missing; a date alone cannot establish two-hour age.")
        else:
            late = now - order.ordered_at > timedelta(hours=settings.unconfirmed_hours)
            results["unconfirmed"] = (late, f"Order remains unconfirmed beyond {settings.unconfirmed_hours} hours.")

    if order.status == "confirmed" and not (order.dispatch_at or order.dispatch_date):
        if not order.confirmed_at:
            results["dispatch_delay"] = (None, "Dispatch rule: confirmation timestamp is missing.")
        else:
            late = now - order.confirmed_at > timedelta(hours=settings.dispatch_hours)
            results["dispatch_delay"] = (late, f"Confirmed order has not been dispatched beyond {settings.dispatch_hours} hours.")

    if order.status in ["dispatched", "delivery_failed"]:
        late = delivery_is_late(order, now)
        reason = "Shipment rule: expected delivery date is missing." if late is None else "Dispatched order is past its expected delivery date without delivery."
        results["shipment_delay"] = (late, reason)

    if order.status == "delivery_failed":
        results["failed_delivery"] = (True, "Delivery attempt failed and needs immediate follow-up.")

    if order.status not in ["delivered", "cancelled"] and order.order_value > settings.high_value_amount:
        late = delivery_is_late(order, now)
        reason = "High-value rule: expected delivery date is missing." if late is None else f"Order value exceeds Rs {settings.high_value_amount} and delivery is overdue."
        results["high_value"] = (late, reason)
    return results


@transaction.atomic
def evaluate_orders(now=None):
    """Safe to run after an import, a rule change, or every minute."""
    now = now or timezone.now()
    settings = get_rule_settings()
    counts = {"created": 0, "resolved": 0, "needs_data": 0}
    for order in Order.objects.select_related("assigned_employee").all():
        issues = []
        for code, (result, reason) in check_order(order, settings, now).items():
            current = Task.objects.filter(order=order, rule_code=code, condition_active=True).first()
            if result is None:
                issues.append(reason)
                continue
            if result:
                if current:
                    # Keep workflow edits and first detection/deadline unchanged.
                    if current.reason != reason:
                        current.reason = reason
                        current.save(update_fields=["reason"])
                    continue
                last = Task.objects.filter(order=order, rule_code=code).aggregate(value=Max("occurrence"))["value"] or 0
                rule = RULES[code]
                task = Task.objects.create(
                    order=order, rule_code=code, occurrence=last + 1,
                    reason=reason, priority=rule["priority"],
                    assigned_employee=order.assigned_employee,
                    action_required=rule["action"],
                    deadline=now + timedelta(hours=rule["hours"]), detected_at=now,
                )
                add_audit(task, "Assigned", f"Automatically assigned to {task.assigned_employee.username}.", now=now)
                counts["created"] += 1
            elif current:
                current.condition_active = False
                if current.status != "Resolved":
                    old_status = current.status
                    current.status = "Resolved"
                    current.resolved_at = now
                    add_audit(current, "Status changed", f"{old_status} → Resolved: source data or rule settings cleared the condition.", now=now)
                    counts["resolved"] += 1
                current.save(update_fields=["condition_active", "status", "resolved_at"])
        if order.rule_issues != issues:
            order.rule_issues = issues
            order.save(update_fields=["rule_issues"])
        if issues:
            counts["needs_data"] += 1
    return counts


@transaction.atomic
def update_task(task, actor, status=None, assignee=None):
    """Reassigning never changes a deadline or restarts the SLA."""
    task = Task.objects.select_for_update().get(pk=task.pk)
    if not actor.is_staff and task.assigned_employee_id != actor.pk:
        raise PermissionDenied
    if assignee is not None and assignee.pk != task.assigned_employee_id:
        if not actor.is_staff:
            raise PermissionDenied
        if not assignee.is_active:
            raise ValidationError("Choose an active employee.")
        old = task.assigned_employee.username
        task.assigned_employee = assignee
        add_audit(task, "Reassigned", f"{old} → {assignee.username}. Deadline unchanged.", actor)
    if status is not None and status != task.status:
        if status not in dict(Task.STATUS_CHOICES):
            raise ValidationError("Choose a valid task status.")
        if status != "Resolved" and not task.condition_active:
            raise ValidationError("This source condition has cleared. A new incident will create a new task if needed.")
        old = task.status
        task.status = status
        task.resolved_at = timezone.now() if status == "Resolved" else None
        add_audit(task, "Status changed", f"{old} → {status}.", actor)
    task.save(update_fields=["assigned_employee", "status", "resolved_at"])
    return task
