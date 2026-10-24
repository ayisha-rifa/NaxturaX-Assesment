"""Create fictional accounts and fresh, relative-date assessment files."""
import csv
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from openpyxl import Workbook

from tracker.models import Order


HEADERS = [
    "Order ID", "Order date", "Customer name", "Phone number", "Order value",
    "Payment type", "Order status", "Dispatch date", "Expected delivery date",
    "Assigned employee", "Confirmed at",
]
DEMO_PASSWORD = "DemoPass123!"


def make_sample_rows(now):
    """120 valid orders, three correction examples, one duplicate and four errors."""
    rows = []
    for number in range(1, 121):
        row = {
            "Order ID": f"DEMO-{number:03}",
            "Order date": (now - timedelta(days=5)).isoformat(),
            "Customer name": f"Sample Customer {number}",
            "Phone number": f"90000{number:05}",
            "Order value": "1200.00",
            "Payment type": "COD" if number % 2 else "prepaid",
            "Order status": "delivered",
            "Dispatch date": (now - timedelta(days=3)).isoformat(),
            "Expected delivery date": (now - timedelta(days=1)).isoformat(),
            "Assigned employee": ["asha", "ravi", "meera"][number % 3],
            "Confirmed at": (now - timedelta(days=4)).isoformat(),
        }
        group = (number - 1) % 8
        if group in [1, 2, 3, 7]:
            row["Dispatch date"] = ""
            row["Expected delivery date"] = (now + timedelta(days=2)).isoformat()
        if group == 1:
            row.update({"Order status": "unconfirmed", "Order date": (now - timedelta(hours=4)).isoformat(), "Confirmed at": ""})
        elif group == 2:
            row.update({"Order status": "confirmed", "Order date": (now - timedelta(hours=36)).isoformat(), "Confirmed at": (now - timedelta(hours=30)).isoformat()})
        elif group == 3:
            # These become dispatch exceptions when the threshold changes to 12 h.
            row.update({"Order status": "confirmed", "Order date": (now - timedelta(hours=24)).isoformat(), "Confirmed at": (now - timedelta(hours=18)).isoformat()})
        elif group == 4:
            row["Order status"] = "dispatched"
        elif group == 5:
            row["Order status"] = "delivery_failed"
            row["Expected delivery date"] = (now + timedelta(days=1)).isoformat()
        elif group == 6:
            row["Order status"] = "dispatched"
            row["Order value"] = "7500.00"
        elif group == 7:
            row.update({"Order status": "unconfirmed", "Order date": (now - timedelta(minutes=30)).isoformat(), "Confirmed at": ""})
        rows.append(row)

    missing_time = rows[1].copy()
    missing_time.update({"Order ID": "DATA-ORDER-TIME", "Order date": now.date().isoformat()})
    missing_confirmation = rows[2].copy()
    missing_confirmation.update({"Order ID": "DATA-CONFIRMATION", "Confirmed at": ""})
    missing_delivery = rows[4].copy()
    missing_delivery.update({"Order ID": "DATA-DELIVERY", "Expected delivery date": "", "Assigned employee": ""})
    rows.extend([missing_time, missing_confirmation, missing_delivery, rows[0].copy()])

    for order_id, field, value in [
        ("ERROR-CUSTOMER", "Customer name", ""),
        ("ERROR-DATE", "Order date", "2026-02-30"),
        ("ERROR-VALUE", "Order value", "-100"),
        ("ERROR-PAYMENT", "Payment type", "bitcoin"),
    ]:
        invalid = rows[0].copy()
        invalid.update({"Order ID": order_id, field: value})
        rows.append(invalid)
    return rows


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)


class Command(BaseCommand):
    help = "Generate fictional demo accounts and CSV/XLSX files; optionally load an empty database."

    def add_arguments(self, parser):
        parser.add_argument("--load", action="store_true", help="Import the sample only if no orders exist.")
        parser.add_argument("--keep-files", action="store_true", help="Use the supplied sample files without rewriting them.")

    def handle(self, *args, **options):
        User = get_user_model()
        accounts = [("manager", "Manager", True), ("asha", "Asha", False), ("ravi", "Ravi", False), ("meera", "Meera", False)]
        for username, name, manager in accounts:
            user, created = User.objects.get_or_create(username=username, defaults={"first_name": name, "is_staff": manager})
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()
        manager = User.objects.get(username="manager")
        folder = Path(settings.BASE_DIR) / "sample_data"
        sample_path = folder / "orders_sample.csv"
        if options["keep_files"]:
            if not sample_path.exists():
                raise CommandError("Sample CSV is missing. Run create_demo without --keep-files to generate it.")
            self.stdout.write("Using existing sample files without changing them.")
        else:
            self.generate_files(folder)
        self.stdout.write("Local demo users: manager, asha, ravi, meera. New accounts use DemoPass123!; existing passwords are unchanged.")
        if options["load"]:
            if Order.objects.exists():
                self.stdout.write("Orders already exist; skipped loading to preserve current data and task history. Upload a file to review updates.")
                return
            from django.db import transaction
            from tracker.importer import commit_import, preview_import
            from tracker.rules import evaluate_orders
            with sample_path.open("rb") as file:
                batch = preview_import(File(file, name=sample_path.name), manager)
            with transaction.atomic():
                summary = commit_import(batch, manager)
                result = evaluate_orders()
            self.stdout.write(f"Imported sample: {summary}")
            self.stdout.write(f"Rule check: {result}")

    def generate_files(self, folder):
        rows = make_sample_rows(timezone.localtime().replace(microsecond=0))
        folder.mkdir(exist_ok=True, parents=True)
        sample_path = folder / "orders_sample.csv"
        write_csv(sample_path, rows)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Orders"
        sheet.append(HEADERS)
        for row in rows:
            sheet.append([row.get(header, "") for header in HEADERS])
        workbook.save(folder / "orders_sample.xlsx")
        workbook.close()

        conflicting = rows[0].copy()
        conflicting["Order value"] = "999.00"
        errors = [rows[0].copy(), conflicting] + rows[-4:] + [rows[120].copy(), rows[122].copy()]
        write_csv(folder / "orders_errors.csv", errors)
        # A delivery update clears both shipment and high-value exceptions.
        delivered_update = rows[6].copy()
        delivered_update["Order status"] = "delivered"
        write_csv(folder / "orders_update.csv", [delivered_update])
        self.stdout.write("Generated sample_data/orders_sample.csv and .xlsx (123 unique usable orders plus deliberate errors).")
        self.stdout.write("Also generated orders_errors.csv and orders_update.csv.")
