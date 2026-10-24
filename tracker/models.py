from django.conf import settings
from django.db import models
from django.utils import timezone


class Order(models.Model):
    STATUS_CHOICES = [(s, s.replace("_", " ").title()) for s in [
        "unconfirmed", "confirmed", "dispatched", "delivery_failed", "delivered", "cancelled",
    ]]
    order_id = models.CharField(max_length=64, unique=True)
    order_date = models.DateField()
    ordered_at = models.DateTimeField(null=True, blank=True)
    customer_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=32)
    order_value = models.DecimalField(max_digits=12, decimal_places=2)
    payment_type = models.CharField(max_length=10, choices=[("prepaid", "Prepaid"), ("cod", "COD")])
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    dispatch_date = models.DateField(null=True, blank=True)
    dispatch_at = models.DateTimeField(null=True, blank=True)
    expected_delivery_date = models.DateField(null=True, blank=True)
    expected_delivery_at = models.DateTimeField(null=True, blank=True)
    assigned_employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    data_issues = models.JSONField(default=list, blank=True)
    rule_issues = models.JSONField(default=list, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-order_date", "order_id"]

    @property
    def all_issues(self):
        return self.data_issues + self.rule_issues

    def __str__(self):
        return self.order_id


class ImportBatch(models.Model):
    filename = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=12, default="preview")
    preview = models.JSONField(default=list)
    issues = models.JSONField(default=list)
    summary = models.JSONField(default=dict)


class RuleSettings(models.Model):
    unconfirmed_hours = models.PositiveIntegerField(default=2)
    dispatch_hours = models.PositiveIntegerField(default=24)
    high_value_amount = models.DecimalField(max_digits=12, decimal_places=2, default=5000)


class Task(models.Model):
    RULE_CHOICES = [
        ("unconfirmed", "Unconfirmed order"), ("dispatch_delay", "Dispatch delay"),
        ("shipment_delay", "Shipment delay"), ("failed_delivery", "Failed delivery"),
        ("high_value", "High-value overdue order"),
    ]
    STATUS_CHOICES = [(s, s) for s in ["Open", "In Progress", "Resolved"]]
    PRIORITY_CHOICES = [(s, s) for s in ["High", "Medium", "Low"]]
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="tasks")
    rule_code = models.CharField(max_length=24, choices=RULE_CHOICES)
    occurrence = models.PositiveIntegerField(default=1)
    condition_active = models.BooleanField(default=True)
    reason = models.TextField()
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES)
    assigned_employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action_required = models.TextField()
    deadline = models.DateTimeField()
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="Open")
    detected_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["deadline", "id"]
        constraints = [
            models.UniqueConstraint(fields=["order", "rule_code", "occurrence"], name="unique_rule_occurrence"),
            models.UniqueConstraint(fields=["order", "rule_code"], condition=models.Q(condition_active=True), name="one_current_rule_incident"),
        ]

    @property
    def is_overdue(self):
        return self.status != "Resolved" and self.deadline < timezone.now()


class AuditEvent(models.Model):
    order = models.ForeignKey(Order, on_delete=models.PROTECT)
    task = models.ForeignKey(Task, on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    actor_name = models.CharField(max_length=150)
    action = models.CharField(max_length=60)
    details = models.TextField(blank=True)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-timestamp", "-id"]
