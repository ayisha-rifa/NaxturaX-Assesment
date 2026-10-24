import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .forms import FilterForm, ReportForm, RuleSettingsForm, TaskForm, UploadForm
from .models import AuditEvent, ImportBatch, Order, Task
from .rules import RULES, evaluate_orders, get_rule_settings, update_task


PENDING = ["Open", "In Progress"]


def require_manager(user):
    if not user.is_staff:
        raise PermissionDenied("Only a manager can perform this action.")


def visible_tasks(user):
    tasks = Task.objects.select_related("order", "assigned_employee")
    return tasks if user.is_staff else tasks.filter(assigned_employee=user)


def visible_orders(user):
    orders = Order.objects.select_related("assigned_employee")
    if not user.is_staff:
        task_orders = visible_tasks(user).values("order_id")
        orders = orders.filter(Q(assigned_employee=user) | Q(pk__in=task_orders))
    return orders


def filtered_data(request):
    """Keep dashboard, worklist and order filters consistent."""
    orders = visible_orders(request.user)
    tasks = visible_tasks(request.user)
    form = FilterForm(request.GET, user=request.user)
    if not form.is_valid():
        return orders, tasks, form
    data = form.cleaned_data
    if data.get("start_date"):
        orders = orders.filter(order_date__gte=data["start_date"])
        tasks = tasks.filter(order__order_date__gte=data["start_date"])
    if data.get("end_date"):
        orders = orders.filter(order_date__lte=data["end_date"])
        tasks = tasks.filter(order__order_date__lte=data["end_date"])
    if data.get("search"):
        search = data["search"]
        orders = orders.filter(Q(order_id__icontains=search) | Q(customer_name__icontains=search))
        tasks = tasks.filter(Q(order__order_id__icontains=search) | Q(order__customer_name__icontains=search))
    if data.get("employee"):
        employee = data["employee"]
        tasks = tasks.filter(assigned_employee=employee)
        orders = orders.filter(Q(assigned_employee=employee) | Q(pk__in=tasks.values("order_id")))
    if data.get("exception"):
        tasks = tasks.filter(rule_code=data["exception"])
    if data.get("priority"):
        tasks = tasks.filter(priority=data["priority"])
    if data.get("exception") or data.get("priority"):
        orders = orders.filter(pk__in=tasks.values("order_id"))
    if data.get("needs_data"):
        orders = orders.exclude(data_issues=[], rule_issues=[])
        tasks = tasks.filter(order_id__in=orders.values("pk"))
    return orders, tasks, form


def pagination(request, rows):
    page = Paginator(rows, 30).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return page, query.urlencode()


@login_required
def dashboard(request):
    orders, tasks, form = filtered_data(request)
    pending = tasks.filter(status__in=PENDING)
    now = timezone.now()
    corrections = orders.exclude(data_issues=[], rule_issues=[])
    attention = orders.filter(Q(pk__in=pending.values("order_id")) | Q(pk__in=corrections.values("pk"))).distinct()
    category_counts = []
    for code, rule in RULES.items():
        category_counts.append({"name": rule["label"], "count": pending.filter(rule_code=code).count()})
    employee_counts = pending.values(
        "assigned_employee__username", "assigned_employee__first_name", "assigned_employee__last_name"
    ).annotate(count=Count("id")).order_by("assigned_employee__username")
    return render(request, "tracker/dashboard.html", {
        "filter_form": form,
        "total_orders": orders.count(),
        "attention_count": attention.count(),
        "pending_count": pending.count(),
        "open_count": tasks.filter(status="Open").count(),
        "overdue_count": pending.filter(deadline__lt=now).count(),
        "correction_count": corrections.count(),
        "category_counts": category_counts,
        "employee_counts": employee_counts,
        "high_priority_tasks": pending.filter(priority="High").order_by("deadline")[:15],
        "now": now,
    })


@login_required
@require_POST
def check_rules(request):
    require_manager(request.user)
    evaluate_orders()
    messages.success(request, "Rules checked. Existing task assignments and deadlines were preserved.")
    return redirect("tracker:dashboard")


@login_required
@require_http_methods(["GET", "POST"])
def imports(request):
    require_manager(request.user)
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        from .importer import preview_import
        try:
            batch = preview_import(form.cleaned_data["file"], request.user)
        except ValueError as error:
            form.add_error("file", str(error))
        else:
            return redirect("tracker:import_detail", pk=batch.pk)
    batches = ImportBatch.objects.filter(uploaded_by=request.user).order_by("-created_at")[:20]
    return render(request, "tracker/imports.html", {"form": form, "batches": batches})


@login_required
def import_detail(request, pk):
    require_manager(request.user)
    batch = get_object_or_404(ImportBatch, pk=pk, uploaded_by=request.user)
    return render(request, "tracker/import_detail.html", {"batch": batch})


@login_required
@require_POST
def import_commit(request, pk):
    require_manager(request.user)
    batch = get_object_or_404(ImportBatch, pk=pk, uploaded_by=request.user)
    from .importer import commit_import
    try:
        with transaction.atomic():
            commit_import(batch, request.user, allow_updates=request.POST.get("allow_updates") == "on")
            evaluate_orders()
    except ValueError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Import completed and follow-up rules checked.")
    return redirect("tracker:import_detail", pk=pk)


@login_required
def orders(request):
    order_rows, tasks, form = filtered_data(request)
    page, query = pagination(request, order_rows.order_by("-order_date", "order_id"))
    return render(request, "tracker/orders.html", {
        "orders": page, "page_obj": page, "page_query": query, "filter_form": form,
    })


@login_required
def order_detail(request, pk):
    order = get_object_or_404(visible_orders(request.user), pk=pk)
    tasks = visible_tasks(request.user).filter(order=order).order_by("-detected_at")
    return render(request, "tracker/order_detail.html", {"order": order, "tasks": tasks})


@login_required
def tasks(request):
    order_rows, task_rows, form = filtered_data(request)
    status = request.GET.get("status", "")
    valid_statuses = ["Open", "In Progress", "Resolved"]
    if status in valid_statuses:
        task_rows = task_rows.filter(status=status)
    elif status:
        form.add_error(None, "Choose a valid task status.")
    page, query = pagination(request, task_rows.order_by("deadline", "id"))
    return render(request, "tracker/tasks.html", {
        "tasks": page, "page_obj": page, "page_query": query, "filter_form": form,
        "selected_status": status, "statuses": valid_statuses,
    })


@login_required
@require_http_methods(["GET", "POST"])
def task_detail(request, pk):
    task = get_object_or_404(visible_tasks(request.user), pk=pk)
    form = TaskForm(
        request.POST or None, user=request.user,
        initial={"status": task.status, "assigned_employee": task.assigned_employee_id},
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_task(task, request.user, status=form.cleaned_data["status"], assignee=form.cleaned_data.get("assigned_employee"))
        except (ValueError, ValidationError) as error:
            form.add_error(None, error)
        else:
            messages.success(request, "Task updated. Its original deadline has been kept.")
            return redirect("tracker:task_detail", pk=pk)
    events = AuditEvent.objects.filter(task=task).order_by("-timestamp", "-id")
    return render(request, "tracker/task_detail.html", {"task": task, "form": form, "events": events})


@login_required
@require_http_methods(["GET", "POST"])
def rule_settings(request):
    require_manager(request.user)
    settings = get_rule_settings()
    initial = {name: getattr(settings, name) for name in ("unconfirmed_hours", "dispatch_hours", "high_value_amount")}
    form = RuleSettingsForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            for field, value in form.cleaned_data.items():
                setattr(settings, field, value)
            settings.save()
            evaluate_orders()
        messages.success(request, "Rule settings saved and orders checked again.")
        return redirect("tracker:rule_settings")
    return render(request, "tracker/rule_settings.html", {"form": form, "rules": RULES})


@login_required
def reports(request):
    form = ReportForm(request.GET or None, initial={"date": timezone.localdate()})
    return render(request, "tracker/reports.html", {"form": form})


def safe_csv_cell(value):
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


@login_required
def daily_report(request):
    form = ReportForm(request.GET)
    if not form.is_valid():
        return render(request, "tracker/reports.html", {"form": form}, status=400)
    date = form.cleaned_data["date"]
    now = timezone.now()
    rows = visible_tasks(request.user).filter(
        Q(detected_at__date=date) | Q(status__in=PENDING)
    ).order_by("assigned_employee__username", "deadline", "id")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="daily-exceptions-{date.isoformat()}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow([
        "Report date", "Generated at", "Task ID", "Order ID", "Customer name", "Phone",
        "Exception", "Reason", "Priority", "Assigned employee", "Action required", "Deadline",
        "Task status", "New on date", "Unresolved", "Overdue", "High priority", "Employee pending action",
    ])
    for task in rows:
        pending = task.status in PENDING
        writer.writerow([safe_csv_cell(value) for value in [
            date.isoformat(), timezone.localtime(now).isoformat(), task.pk, task.order.order_id,
            task.order.customer_name, task.order.phone, RULES[task.rule_code]["label"], task.reason,
            task.priority, task.assigned_employee.get_full_name() or task.assigned_employee.username,
            task.action_required, timezone.localtime(task.deadline).isoformat(), task.status,
            timezone.localdate(task.detected_at) == date, pending, pending and task.deadline < now,
            task.priority == "High", task.action_required if pending else "",
        ]])
    return response
