# D2C Order Follow-up Tracker

A small Django application for finding orders that need attention and keeping their follow-up tasks in one place. It uses normal HTML pages, a little CSS, and SQLite. There is no separate frontend build or external business-service integration.

Managers can upload order spreadsheets, see validation errors, assign tasks, change task status, and download a daily report. Employees can view their own work and update its status.

## Run locally

Use Python 3.12. The application listens on port 8000. The development server is intended for a local demonstration, not a public production deployment.

### Windows (PowerShell or Command Prompt)

1. Install Python 3.12 from [python.org](https://www.python.org/downloads/windows/). Enable **Add python.exe to PATH** during installation, then open a new terminal.
2. Download and extract this repository's ZIP, or install Git and clone it:

   ```powershell
   git clone https://github.com/ayisha-rifa/NaturaX-Assignment.git
   cd NaturaX-Assignment
   ```

3. Open PowerShell or Command Prompt in the folder containing `manage.py`. Check Python and set up the application:

   ```powershell
   py -3.12 --version
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe manage.py migrate
   .\.venv\Scripts\python.exe manage.py create_demo --keep-files --load
   .\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
   ```

   If `py` is unavailable but `python --version` shows Python 3.12, use `python -m venv .venv` instead. The remaining commands stay the same.

4. Keep that terminal running. Open a **second terminal in the same project folder** and start the automatic rule checker:

   ```powershell
   .\.venv\Scripts\python.exe manage.py run_followups
   ```

5. Enter `http://127.0.0.1:8000/` in your browser. Sign in with username `manager` and password `DemoPass123!`.

These commands use the virtual environment's Python directly, so activation is unnecessary. If you prefer activation, PowerShell uses `.\.venv\Scripts\Activate.ps1` and Command Prompt uses `.venv\Scripts\activate.bat`. If PowerShell blocks the activation script, use the direct commands above without changing its execution policy.

For later runs, repeat only the web server and rule checker commands in two terminals. To check the rules once instead of keeping a worker running:

```powershell
.\.venv\Scripts\python.exe manage.py run_followups --once
```

### macOS or Linux

Run these commands from the project folder:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py create_demo --keep-files --load
python manage.py runserver 127.0.0.1:8000
```

In a **second terminal**, from the same project folder, start the rule checker:

```bash
source .venv/bin/activate
python manage.py run_followups
```

To check once without a long-running process:

```bash
python manage.py run_followups --once
```

### Keeping the application running

On either operating system, keep both processes running. Rules run immediately when an import is committed or settings are changed, and the worker checks orders every 60 seconds. The manager can also use **Check rules now** on the dashboard. Merely starting the web server does not start the periodic worker.

Stop a process with `Ctrl+C`. Restarting either process does not erase orders, task assignments, or audit history. Data is stored in the ignored `db.sqlite3` file; do not delete that file to restart the application.

## Demo accounts

| Username | Role |
| --- | --- |
| `manager` | Manager: all orders, imports, task reassignment, reports and rule settings |
| `asha` | Employee: own orders/tasks and task status changes |
| `ravi` | Employee: own orders/tasks and task status changes |
| `meera` | Employee: own orders/tasks and task status changes |

The sample accounts use the password `DemoPass123!`. These are public demonstration credentials. Do not expose this setup to the Internet or reuse the password for real accounts. The setup command creates missing accounts; it does not reset existing users' passwords.

## Try the application

1. Sign in as `manager` and inspect the dashboard.
2. Open **Import orders** and upload `sample_data/orders_sample.csv` or its `.xlsx` version.
3. Inspect the preview. Rejected rows show their problems, while valid orders can still be imported.
4. Commit the import. Existing identical orders are unchanged. Updates to existing orders require the update checkbox.
5. Open **Tasks**, inspect a reason/action/deadline, change status, and reassign a task.
6. Open its audit history to see the responsible user and timestamp.
7. Filter the dashboard by order date, exception, employee, or priority.
8. Download a CSV from **Reports**.
9. Open **Rule settings** and change dispatch delay from 24 hours to 12 hours.

`create_demo --keep-files --load` uses the existing sample files without rewriting them and imports sample orders only when the order table is empty. It does not silently replace an existing working dataset. The `--keep-files` option reports a clear error if a required sample file is missing.

Running `python manage.py create_demo` without `--keep-files` regenerates the sample files using the current time. Without `--load`, it does not import them; existing work remains in the database. For a fresh demonstration database, `python manage.py create_demo --load` generates current samples and loads them only if the order table is empty. Review changed sample files before committing an update through the interface.

The main sample includes 123 usable unique order IDs, all five exception types, normal orders, incomplete rule data, one identical duplicate and four rejected rows. `sample_data/orders_errors.csv` is a smaller error-handling exercise. `sample_data/orders_update.csv` changes `DEMO-007` to delivered so you can preview an update and observe its related tasks resolve. Dates are generated relative to the command's execution time, so regenerate the files before a later demonstration if appropriate.

## Spreadsheet format

Include these column headers, even when a conditional value is blank:

```text
Order ID,Order date,Customer name,Phone number,Order value,Payment type,Order status,Dispatch date,Expected delivery date,Assigned employee
```

The additional optional column **Confirmed at** supplies the confirmation timestamp for dispatch-delay calculations. It was added because the assessment's ten columns do not include that information.

- CSV files must use UTF-8; an Excel-style UTF-8 BOM is accepted. Excel files must be `.xlsx`, not `.xls`.
- Maximum size: 5 MB; maximum non-empty order rows: 10,000.
- Required row values: order ID, order date, customer name, phone, order value, payment type, and status.
- Use `prepaid` or `COD` for payment type.
- Supported statuses: `unconfirmed`, `confirmed`, `dispatched`, `delivery_failed`, `delivered`, `cancelled`. Common explicit aliases such as `shipped` and `in transit` map to `dispatched`.
- Use employee usernames such as `asha`; matching employee full names are also accepted. Missing, unknown, or ambiguous employees fall back to the uploading manager and produce a correction warning.
- Dates accept `YYYY-MM-DD` or `DD/MM/YYYY`. Timestamps accept ISO 8601, such as `2026-10-23T14:30:00+05:30`, or `DD/MM/YYYY HH:MM`. Timestamps without an offset are interpreted in Asia/Kolkata.
- A date-only order value remains a date: the importer does not invent its order time.
- Dispatch and expected-delivery fields are conditional. Missing evidence required by a rule is shown as needing data correction rather than guessed.
- Phone numbers and order IDs are stored as text. Format phone cells as text in Excel to preserve leading zeroes and `+` prefixes.
- Negative/invalid amounts, unknown statuses, invalid dates, contradictory dates, and formula cells in workbooks are rejected.

Identical duplicate rows within a file are identified and collapsed. Conflicting rows with the same order ID are rejected for correction. Re-uploading an existing identical order is a no-op. Changed existing data is shown for explicit update confirmation; it never resets manually edited tasks.

## Rule and task policies

| Exception | Trigger | Priority | Follow-up deadline after detection |
| --- | --- | --- | --- |
| Unconfirmed | Unconfirmed for more than 2 hours from its precise order timestamp | Medium | 2 hours |
| Dispatch delay | Confirmed, undispatched, and more than 24 hours from `Confirmed at` | High | 2 hours |
| Shipment delay | Dispatched or failed-delivery shipment past expected delivery | Medium | 4 hours |
| Failed delivery | Explicit failed-delivery status | High | Immediate |
| High-value overdue | Undelivered order above ₹5,000 and past expected delivery | High | 1 hour |

The assessment specifies the exception triggers. Priority mappings, follow-up deadlines, confirmation-based dispatch timing, and lifecycle policies are agreed prototype defaults rather than quoted assessment rules.

The manager can edit order-age/value thresholds in **Rule settings**. Follow-up priorities, recommended actions, and deadline lengths are collected in the `RULES` dictionary in `tracker/rules.py`. Order thresholds and follow-up task deadlines are different concepts.

Time is stored in UTC and displayed in Asia/Kolkata. A date-only delivery promise is valid for that entire local day. Delivered and cancelled orders are excluded from new delivery-delay/high-value exceptions. Missing evidence returns `NEEDS_DATA`; it does not create an exception or falsely resolve an existing task.

One order can have more than one exception. Task identity is based on the order, rule, and occurrence, with a database constraint allowing only one current incident per order/rule. Repeated imports/checks preserve task progress, reassignment, detection time, and deadline. Manually resolving the task does not change the order's fulfillment status. That same continuing incident stays resolved; an issue that clears and later recurs creates another occurrence. Sufficient source data that clears a rule automatically resolves its pending task with a System audit entry.

Reassigning an overdue task **keeps its original deadline**. It remains overdue under the new employee. Every assignment, reassignment, and task status change records the order, actor, action, and timestamp.

## Dashboard and report meanings

- **Total orders** counts distinct accepted order IDs, not raw spreadsheet rows.
- **Orders requiring attention** counts distinct orders with pending tasks or retained correction issues.
- **Pending** means Open or In Progress. **Overdue** means pending and past its task deadline.
- Rejected import rows are visible in import results; they are not included as accepted orders.
- Dashboard date filters use **Order date**. Other filters use task exception, assignee, and priority.
- The daily report includes tasks detected on the selected local date **or** tasks still unresolved at export time. Columns identify new, unresolved, overdue, and high-priority cases and list each employee's pending action.
- A task appears once in the CSV even when several report flags apply. The export timestamp makes its current-state meaning explicit. Selecting an earlier date does not reconstruct that day's historical closing backlog.

## Tests

On Windows, from the project folder:

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test tracker
```

On macOS or Linux, with the virtual environment activated:

```bash
python manage.py check
python manage.py test tracker
```

## Where to read the code

```text
config/                         Django settings and application URLs
tracker/models.py               Orders, imports, tasks, settings and audit tables
tracker/importer.py             File parsing, validation, preview and commit
tracker/rules.py                Five rules and task lifecycle
tracker/forms.py                Form validation
tracker/views.py                Page handlers, filters and CSV report
tracker/templates/              HTML pages
tracker/static/                 Local CSS
tracker/management/commands/    Demo setup and periodic rule checker
tracker/tests.py                Automated checks
sample_data/                    CSV/XLSX examples
```

The project deliberately uses one Django app and direct functions rather than microservices or a separate frontend.



## Limits and future work

This is a local, single-company assessment prototype. It has no courier, WhatsApp, payment, or automatic messaging integration. Order data changes only when an updated file is imported. The periodic checker requires its own running process. SQLite and whole-dataset checks suit this sample, not a large concurrent operation. There is no historical end-of-day report reconstruction, self-service password reset, or production hosting configuration.

Task deadlines currently count elapsed hours, including nights and weekends. Accuracy depends on the uploaded status and timestamps. Production work would include deployment hardening, stronger account management, backups, PostgreSQL, and monitored background jobs. Environment settings support `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, and `DATABASE_PATH`; setting these alone does not make the development server production-ready.
