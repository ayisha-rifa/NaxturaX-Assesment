"""Business examples and interview scenarios, using an isolated test database."""
import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from .importer import commit_import, preview_import
from .management.commands.create_demo import HEADERS
from .models import AuditEvent, ImportBatch, Order, Task
from .rules import check_order, delivery_is_late, evaluate_orders, get_rule_settings, update_task


class OrderTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user("manager", password="DemoPass123!", is_staff=True)
        self.asha = User.objects.create_user("asha", first_name="Asha")
        self.ravi = User.objects.create_user("ravi", first_name="Ravi")
        self.now = datetime(2026, 10, 23, 15, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    def make_order(self, **changes):
        values = {
            "order_id": f"ORDER-{Order.objects.count() + 1}", "order_date": (self.now - timedelta(days=5)).date(),
            "ordered_at": self.now - timedelta(days=5), "customer_name": "Example Customer",
            "phone": "09876543210", "order_value": Decimal("1200.00"), "payment_type": "cod",
            "status": "unconfirmed", "assigned_employee": self.asha,
            "expected_delivery_date": (self.now + timedelta(days=2)).date(),
        }
        values.update(changes)
        return Order.objects.create(**values)

    def csv_file(self, rows, name="orders.csv"):
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)
        return SimpleUploadedFile(name, stream.getvalue().encode("utf-8-sig"), content_type="text/csv")

    def import_row(self, **changes):
        row = {
            "Order ID": "UPLOAD-1", "Order date": (self.now - timedelta(days=3)).isoformat(),
            "Customer name": "Upload Customer", "Phone number": "09876543210", "Order value": "1250.00",
            "Payment type": "COD", "Order status": "unconfirmed", "Dispatch date": "",
            "Expected delivery date": (self.now + timedelta(days=2)).date().isoformat(),
            "Assigned employee": "asha", "Confirmed at": "",
        }
        row.update(changes)
        return row


class RuleTests(OrderTestCase):
    def test_unconfirmed_strict_two_hour_boundary(self):
        order = self.make_order(ordered_at=self.now - timedelta(hours=2))
        settings = get_rule_settings()
        self.assertFalse(check_order(order, settings, self.now)["unconfirmed"][0])
        self.assertTrue(check_order(order, settings, self.now + timedelta(seconds=1))["unconfirmed"][0])

    def test_dispatch_boundary_and_missing_confirmation(self):
        order = self.make_order(status="confirmed", confirmed_at=self.now - timedelta(hours=24))
        settings = get_rule_settings()
        self.assertFalse(check_order(order, settings, self.now)["dispatch_delay"][0])
        self.assertTrue(check_order(order, settings, self.now + timedelta(seconds=1))["dispatch_delay"][0])
        order.confirmed_at = None
        self.assertIsNone(check_order(order, settings, self.now)["dispatch_delay"][0])

    def test_high_value_is_strictly_greater_than_5000(self):
        order = self.make_order(status="dispatched", order_value=Decimal("5000.00"), expected_delivery_date=(self.now - timedelta(days=1)).date())
        settings = get_rule_settings()
        self.assertFalse(check_order(order, settings, self.now)["high_value"][0])
        order.order_value = Decimal("5000.01")
        self.assertTrue(check_order(order, settings, self.now)["high_value"][0])

    def test_date_only_expected_delivery_covers_whole_local_day(self):
        order = self.make_order(expected_delivery_date=self.now.date())
        late_evening = self.now.replace(hour=23, minute=59, second=59)
        self.assertFalse(delivery_is_late(order, late_evening))
        self.assertTrue(delivery_is_late(order, late_evening + timedelta(seconds=1)))

    def test_expected_timestamp_must_have_passed(self):
        order = self.make_order(expected_delivery_at=self.now)
        self.assertFalse(delivery_is_late(order, self.now))
        self.assertTrue(delivery_is_late(order, self.now + timedelta(seconds=1)))

    def test_maximum_supported_expected_date_does_not_overflow(self):
        order = self.make_order(expected_delivery_date=date(9999, 12, 31))
        self.assertFalse(delivery_is_late(order, self.now))

    def test_far_future_order_timestamp_does_not_overflow(self):
        order = self.make_order(ordered_at=datetime(9999, 12, 31, 23, tzinfo=ZoneInfo("UTC")))
        self.assertFalse(check_order(order, get_rule_settings(), self.now)["unconfirmed"][0])

    def test_date_only_order_needs_correction_without_false_exception(self):
        order = self.make_order(ordered_at=None)
        result = evaluate_orders(now=self.now)
        order.refresh_from_db()
        self.assertEqual(result["needs_data"], 1)
        self.assertTrue(order.rule_issues)
        self.assertFalse(Task.objects.exists())

    def test_overlap_creates_both_shipment_and_high_value_tasks(self):
        self.make_order(status="dispatched", order_value=Decimal("7000"), expected_delivery_date=(self.now - timedelta(days=1)).date())
        evaluate_orders(now=self.now)
        self.assertSetEqual(set(Task.objects.values_list("rule_code", flat=True)), {"shipment_delay", "high_value"})

    def test_delivered_and_cancelled_orders_are_not_flagged(self):
        for status in ["delivered", "cancelled"]:
            self.make_order(status=status, order_value=Decimal("7000"), expected_delivery_date=(self.now - timedelta(days=1)).date())
        evaluate_orders(now=self.now)
        self.assertFalse(Task.objects.exists())

    def test_failed_delivery_needs_no_missing_attempt_timestamp(self):
        self.make_order(status="delivery_failed", expected_delivery_date=None)
        evaluate_orders(now=self.now)
        task = Task.objects.get(rule_code="failed_delivery")
        self.assertEqual(task.deadline, self.now)
        self.assertEqual(task.priority, "High")
        self.assertEqual(Task.objects.count(), 1)

    def test_repeated_checks_preserve_workflow_and_do_not_duplicate_audits(self):
        self.make_order()
        evaluate_orders(now=self.now)
        task = Task.objects.get()
        original_deadline = task.deadline
        update_task(task, self.manager, status="In Progress", assignee=self.ravi)
        event_count = AuditEvent.objects.count()
        evaluate_orders(now=self.now + timedelta(hours=2))
        task.refresh_from_db()
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), event_count)
        self.assertEqual(task.assigned_employee, self.ravi)
        self.assertEqual(task.status, "In Progress")
        self.assertEqual(task.deadline, original_deadline)

    def test_missing_evidence_does_not_clear_existing_exception(self):
        order = self.make_order(status="dispatched", expected_delivery_date=(self.now - timedelta(days=1)).date())
        evaluate_orders(now=self.now)
        order.expected_delivery_date = None
        order.save()
        evaluate_orders(now=self.now)
        task = Task.objects.get()
        self.assertEqual(task.status, "Open")
        self.assertTrue(task.condition_active)

    def test_manual_resolution_stays_closed_until_clear_then_recurrence(self):
        order = self.make_order()
        evaluate_orders(now=self.now)
        first = Task.objects.get()
        update_task(first, self.manager, status="Resolved")
        evaluate_orders(now=self.now + timedelta(minutes=1))
        first.refresh_from_db()
        self.assertEqual(first.status, "Resolved")
        self.assertEqual(Task.objects.count(), 1)
        order.status = "delivered"
        order.save()
        evaluate_orders(now=self.now)
        order.status = "unconfirmed"
        order.save()
        evaluate_orders(now=self.now)
        self.assertEqual(Task.objects.count(), 2)
        self.assertEqual(Task.objects.get(condition_active=True).occurrence, 2)

    def test_delivery_update_auto_resolves_with_system_audit(self):
        order = self.make_order()
        evaluate_orders(now=self.now)
        order.status = "delivered"
        order.save()
        evaluate_orders(now=self.now)
        task = Task.objects.get()
        self.assertEqual(task.status, "Resolved")
        self.assertFalse(task.condition_active)
        self.assertTrue(AuditEvent.objects.filter(task=task, action="Status changed", actor_name="System").exists())

    def test_rule_change_24_to_12_creates_new_case_without_duplicates(self):
        self.make_order(status="confirmed", confirmed_at=self.now - timedelta(hours=18))
        evaluate_orders(now=self.now)
        self.assertFalse(Task.objects.exists())
        settings = get_rule_settings()
        settings.dispatch_hours = 12
        settings.save()
        evaluate_orders(now=self.now)
        evaluate_orders(now=self.now)
        self.assertEqual(Task.objects.count(), 1)

    def test_overdue_reassignment_preserves_deadline_and_actor(self):
        self.make_order(status="delivery_failed")
        evaluate_orders(now=timezone.now() - timedelta(hours=1))
        task = Task.objects.get(rule_code="failed_delivery")
        deadline = task.deadline
        update_task(task, self.manager, assignee=self.ravi)
        task.refresh_from_db()
        self.assertEqual(task.deadline, deadline)
        self.assertTrue(task.is_overdue)
        self.assertEqual(task.assigned_employee, self.ravi)
        event = AuditEvent.objects.get(action="Reassigned")
        self.assertEqual(event.actor_name, "manager")
        self.assertEqual(event.order, task.order)

    def test_employee_cannot_update_other_employee_task_or_reassign(self):
        self.make_order()
        evaluate_orders(now=self.now)
        task = Task.objects.get()
        with self.assertRaises(PermissionDenied):
            update_task(task, self.ravi, status="Resolved")
        with self.assertRaises(PermissionDenied):
            update_task(task, self.asha, assignee=self.ravi)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_invalid_status_rolls_back_reassignment_and_its_audit(self):
        self.make_order()
        evaluate_orders(now=self.now)
        task = Task.objects.get()
        with self.assertRaises(ValidationError):
            update_task(task, self.manager, assignee=self.ravi, status="invalid")
        task.refresh_from_db()
        self.assertEqual(task.assigned_employee, self.asha)
        self.assertEqual(AuditEvent.objects.count(), 1)


class ImportTests(OrderTestCase):
    def test_preview_is_read_only_then_commit_persists(self):
        batch = preview_import(self.csv_file([self.import_row()]), self.manager)
        self.assertFalse(Order.objects.exists())
        summary = commit_import(batch, self.manager)
        self.assertEqual(summary["created"], 1)
        self.assertEqual(Order.objects.get().phone, "09876543210")
        self.assertEqual(Order.objects.get().assigned_employee, self.asha)

    def test_invalid_rows_rejected_while_valid_rows_import(self):
        valid = self.import_row()
        bad_date = self.import_row(**{"Order ID": "BAD-DATE", "Order date": "2026-02-30"})
        missing = self.import_row(**{"Order ID": "MISSING", "Customer name": ""})
        bad_money = self.import_row(**{"Order ID": "BAD-MONEY", "Order value": "1.001"})
        batch = preview_import(self.csv_file([valid, bad_date, missing, bad_money]), self.manager)
        self.assertEqual(batch.summary["rejected"], 3)
        self.assertTrue(batch.issues)
        commit_import(batch, self.manager)
        self.assertEqual(Order.objects.count(), 1)

    def test_identical_and_conflicting_duplicates(self):
        row = self.import_row()
        batch = preview_import(self.csv_file([row, row.copy()]), self.manager)
        self.assertEqual(batch.summary["duplicates"], 1)
        commit_import(batch, self.manager)
        self.assertEqual(Order.objects.count(), 1)
        conflicting = self.import_row(**{"Order value": "999"})
        batch = preview_import(self.csv_file([row, conflicting]), self.manager)
        self.assertEqual(batch.summary["rejected"], 2)

    def test_reupload_is_noop_and_keeps_task_changes(self):
        row = self.import_row()
        batch = preview_import(self.csv_file([row]), self.manager)
        commit_import(batch, self.manager)
        evaluate_orders(now=self.now)
        task = Task.objects.get()
        update_task(task, self.manager, status="In Progress", assignee=self.ravi)
        event_count = AuditEvent.objects.count()
        repeat = preview_import(self.csv_file([row]), self.manager)
        self.assertEqual(repeat.summary["unchanged"], 1)
        commit_import(repeat, self.manager)
        evaluate_orders(now=self.now)
        task.refresh_from_db()
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), event_count)
        self.assertEqual(task.status, "In Progress")
        self.assertEqual(task.assigned_employee, self.ravi)

    def test_changed_order_requires_explicit_update_confirmation(self):
        commit_import(preview_import(self.csv_file([self.import_row()]), self.manager), self.manager)
        changed = self.import_row(**{"Customer name": "Changed Customer"})
        batch = preview_import(self.csv_file([changed]), self.manager)
        summary = commit_import(batch, self.manager)
        self.assertEqual(summary["skipped_updates"], 1)
        self.assertEqual(Order.objects.get().customer_name, "Upload Customer")
        batch = preview_import(self.csv_file([changed]), self.manager)
        commit_import(batch, self.manager, allow_updates=True)
        self.assertEqual(Order.objects.get().customer_name, "Changed Customer")

    def test_stale_preview_is_rejected_without_partial_commit(self):
        commit_import(preview_import(self.csv_file([self.import_row()]), self.manager), self.manager)
        batch = preview_import(self.csv_file([
            self.import_row(**{"Order ID": "NEW"}),
            self.import_row(**{"Customer name": "New Name"}),
        ]), self.manager)
        Order.objects.filter(order_id="UPLOAD-1").update(phone="9876543211")
        with self.assertRaises(ValueError):
            commit_import(batch, self.manager, allow_updates=True)
        self.assertFalse(Order.objects.filter(order_id="NEW").exists())
        batch.refresh_from_db()
        self.assertEqual(batch.status, "preview")

    def test_commit_twice_is_idempotent_and_owned(self):
        batch = preview_import(self.csv_file([self.import_row()]), self.manager)
        with self.assertRaises(ValueError):
            commit_import(batch, self.ravi)
        first = commit_import(batch, self.manager)
        self.assertEqual(commit_import(batch, self.manager), first)
        self.assertEqual(Order.objects.count(), 1)

    def test_missing_employee_uses_manager_and_warns(self):
        batch = preview_import(self.csv_file([self.import_row(**{"Assigned employee": "unknown"})]), self.manager)
        commit_import(batch, self.manager)
        order = Order.objects.get()
        self.assertEqual(order.assigned_employee, self.manager)
        self.assertTrue(order.data_issues)

    def test_date_precision_is_retained(self):
        row = self.import_row(**{"Order date": "2026-10-20", "Order status": "dispatched", "Dispatch date": "2026-10-21"})
        batch = preview_import(self.csv_file([row]), self.manager)
        commit_import(batch, self.manager)
        order = Order.objects.get()
        self.assertIsNone(order.ordered_at)
        self.assertIsNone(order.dispatch_at)
        self.assertEqual(order.dispatch_date.isoformat(), "2026-10-21")

    def test_excel_datetime_cells_import_and_formula_rows_are_rejected(self):
        row = self.import_row()
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(HEADERS)
        values = [row[header] for header in HEADERS]
        values[1] = datetime(2026, 10, 20, 12, 0)
        sheet.append(values)
        bad_values = values.copy()
        bad_values[0] = "FORMULA"
        bad_values[2] = '=HYPERLINK("https://example.com")'
        sheet.append(bad_values)
        content = io.BytesIO()
        workbook.save(content)
        workbook.close()
        batch = preview_import(SimpleUploadedFile("orders.xlsx", content.getvalue()), self.manager)
        self.assertEqual(batch.summary["rejected"], 1)
        commit_import(batch, self.manager)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(timezone.localtime(Order.objects.get().ordered_at).hour, 12)

    def test_file_type_headers_and_bad_workbook_are_clear_errors(self):
        for name, content in [("orders.xls", b"bad"), ("orders.csv", b"Wrong,Header\n1,2"), ("orders.xlsx", b"not a workbook")]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    preview_import(SimpleUploadedFile(name, content), self.manager)


class WebTests(OrderTestCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order(status="delivery_failed", customer_name="=unsafe name")
        evaluate_orders(now=timezone.now() - timedelta(hours=1))
        self.task = Task.objects.get(rule_code="failed_delivery")

    def test_login_required_and_employee_cannot_import_or_edit_rules(self):
        self.assertEqual(self.client.get(reverse("tracker:dashboard")).status_code, 302)
        self.client.force_login(self.asha)
        self.assertEqual(self.client.get(reverse("tracker:imports")).status_code, 403)
        self.assertEqual(self.client.get(reverse("tracker:rule_settings")).status_code, 403)

    def test_employee_cannot_see_or_edit_another_employee_task(self):
        self.client.force_login(self.ravi)
        self.assertEqual(self.client.get(reverse("tracker:task_detail", args=[self.task.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("tracker:task_detail", args=[self.task.pk]), {"status": "Resolved"}).status_code, 404)

    def test_required_pages_render_with_real_records_and_bad_filter(self):
        self.client.force_login(self.manager)
        pages = ["dashboard", "orders", "tasks", "imports", "reports", "rule_settings"]
        for page in pages:
            with self.subTest(page=page):
                self.assertEqual(self.client.get(reverse(f"tracker:{page}")).status_code, 200)
        self.assertEqual(self.client.get(reverse("tracker:order_detail", args=[self.order.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("tracker:task_detail", args=[self.task.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("tracker:dashboard"), {"employee": "bad", "start_date": "invalid"}).status_code, 200)

    def test_dashboard_counts_orders_without_double_counting_overlapping_rules(self):
        self.order.order_value = Decimal("7000")
        self.order.expected_delivery_date = timezone.localdate() - timedelta(days=1)
        self.order.save()
        evaluate_orders()
        self.client.force_login(self.manager)
        response = self.client.get(reverse("tracker:dashboard"))
        self.assertEqual(response.context["total_orders"], 1)
        self.assertEqual(response.context["attention_count"], 1)
        self.assertEqual(response.context["pending_count"], 3)

    def test_csv_report_has_required_flags_and_escapes_user_formulas(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("tracker:daily_report"), {"date": timezone.localdate().isoformat()})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        row = next(row for row in rows if row["Task ID"] == str(self.task.pk))
        self.assertEqual(row["Customer name"], "'=unsafe name")
        self.assertEqual(row["Overdue"], "True")
        self.assertEqual(row["Unresolved"], "True")
        self.assertTrue(row["Employee pending action"])
        self.assertEqual(self.client.get(reverse("tracker:daily_report"), {"date": "bad"}).status_code, 400)

    def test_new_resolved_tasks_are_in_daily_report(self):
        update_task(self.task, self.manager, status="Resolved")
        self.client.force_login(self.manager)
        response = self.client.get(reverse("tracker:daily_report"), {"date": timezone.localdate(self.task.detected_at).isoformat()})
        self.assertIn(self.order.order_id, response.content.decode())

    def test_maximum_report_date_keeps_current_unresolved_tasks(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("tracker:daily_report"), {"date": "9999-12-31"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        row = next(row for row in rows if row["Task ID"] == str(self.task.pk))
        self.assertEqual(row["New on date"], "False")
        self.assertEqual(row["Unresolved"], "True")

    def test_manager_reassignment_through_form_persists(self):
        self.client.force_login(self.manager)
        deadline = self.task.deadline
        response = self.client.post(reverse("tracker:task_detail", args=[self.task.pk]), {"status": "In Progress", "assigned_employee": self.ravi.pk})
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "In Progress")
        self.assertEqual(self.task.assigned_employee, self.ravi)
        self.assertEqual(self.task.deadline, deadline)
        self.assertTrue(self.task.is_overdue)

    def test_import_preview_page_renders_errors_and_duplicate_rows(self):
        row = self.import_row()
        batch = preview_import(self.csv_file([
            row, row.copy(), self.import_row(**{"Order ID": "MISSING", "Customer name": ""}),
        ]), self.manager)
        self.client.force_login(self.manager)
        response = self.client.get(reverse("tracker:import_detail", args=[batch.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "UPLOAD-1")
        self.assertContains(response, "Customer name is required.")
        self.assertFalse(Order.objects.filter(order_id="UPLOAD-1").exists())

    def test_import_and_rule_evaluation_fail_together_without_partial_commit(self):
        batch = preview_import(self.csv_file([self.import_row()]), self.manager)
        self.client.force_login(self.manager)
        with patch("tracker.views.evaluate_orders", side_effect=RuntimeError("Unexpected evaluation failure")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("tracker:import_commit", args=[batch.pk]))
        batch.refresh_from_db()
        self.assertEqual(batch.status, "preview")
        self.assertFalse(Order.objects.filter(order_id="UPLOAD-1").exists())


class DemoCommandTests(TestCase):
    def test_sample_has_100_orders_all_rules_and_repeat_command_preserves_state(self):
        with TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            call_command("create_demo", load=True, stdout=io.StringIO())
            self.assertGreaterEqual(Order.objects.count(), 120)
            self.assertSetEqual(set(Task.objects.values_list("rule_code", flat=True)), {"unconfirmed", "dispatch_delay", "shipment_delay", "failed_delivery", "high_value"})
            self.assertEqual(ImportBatch.objects.get().summary["rejected"], 4)
            user = get_user_model().objects.get(username="manager")
            user.set_password("ChangedPassword123!")
            user.save()
            task_count = Task.objects.count()
            audit_count = AuditEvent.objects.count()
            sample_file = Path(folder) / "sample_data" / "orders_sample.csv"
            original_file = sample_file.read_bytes()
            call_command("create_demo", keep_files=True, load=True, stdout=io.StringIO())
            self.assertEqual(sample_file.read_bytes(), original_file)
            user.refresh_from_db()
            self.assertTrue(user.check_password("ChangedPassword123!"))
            self.assertEqual(Task.objects.count(), task_count)
            self.assertEqual(AuditEvent.objects.count(), audit_count)
