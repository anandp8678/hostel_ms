from django import forms

from . import models as m
from .services import onboard_student


class BS:
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        for f in self.fields.values():
            w = f.widget
            if isinstance(w, (forms.CheckboxInput,)):
                w.attrs["class"] = "form-check-input"
            elif isinstance(w, forms.Select):
                w.attrs["class"] = "form-select"
            else:
                w.attrs["class"] = "form-control"
            if isinstance(w, forms.DateInput):
                w.input_type = "date"
                w.format = "%Y-%m-%d"


class DateMixin:
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        for f in self.fields.values():
            if isinstance(f, forms.DateField):
                f.widget = forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})


class MF(BS, DateMixin, forms.ModelForm):
    pass


class StudentAddForm(BS, forms.Form):
    name = forms.CharField(max_length=100)
    roll_no = forms.CharField(max_length=30, label="Roll no. (also the login username)")
    course = forms.CharField(max_length=100, required=False)
    phone = forms.CharField(max_length=20, required=False)
    parent_name = forms.CharField(max_length=100, required=False)
    parent_phone = forms.CharField(max_length=20, required=False, label="Parent phone (parent's login is created from this)")
    room = forms.ModelChoiceField(m.Room.objects.all(), required=False, empty_label="No room yet")

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["room"].queryset = m.Room.objects.filter(pk__in=[r.pk for r in m.Room.objects.all() if r.vacancy > 0])

    def save(self):
        d = self.cleaned_data
        try:
            return onboard_student(**d)
        except ValueError as e:
            raise forms.ValidationError(str(e))


class StudentEditForm(MF):
    class Meta:
        model = m.Student
        fields = ["course", "is_active"]


class RoomForm(MF):
    class Meta:
        model = m.Room
        fields = ["block", "number", "capacity", "is_blacklisted", "blacklist_reason"]


class AllocationForm(MF):
    class Meta:
        model = m.RoomAllocation
        fields = ["student", "room", "start_date"]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["student"].queryset = m.Student.objects.filter(is_active=True)
        self.fields["room"].queryset = m.Room.objects.filter(is_blacklisted=False)


class MenuForm(MF):
    class Meta:
        model = m.MealMenu
        fields = ["day", "meal", "items"]


class AnnouncementForm(MF):
    class Meta:
        model = m.Announcement
        fields = ["title", "body", "audience"]


class ComplaintStaffForm(MF):
    class Meta:
        model = m.Complaint
        fields = ["status", "staff_note"]


class ComplaintForm(MF):
    class Meta:
        model = m.Complaint
        fields = ["title", "description"]


class LeaveForm(MF):
    class Meta:
        model = m.LeaveRequest
        fields = ["from_date", "to_date", "reason"]


class FeeForm(MF):
    class Meta:
        model = m.Fee
        fields = ["student", "title", "amount", "due_date"]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["student"].required = False
        self.fields["student"].empty_label = "All students currently staying"
        self.fields["student"].queryset = m.Student.objects.filter(is_active=True)
        if self.instance.pk:
            self.fields["student"].disabled = True


class PaymentForm(MF):
    class Meta:
        model = m.Payment
        fields = ["amount", "paid_on", "method", "reference"]


class WardenForm(BS, forms.Form):
    name = forms.CharField(max_length=100)
    username = forms.CharField(max_length=50)
    phone = forms.CharField(max_length=20, required=False)

    def clean_username(self):
        u = self.cleaned_data["username"]
        if m.User.objects.filter(username=u).exists():
            raise forms.ValidationError("Username already taken.")
        return u


class ImportForm(BS, forms.Form):
    file = forms.FileField(label="CSV file")


class AllocationImportForm(BS, forms.Form):
    file = forms.FileField(label="CSV file", help_text="Upload a .csv file containing student roll numbers and allocated rooms.")


class RoomBulkAddForm(BS, forms.Form):
    MODE_CHOICES = [
        ("count", "Amount / Range"),
        ("floor", "Multi-Floor"),
        ("list", "Room List"),
        ("csv", "Upload CSV"),
    ]
    mode = forms.ChoiceField(choices=MODE_CHOICES, initial="count", required=False)

    # Count / Range mode fields
    block = forms.CharField(max_length=20, initial="A", required=False, label="Block")
    start_number = forms.CharField(max_length=20, initial="101", required=False, label="Starting room number")
    count = forms.IntegerField(min_value=1, initial=10, required=False, label="Number of rooms to add (amount)")
    end_number = forms.CharField(max_length=20, required=False, label="Or ending room number (optional)")
    capacity = forms.IntegerField(min_value=1, initial=2, required=False, label="Capacity per room")
    prefix = forms.CharField(max_length=10, required=False, label="Prefix (optional, e.g. F-)")

    # Floor mode fields
    start_floor = forms.IntegerField(min_value=0, initial=1, required=False, label="From floor")
    end_floor = forms.IntegerField(min_value=0, initial=3, required=False, label="To floor")
    rooms_per_floor = forms.IntegerField(min_value=1, initial=10, required=False, label="Rooms per floor")

    # List mode fields
    room_list = forms.CharField(widget=forms.Textarea(attrs={"rows": 4, "placeholder": "e.g. 101, 102, 103, 104\nor one per line"}),
                                required=False, label="Room numbers")

    # CSV mode fields
    file = forms.FileField(required=False, label="CSV file")


class WardenEditForm(MF):
    class Meta:
        model = m.User
        fields = ["first_name", "last_name", "phone"]
