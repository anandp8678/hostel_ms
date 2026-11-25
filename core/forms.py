import re

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
            elif isinstance(w, forms.CheckboxSelectMultiple):
                w.attrs["class"] = "list-unstyled mb-0"
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


class BuildingForm(MF):
    allowed_years_choices = forms.MultipleChoiceField(
        choices=m.Building.YEAR_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Allowed Academic Years",
        help_text="Select which student years may reside here (ignored and locked to 1st Year if '1st-Year Only' is checked).",
    )

    class Meta:
        model = m.Building
        fields = ["name", "code", "description", "is_first_year_only", "is_active"]
        labels = {
            "name": "Building Name",
            "code": "Short Building Code (e.g. B1, B2)",
            "is_first_year_only": "1st-Year Only Block (Anti-Ragging Isolation)",
        }

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        if self.instance and self.instance.pk:
            self.fields["allowed_years_choices"].initial = [str(y) for y in self.instance.allowed_years_list]
        else:
            self.fields["allowed_years_choices"].initial = ["1", "2", "3", "4"]

    def clean(self):
        cd = super().clean()
        if cd.get("is_first_year_only"):
            cd["allowed_years"] = "1"
        else:
            yrs = cd.get("allowed_years_choices") or ["2", "3", "4"]
            cd["allowed_years"] = ",".join(sorted({str(y) for y in yrs}))
        return cd

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.allowed_years = self.cleaned_data["allowed_years"]
        if commit:
            obj.save()
        return obj


class StudentAddForm(BS, forms.Form):
    name = forms.CharField(max_length=100)
    roll_no = forms.CharField(max_length=30, label="Roll no. (also the login username)")
    course = forms.CharField(max_length=100, required=False)
    year_of_study = forms.TypedChoiceField(
        choices=m.Student.YEAR_CHOICES, coerce=int, initial=1, label="Academic year"
    )
    phone = forms.CharField(max_length=20, required=False)
    parent_name = forms.CharField(max_length=100, required=False)
    parent_phone = forms.CharField(max_length=20, required=False, label="Parent phone (parent's login is created from this)")
    room = forms.ModelChoiceField(m.Room.objects.all(), required=False, empty_label="No room yet")

    def __init__(self, *a, **k):
        scoped_building = k.pop("building", None)
        super().__init__(*a, **k)
        qs = m.Room.objects.select_related("building").filter(
            pk__in=[r.pk for r in m.Room.objects.all() if r.vacancy > 0]
        )
        if scoped_building is not None:
            qs = qs.filter(building=scoped_building)
            if scoped_building.is_first_year_only:
                self.fields["year_of_study"].initial = 1
        self.fields["room"].queryset = qs

    def clean(self):
        cd = super().clean()
        room = cd.get("room")
        yr = cd.get("year_of_study") or 1
        if room and room.building_id and not room.building.allows_year(yr):
            year_lbl = dict(m.Student.YEAR_CHOICES).get(int(yr), f"Year {yr}")
            self.add_error(
                "room",
                f"Anti-ragging policy violation: {year_lbl} students cannot be placed in {room.building.name} "
                f"(restricted to {room.building.allowed_years_display}).",
            )
        return cd

    def save(self):
        d = self.cleaned_data
        try:
            return onboard_student(**d)
        except ValueError as e:
            raise forms.ValidationError(str(e))


class StudentEditForm(MF):
    class Meta:
        model = m.Student
        fields = ["course", "year_of_study", "is_active"]

    def clean(self):
        cd = super().clean()
        yr = cd.get("year_of_study")
        if self.instance and self.instance.pk and yr is not None:
            cur_room = self.instance.room
            if cur_room and cur_room.building_id and not cur_room.building.allows_year(yr):
                year_lbl = dict(m.Student.YEAR_CHOICES).get(int(yr), f"Year {yr}")
                raise forms.ValidationError(
                    f"This student currently occupies {cur_room} ({cur_room.building.name}). "
                    f"Vacate their room before changing academic year to {year_lbl}."
                )
        return cd


class RoomForm(MF):
    class Meta:
        model = m.Room
        fields = ["building", "block", "number", "capacity", "is_blacklisted", "blacklist_reason"]
        labels = {
            "building": "Hostel Building",
            "block": "Floor / Prefix (e.g. G for Ground, F for 1st)",
            "number": "Room number (e.g. 101, displays as B1 · G-101)",
        }
        help_texts = {
            "building": "Select whether this room is in Building 1 (1st Year) or Building 2 (Senior Block).",
            "block": "Floor prefix indicates the floor: G = Ground Floor, F = First Floor, S = Second Floor, etc.",
            "number": "Enter room number (e.g. 101). If entered as G-101, the prefix is formatted automatically.",
        }

    def __init__(self, *a, **k):
        scoped_building = k.pop("building", None)
        super().__init__(*a, **k)
        qs = m.Building.objects.filter(is_active=True)
        if scoped_building is not None:
            qs = qs.filter(pk=scoped_building.pk)
            self.fields["building"].initial = scoped_building
        self.fields["building"].queryset = qs
        self.fields["building"].required = qs.exists()

    def clean(self):
        cd = super().clean()
        num = (cd.get("number") or "").strip()
        m_prefix = re.match(r"^([a-zA-Z]+)[ -](\d.*)$", num)
        if m_prefix:
            cd["block"] = m_prefix.group(1).upper()
            cd["number"] = m_prefix.group(2).strip()
            self.instance.block = cd["block"]
            self.instance.number = cd["number"]
        return cd


class AllocationForm(MF):
    class Meta:
        model = m.RoomAllocation
        fields = ["student", "room", "start_date"]

    def __init__(self, *a, **k):
        scoped_building = k.pop("building", None)
        super().__init__(*a, **k)
        students_qs = m.Student.objects.filter(is_active=True).select_related("user")
        rooms_qs = m.Room.objects.filter(is_blacklisted=False).select_related("building")
        if scoped_building is not None:
            rooms_qs = rooms_qs.filter(building=scoped_building)
            allowed_yrs = scoped_building.allowed_years_list
            if scoped_building.is_first_year_only:
                students_qs = students_qs.filter(year_of_study=1)
            elif allowed_yrs:
                students_qs = students_qs.filter(year_of_study__in=allowed_yrs)
        self.fields["student"].queryset = students_qs
        self.fields["room"].queryset = rooms_qs


class MenuForm(MF):
    class Meta:
        model = m.MealMenu
        fields = ["day", "meal", "items"]


class AnnouncementForm(MF):
    class Meta:
        model = m.Announcement
        fields = ["title", "body", "audience", "building"]

    def __init__(self, *a, **k):
        scoped_building = k.pop("building", None)
        super().__init__(*a, **k)
        if "building" in self.fields:
            qs = m.Building.objects.filter(is_active=True)
            if scoped_building is not None:
                qs = qs.filter(pk=scoped_building.pk)
                self.fields["building"].queryset = qs
                self.fields["building"].initial = scoped_building
                self.fields["building"].required = True
            else:
                self.fields["building"].queryset = qs
                self.fields["building"].required = False
                self.fields["building"].empty_label = "All Buildings"


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


class FeeConfigForm(MF):
    class Meta:
        model = m.FeeConfig
        fields = [
            "rent_amount",
            "food_rate_per_day",
            "mess_cut_min_days",
            "mess_cut_rate_per_day",
            "mess_cut_mode",
            "count_leaves",
            "late_fine_amount",
            "late_fine_interval_days",
        ]
        labels = {
            "rent_amount": "Fixed Monthly Rent (₹)",
            "food_rate_per_day": "Food Rate per Day (₹)",
            "mess_cut_min_days": "Consecutive Absent Days for Mess Cut",
            "mess_cut_rate_per_day": "Mess Cut Deduction Rate per Day (₹)",
            "mess_cut_mode": "Mess Cut Calculation Rule",
            "count_leaves": "Include Approved Leaves as Absence for Mess Cut",
            "late_fine_amount": "Late Fine Amount (₹)",
            "late_fine_interval_days": "Late Fine Overdue Interval (Days)",
        }
        help_texts = {
            "rent_amount": "Default fixed hostel room rent per student per month (e.g. ₹1500).",
            "food_rate_per_day": "Daily food / mess charge (e.g. ₹130).",
            "mess_cut_min_days": "Minimum consecutive absent days required to qualify for mess cut (default: 3 days).",
            "mess_cut_rate_per_day": "Amount to reduce per eligible day (default: ₹130 = daily food rate).",
            "late_fine_amount": "Fine charged for every overdue interval (e.g. ₹300).",
            "late_fine_interval_days": "Overdue days threshold interval (e.g. every 5 days: after 5 days past due -> +₹300, after 10 days -> +₹600).",
        }

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        for f_name in ["late_fine_amount", "late_fine_interval_days", "count_leaves"]:
            if f_name in self.fields:
                self.fields[f_name].required = False


class FeeForm(MF):
    class Meta:
        model = m.Fee
        fields = ["student", "title", "rent_amount", "food_amount", "mess_cut_days", "mess_cut_amount", "fine_amount", "fine_waived", "amount", "due_date", "notes"]
        labels = {
            "rent_amount": "Room Rent (₹)",
            "food_amount": "Food Charges (₹)",
            "mess_cut_days": "Mess Cut Days",
            "mess_cut_amount": "Mess Cut Deduction (₹)",
            "fine_amount": "Late Fine (₹)",
            "fine_waived": "Waive Late Fine",
            "amount": "Total Net Payable (₹)",
            "notes": "Breakdown Notes / Remarks",
        }

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["student"].required = False
        self.fields["student"].empty_label = "All students currently staying"
        self.fields["student"].queryset = m.Student.objects.filter(is_active=True)
        if self.instance.pk:
            self.fields["student"].disabled = True
        for f_name in ["rent_amount", "food_amount", "mess_cut_days", "mess_cut_amount", "fine_amount", "fine_waived", "notes"]:
            if f_name in self.fields:
                self.fields[f_name].required = False

    def clean(self):
        cd = super().clean()
        rent = cd.get("rent_amount") or 0
        food = cd.get("food_amount") or 0
        mess_cut = cd.get("mess_cut_amount") or 0
        fine = cd.get("fine_amount") or 0
        waived = cd.get("fine_waived")
        if waived:
            fine = 0
            cd["fine_amount"] = 0
            self.instance.fine_amount = 0
        amt = cd.get("amount")
        if (rent or food) and (amt is None or amt == 0):
            base = max(0, rent + food - mess_cut)
            cd["base_amount"] = base
            cd["amount"] = base + fine
            self.instance.base_amount = base
            self.instance.amount = cd["amount"]
        elif amt is not None:
            if cd.get("base_amount") is None or cd.get("base_amount") == 0:
                cd["base_amount"] = max(0, amt - fine)
                self.instance.base_amount = cd["base_amount"]
        return cd


class PaymentForm(MF):
    class Meta:
        model = m.Payment
        fields = ["amount", "paid_on", "method", "reference"]


class WardenForm(BS, forms.Form):
    name = forms.CharField(max_length=100)
    username = forms.CharField(max_length=50)
    phone = forms.CharField(max_length=20, required=False)
    assigned_building = forms.ModelChoiceField(
        m.Building.objects.filter(is_active=True),
        required=False,
        empty_label="Head Warden (Full Access - All Buildings)",
        label="Warden Tier / Assigned Building",
        help_text="Select a building (e.g. First Year Block) to strictly lock this warden to it, or leave empty for Head Warden.",
    )

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
    building = forms.ModelChoiceField(
        m.Building.objects.filter(is_active=True),
        required=False,
        empty_label="Select building...",
        label="Hostel Building",
    )

    # Count / Range mode fields
    block = forms.CharField(max_length=20, initial="G", required=False, label="Floor / Prefix (G for Ground, F for 1st, S for 2nd)")
    count_block = forms.CharField(max_length=20, required=False)
    start_number = forms.CharField(max_length=20, initial="101", required=False, label="Starting room number (e.g. 101 -> G-101)")
    count = forms.IntegerField(min_value=1, initial=10, required=False, label="Number of rooms to add (amount)")
    end_number = forms.CharField(max_length=20, required=False, label="Or ending room number (optional)")
    capacity = forms.IntegerField(min_value=1, initial=2, required=False, label="Capacity per room")
    count_capacity = forms.IntegerField(min_value=1, required=False)
    prefix = forms.CharField(max_length=10, required=False, widget=forms.HiddenInput)

    # Floor mode fields
    floor_block = forms.CharField(max_length=20, initial="G", required=False, label="Floor Prefix")
    start_floor = forms.IntegerField(min_value=0, initial=1, required=False, label="From floor")
    end_floor = forms.IntegerField(min_value=0, initial=3, required=False, label="To floor")
    rooms_per_floor = forms.IntegerField(min_value=1, initial=10, required=False, label="Rooms per floor")
    floor_capacity = forms.IntegerField(min_value=1, initial=2, required=False, label="Capacity per room")

    # List mode fields
    list_block = forms.CharField(max_length=20, initial="G", required=False, label="Default Floor Prefix")
    list_capacity = forms.IntegerField(min_value=1, initial=2, required=False, label="Capacity per room")
    room_list = forms.CharField(widget=forms.Textarea(attrs={"rows": 4, "placeholder": "e.g. 101, 102, 103, 104\nor one per line"}),
                                required=False, label="Room numbers")

    # CSV mode fields
    file = forms.FileField(required=False, label="CSV file")


class WardenEditForm(MF):
    class Meta:
        model = m.User
        fields = ["first_name", "last_name", "phone", "assigned_building"]
        labels = {"assigned_building": "Warden Tier / Assigned Building"}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        if "assigned_building" in self.fields:
            self.fields["assigned_building"].queryset = m.Building.objects.filter(is_active=True)
            self.fields["assigned_building"].required = False
            self.fields["assigned_building"].empty_label = "Head Warden (Full Access - All Buildings)"
