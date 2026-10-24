from django import forms
from django.contrib.auth import get_user_model


RULE_CHOICES = [
    ("", "All exception types"),
    ("unconfirmed", "Unconfirmed order"),
    ("dispatch_delay", "Dispatch delay"),
    ("shipment_delay", "Shipment delay"),
    ("failed_delivery", "Failed delivery"),
    ("high_value", "High-value overdue"),
]
PRIORITY_CHOICES = [("", "All priorities"), ("High", "High"), ("Medium", "Medium"), ("Low", "Low")]
STATUS_CHOICES = [("Open", "Open"), ("In Progress", "In Progress"), ("Resolved", "Resolved")]


class UploadForm(forms.Form):
    file = forms.FileField(label="Order file", help_text="CSV or .xlsx, up to 5 MB and 10,000 rows.")


class FilterForm(forms.Form):
    start_date = forms.DateField(required=False, label="Order date from", widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(required=False, label="Order date to", widget=forms.DateInput(attrs={"type": "date"}))
    exception = forms.ChoiceField(required=False, choices=RULE_CHOICES)
    employee = forms.ModelChoiceField(queryset=get_user_model().objects.none(), required=False, empty_label="All employees")
    priority = forms.ChoiceField(required=False, choices=PRIORITY_CHOICES)
    search = forms.CharField(required=False, max_length=100, label="Order ID or customer")
    needs_data = forms.BooleanField(required=False, label="Only orders needing data correction")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        employees = get_user_model().objects.filter(is_active=True).order_by("username")
        self.fields["employee"].queryset = employees if user.is_staff else employees.filter(pk=user.pk)

    def clean(self):
        data = super().clean()
        start = data.get("start_date")
        end = data.get("end_date")
        if start and end and start > end:
            raise forms.ValidationError("The start date must be before the end date.")
        return data


class TaskForm(forms.Form):
    status = forms.ChoiceField(choices=STATUS_CHOICES)
    assigned_employee = forms.ModelChoiceField(queryset=get_user_model().objects.none())

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        if user.is_staff:
            self.fields["assigned_employee"].queryset = get_user_model().objects.filter(is_active=True).order_by("username")
        else:
            del self.fields["assigned_employee"]


class RuleSettingsForm(forms.Form):
    unconfirmed_hours = forms.IntegerField(min_value=1, max_value=720, label="Unconfirmed order threshold (hours)")
    dispatch_hours = forms.IntegerField(min_value=1, max_value=720, label="Dispatch delay threshold (hours)")
    high_value_amount = forms.DecimalField(min_value=0, max_value=999999999, max_digits=12, decimal_places=2, label="High-value threshold (₹)")


class ReportForm(forms.Form):
    date = forms.DateField(label="New exceptions on", widget=forms.DateInput(attrs={"type": "date"}))
