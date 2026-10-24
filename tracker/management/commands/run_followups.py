import time
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, close_old_connections
from tracker.rules import evaluate_orders


class Command(BaseCommand):
    help = "Evaluate order rules every minute. Keep this running alongside the web server."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Evaluate once and exit.")
        parser.add_argument("--interval", type=int, default=60)

    def handle(self, *args, **options):
        if options["interval"] < 1:
            raise CommandError("Interval must be at least one second.")
        try:
            while True:
                close_old_connections()
                try:
                    result = evaluate_orders()
                    self.stdout.write(f"Rule check: {result}")
                except OperationalError as error:
                    if options["once"] or "locked" not in str(error).lower():
                        raise
                    self.stderr.write("Database is busy; retrying on the next interval.")
                if options["once"]:
                    break
                time.sleep(options["interval"])
        except KeyboardInterrupt:
            self.stdout.write("Rule checker stopped.")
