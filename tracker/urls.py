from django.urls import path

from . import views


app_name = "tracker"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("check-rules/", views.check_rules, name="check_rules"),
    path("imports/", views.imports, name="imports"),
    path("imports/<int:pk>/", views.import_detail, name="import_detail"),
    path("imports/<int:pk>/commit/", views.import_commit, name="import_commit"),
    path("orders/", views.orders, name="orders"),
    path("orders/<int:pk>/", views.order_detail, name="order_detail"),
    path("tasks/", views.tasks, name="tasks"),
    path("tasks/<int:pk>/", views.task_detail, name="task_detail"),
    path("reports/", views.reports, name="reports"),
    path("reports/daily.csv", views.daily_report, name="daily_report"),
    path("settings/rules/", views.rule_settings, name="rule_settings"),
]
