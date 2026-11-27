import re
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils import timezone


ROOM_TYPE_CHOICES = [
    ("1_sharing", "1 Sharing"),
    ("2_sharing", "2 Sharing"),
    ("3_sharing", "3 Sharing"),
]
ROOM_PREF_CHOICES = [("", "Any / Not Specified")] + ROOM_TYPE_CHOICES
ROOM_TYPE_CAPACITY = {"1_sharing": 1, "2_sharing": 2, "3_sharing": 3}
CAPACITY_TO_ROOM_TYPE = {1: "1_sharing", 2: "2_sharing", 3: "3_sharing"}


class Building(models.Model):
    YEAR_CHOICES = [(1, "1st Year"), (2, "2nd Year"), (3, "3rd Year"), (4, "4th Year")]
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=12, unique=True)
    description = models.CharField(max_length=255, blank=True)
    allowed_years = models.CharField(
        max_length=20,
        default="1,2,3,4",
        help_text="Comma-separated allowed academic years (e.g. '1' or '2,3,4')",
    )
    is_first_year_only = models.BooleanField("1st-Year Only (Anti-Ragging Block)", default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code", "name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def allowed_years_list(self):
        return sorted(
            {
                int(y.strip())
                for y in (self.allowed_years or "").split(",")
                if y.strip().isdigit() and int(y.strip()) in (1, 2, 3, 4)
            }
        )

    @property
    def allowed_years_display(self):
        labels = dict(self.YEAR_CHOICES)
        yrs = [1] if self.is_first_year_only else self.allowed_years_list
        return ", ".join(labels[y] for y in yrs if y in labels) or "All Years"

    def allows_year(self, year):
        if year is None:
            return True
        try:
            yr = int(year)
        except (TypeError, ValueError):
            return False
        if self.is_first_year_only:
            return yr == 1
        allowed = self.allowed_years_list
        return yr in allowed if allowed else True

    def clean(self):
        self.code = (self.code or "").strip().upper()
        self.name = (self.name or "").strip()
        if self.is_first_year_only:
            self.allowed_years = "1"
        else:
            yrs = self.allowed_years_list
            self.allowed_years = ",".join(str(y) for y in yrs) if yrs else "1,2,3,4"

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    @property
    def total_rooms(self):
        return self.rooms.count()

    @property
    def total_capacity(self):
        return sum(r.capacity for r in self.rooms.all())

    @property
    def occupied_beds(self):
        return RoomAllocation.objects.filter(room__building=self, end_date__isnull=True).count()

    @property
    def vacant_beds(self):
        return sum(r.vacancy for r in self.rooms.all())


class User(AbstractUser):
    ROLES = [("admin", "Super Admin"), ("warden", "Warden"), ("student", "Student"), ("parent", "Parent")]
    role = models.CharField(max_length=10, choices=ROLES, default="student")
    phone = models.CharField(max_length=20, blank=True)
    must_change_password = models.BooleanField(default=False)
    assigned_building = models.ForeignKey(
        Building,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wardens",
        help_text="Leave empty for Head Warden (full cross-building access). Assign a building to lock this warden exclusively to that building.",
    )

    @property
    def is_staff_role(self):
        return self.role in ("admin", "warden")

    @property
    def is_head_warden(self):
        return self.role == "warden" and self.assigned_building_id is None

    @property
    def scoped_building(self):
        return self.assigned_building if self.role == "warden" else None

    @property
    def warden_role_label(self):
        if self.role != "warden":
            return self.get_role_display()
        if self.assigned_building_id:
            return f"{self.assigned_building.name} Warden"
        return "Head Warden"

    @property
    def display_name(self):
        return self.get_full_name() or self.username


class Parent(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="parent")

    def __str__(self):
        return self.user.display_name


class Student(models.Model):
    YEAR_CHOICES = Building.YEAR_CHOICES
    ROOM_TYPE_CHOICES = ROOM_TYPE_CHOICES
    ROOM_PREF_CHOICES = ROOM_PREF_CHOICES
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="student")
    roll_no = models.CharField("Roll no.", max_length=30, unique=True)
    course = models.CharField(max_length=100, blank=True)
    year_of_study = models.PositiveSmallIntegerField("Academic year", choices=YEAR_CHOICES, default=1)
    preferred_room_type = models.CharField(
        "Preferred room type", max_length=15, choices=ROOM_PREF_CHOICES, blank=True, default=""
    )
    parent = models.ForeignKey(Parent, null=True, blank=True, on_delete=models.SET_NULL, related_name="children")
    is_active = models.BooleanField("Currently staying", default=True)

    # Suspension procedure (managed strictly by admin)
    is_suspended = models.BooleanField("Suspended from hostel", default=False)
    suspension_reason = models.TextField("Suspension reason", blank=True)
    suspension_start = models.DateField("Suspension start date", null=True, blank=True)
    suspension_end = models.DateField("Suspension end date", null=True, blank=True)
    suspended_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    suspended_at = models.DateTimeField("Suspended at", null=True, blank=True)

    class Meta:
        ordering = ["roll_no"]

    def __str__(self):
        return f"{self.user.display_name} ({self.roll_no})"

    def delete(self, *a, **k):
        self.user.delete()  # cascades to the student record

    @property
    def year_label(self):
        return self.get_year_of_study_display()

    @property
    def year_short(self):
        return {1: "1st Yr", 2: "2nd Yr", 3: "3rd Yr", 4: "4th Yr"}.get(self.year_of_study, f"Yr {self.year_of_study}")

    @property
    def preferred_room_type_label(self):
        return self.get_preferred_room_type_display() if self.preferred_room_type else "Any / Not Specified"

    @property
    def room(self):
        a = self.allocations.filter(end_date__isnull=True).select_related("room__building").first()
        return a.room if a else None

    @property
    def building(self):
        r = self.room
        return r.building if r else None

    @property
    def fee_balance(self):
        return sum(f.balance for f in self.fees.all())

    @property
    def is_currently_suspended(self):
        if not self.is_suspended:
            return False
        today = date.today()
        if self.suspension_start and today < self.suspension_start:
            return False
        if self.suspension_end and today > self.suspension_end:
            return False
        return True

    def attendance_percent(self):
        qs = self.attendance.exclude(status="L")
        total = qs.count()
        return round(100 * qs.filter(status="P").count() / total) if total else None


class Room(models.Model):
    ROOM_TYPE_CHOICES = ROOM_TYPE_CHOICES
    ROOM_TYPE_CAPACITY = ROOM_TYPE_CAPACITY
    CAPACITY_TO_ROOM_TYPE = CAPACITY_TO_ROOM_TYPE

    building = models.ForeignKey(Building, null=True, blank=True, on_delete=models.CASCADE, related_name="rooms")
    block = models.CharField(max_length=20, default="A")
    number = models.CharField(max_length=20)
    room_type = models.CharField("Room type", max_length=15, choices=ROOM_TYPE_CHOICES, default="2_sharing")
    capacity = models.PositiveSmallIntegerField(default=2)
    is_blacklisted = models.BooleanField("Blacklisted", default=False)
    blacklist_reason = models.CharField("Blacklist cause", max_length=150, blank=True)

    class Meta:
        unique_together = ("building", "block", "number")
        ordering = ["building__code", "block", "number"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._initial_room_type = self.room_type
        self._initial_capacity = self.capacity

    def _normalize_number_prefix(self):
        if self.number and self.block:
            b = str(self.block).strip()
            num = str(self.number).strip()
            if num.lower().startswith(f"{b.lower()}-"):
                self.number = num[len(b)+1:].strip()
            elif num.lower().startswith(f"{b.lower()} "):
                self.number = num[len(b)+1:].strip()
            elif num.lower().startswith(b.lower()) and len(num) > len(b) and num[len(b):][0].isdigit():
                self.number = num[len(b):].strip()

    def _sync_room_type_and_capacity(self):
        if self.pk is not None:
            if self.room_type != self._initial_room_type:
                if self.room_type in ROOM_TYPE_CAPACITY:
                    self.capacity = ROOM_TYPE_CAPACITY[self.room_type]
            elif self.capacity != self._initial_capacity:
                if self.capacity in CAPACITY_TO_ROOM_TYPE:
                    self.room_type = CAPACITY_TO_ROOM_TYPE[self.capacity]
        else:
            rt_changed = self.room_type != self._initial_room_type
            cap_changed = self.capacity != self._initial_capacity
            if rt_changed and not cap_changed:
                self.capacity = ROOM_TYPE_CAPACITY.get(self.room_type, self.capacity)
            elif cap_changed and not rt_changed:
                if self.capacity in CAPACITY_TO_ROOM_TYPE:
                    self.room_type = CAPACITY_TO_ROOM_TYPE[self.capacity]
            else:
                if self.room_type != "2_sharing" and self.capacity == 2:
                    self.capacity = ROOM_TYPE_CAPACITY.get(self.room_type, self.capacity)
                elif self.room_type == "2_sharing" and self.capacity in CAPACITY_TO_ROOM_TYPE and self.capacity != 2:
                    self.room_type = CAPACITY_TO_ROOM_TYPE[self.capacity]

    @property
    def room_type_label(self):
        if self.capacity in CAPACITY_TO_ROOM_TYPE:
            return dict(ROOM_TYPE_CHOICES)[CAPACITY_TO_ROOM_TYPE[self.capacity]]
        return f"{self.capacity} Sharing"

    def monthly_rent(self, config=None):
        cfg = config or FeeConfig.get_config()
        return cfg.get_rent_for_room(self)

    @property
    def short_code(self):
        if self.number and self.block:
            b = str(self.block).strip()
            num = str(self.number).strip()
            if num.lower().startswith(f"{b.lower()}-"):
                return f"{b}-{num[len(b)+1:].strip()}"
            if num.lower().startswith(b.lower()) and len(num) > len(b) and num[len(b):][0].isdigit():
                return f"{b}-{num[len(b):].strip()}"
            return f"{b}-{num}"
        return self.number or ""

    def __str__(self):
        code = self.short_code
        if self.building_id:
            return f"{self.building.code} · {code}"
        return code

    @property
    def occupied(self):
        return self.allocations.filter(end_date__isnull=True).count()

    @property
    def vacancy(self):
        if self.is_blacklisted:
            return 0
        return max(0, self.capacity - self.occupied)

    @property
    def floor_name(self):
        b = str(self.block or "").strip().upper()
        mapping = {
            "G": "Ground Floor",
            "F": "First Floor",
            "S": "Second Floor",
            "T": "Third Floor",
            "B": "Basement",
            "0": "Ground Floor",
            "1": "First Floor",
            "2": "Second Floor",
            "3": "Third Floor",
            "4": "Fourth Floor",
            "5": "Fifth Floor",
        }
        return mapping.get(b, f"Floor {b}" if b else "Floor")

    @property
    def floor_display(self):
        fn = self.floor_name
        b = str(self.block or "").strip().upper()
        if fn == f"Floor {b}":
            return fn
        return f"{fn} ({b})"

    @property
    def raw_vacancy(self):
        return max(0, self.capacity - self.occupied)

    def clean(self):
        self._normalize_number_prefix()
        self._sync_room_type_and_capacity()
        if self.pk and self.capacity < self.occupied:
            raise ValidationError(f"{self.occupied} students already live here; capacity cannot be lower.")

    def save(self, *args, **kwargs):
        self._normalize_number_prefix()
        self._sync_room_type_and_capacity()
        super().save(*args, **kwargs)
        self._initial_room_type = self.room_type
        self._initial_capacity = self.capacity


class RoomAllocation(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="allocations")
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="allocations")
    start_date = models.DateField(default=date.today)
    end_date = models.DateField(null=True, blank=True, help_text="Leave empty while the student lives here")

    class Meta:
        ordering = ["-start_date"]

    @property
    def is_active(self):
        return self.end_date is None

    def clean(self):
        if self.end_date and self.end_date < self.start_date:
            raise ValidationError("End date cannot be before start date.")
        if self.end_date is None and self.student_id and self.room_id:
            if self.room.is_blacklisted:
                cause = f" ({self.room.blacklist_reason})" if self.room.blacklist_reason else ""
                raise ValidationError(f"Room {self.room} is blacklisted{cause} and cannot accept allocations.")
            if self.room.building_id and not self.room.building.allows_year(self.student.year_of_study):
                raise ValidationError(
                    f"Anti-ragging policy violation: {self.student.year_label} students cannot be allocated to "
                    f"{self.room.building.name} (restricted to {self.room.building.allowed_years_display})."
                )
            others = RoomAllocation.objects.filter(end_date__isnull=True).exclude(pk=self.pk)
            if others.filter(student_id=self.student_id).exists():
                raise ValidationError("This student already has an active room. Vacate it first.")
            if others.filter(room_id=self.room_id).count() >= self.room.capacity:
                raise ValidationError(f"Room {self.room} is full.")


class Attendance(models.Model):
    STATUS = [("P", "Present"), ("A", "Absent"), ("L", "On leave")]
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="attendance")
    date = models.DateField(default=date.today)
    status = models.CharField(max_length=1, choices=STATUS, default="P")

    class Meta:
        unique_together = ("student", "date")
        ordering = ["-date"]


class LeaveRequest(models.Model):
    STATUS = [("Pending", "Pending"), ("Approved", "Approved"), ("Rejected", "Rejected")]
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="leaves")
    from_date = models.DateField()
    to_date = models.DateField()
    reason = models.CharField(max_length=300)
    status = models.CharField(max_length=10, choices=STATUS, default="Pending")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.from_date and self.to_date and self.to_date < self.from_date:
            raise ValidationError("'To' date cannot be before 'From' date.")


class Complaint(models.Model):
    STATUS = [(s, s) for s in ("Submitted", "Under Review", "In Progress", "Resolved")]
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="complaints")
    title = models.CharField(max_length=150)
    description = models.TextField()
    status = models.CharField(max_length=15, choices=STATUS, default="Submitted")
    staff_note = models.CharField("Note to student", max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class MealMenu(models.Model):
    DAYS = list(enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]))
    MEALS = [("B", "Breakfast"), ("L", "Lunch"), ("S", "Snacks"), ("D", "Dinner")]
    day = models.PositiveSmallIntegerField(choices=DAYS)
    meal = models.CharField(max_length=1, choices=MEALS)
    items = models.CharField(max_length=300)

    class Meta:
        unique_together = ("day", "meal")
        ordering = ["day", "meal"]


class FeeConfig(models.Model):
    """Hostel fee structure configuration editable by warden and admin."""
    rent_amount = models.DecimalField("Fallback Room Rent (Unallocated)", max_digits=10, decimal_places=2, default=Decimal("1500.00"))
    rent_1_sharing = models.DecimalField("1 Sharing Monthly Rent", max_digits=10, decimal_places=2, default=Decimal("3000.00"))
    rent_2_sharing = models.DecimalField("2 Sharing Monthly Rent", max_digits=10, decimal_places=2, default=Decimal("2000.00"))
    rent_3_sharing = models.DecimalField("3 Sharing Monthly Rent", max_digits=10, decimal_places=2, default=Decimal("1500.00"))
    food_rate_per_day = models.DecimalField("Food Rate per Day", max_digits=10, decimal_places=2, default=Decimal("130.00"))
    mess_cut_min_days = models.PositiveIntegerField("Min Consecutive Days for Mess Cut", default=3)
    mess_cut_rate_per_day = models.DecimalField("Mess Cut Deduction per Day", max_digits=10, decimal_places=2, default=Decimal("130.00"))
    MODE_CHOICES = [
        ("per_streak", "1 day food cut (₹130) per 3 consecutive absent days"),
        ("per_day", "Full mess cut: Deduct daily rate for all days in a qualifying consecutive streak (≥ min days)"),
    ]
    mess_cut_mode = models.CharField(max_length=20, choices=MODE_CHOICES, default="per_streak")
    count_leaves = models.BooleanField("Count approved leave days towards consecutive absence", default=True)
    late_fine_amount = models.DecimalField("Late fine amount", max_digits=10, decimal_places=2, default=Decimal("300.00"), blank=True)
    late_fine_interval_days = models.PositiveIntegerField("Fine interval in days", default=5, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_config(cls):
        cfg = cls.objects.first()
        if not cfg:
            cfg = cls.objects.create(
                rent_amount=Decimal("1500.00"),
                rent_1_sharing=Decimal("3000.00"),
                rent_2_sharing=Decimal("2000.00"),
                rent_3_sharing=Decimal("1500.00"),
                food_rate_per_day=Decimal("130.00"),
                mess_cut_min_days=3,
                mess_cut_rate_per_day=Decimal("130.00"),
                mess_cut_mode="per_streak",
                count_leaves=True,
                late_fine_amount=Decimal("300.00"),
                late_fine_interval_days=5,
            )
        return cfg

    def get_rent_for_type(self, room_type=""):
        mapping = {
            "1_sharing": self.rent_1_sharing,
            "2_sharing": self.rent_2_sharing,
            "3_sharing": self.rent_3_sharing,
        }
        val = mapping.get(room_type)
        return Decimal(str(val if val is not None else self.rent_amount))

    def get_rent_for_room(self, room=None):
        if room is None:
            return Decimal(str(self.rent_amount))
        rt = getattr(room, "room_type", "")
        if getattr(room, "capacity", None) in CAPACITY_TO_ROOM_TYPE:
            rt = CAPACITY_TO_ROOM_TYPE[room.capacity]
        return self.get_rent_for_type(rt)

    def __str__(self):
        return (
            f"Fee Structure (1-Sh: ₹{self.rent_1_sharing}, 2-Sh: ₹{self.rent_2_sharing}, "
            f"3-Sh: ₹{self.rent_3_sharing}, Food/Day: ₹{self.food_rate_per_day})"
        )


class Fee(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="fees")
    title = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    due_date = models.DateField()
    month = models.CharField("Billing month (YYYY-MM)", max_length=7, blank=True)
    rent_amount = models.DecimalField("Fixed Rent", max_digits=10, decimal_places=2, default=Decimal("0.00"), blank=True)
    food_amount = models.DecimalField("Food Charge", max_digits=10, decimal_places=2, default=Decimal("0.00"), blank=True)
    mess_cut_days = models.PositiveIntegerField("Mess Cut Days", default=0, blank=True)
    mess_cut_amount = models.DecimalField("Mess Cut Deduction", max_digits=10, decimal_places=2, default=Decimal("0.00"), blank=True)
    base_amount = models.DecimalField("Base Fee Amount", max_digits=10, decimal_places=2, default=Decimal("0.00"), blank=True)
    fine_amount = models.DecimalField("Late Fine Amount", max_digits=10, decimal_places=2, default=Decimal("0.00"), blank=True)
    fine_waived = models.BooleanField("Waive Late Fine", default=False, blank=True)
    notes = models.CharField("Notes / Breakdown", max_length=255, blank=True)

    class Meta:
        ordering = ["due_date"]

    @property
    def has_breakdown(self):
        return bool((self.rent_amount or 0) > 0 or (self.food_amount or 0) > 0 or (self.mess_cut_amount or 0) > 0 or (self.fine_amount or 0) > 0)

    @property
    def overdue_days(self):
        if not self.due_date or self.due_date >= date.today():
            return 0
        return (date.today() - self.due_date).days

    def calculate_late_fine(self, as_of=None):
        """
        Calculates the late fine: ₹300 fine charged for every 5 days overdue.
        e.g., 1-4 days: ₹0, 5-9 days: ₹300, 10-14 days: ₹600, etc.
        """
        if self.fine_waived or not self.due_date:
            return Decimal("0.00")
        if as_of is None:
            as_of = date.today()
        if as_of <= self.due_date:
            return Decimal("0.00")

        # If already paid in full on or before due date, no fine
        paid_on_time = self.payments.filter(paid_on__lte=self.due_date).aggregate(t=Sum("amount"))["t"] or Decimal("0.00")
        target_base = self.base_amount if self.base_amount > 0 else (self.amount - self.fine_amount)
        if paid_on_time >= target_base and target_base > 0:
            return Decimal("0.00")

        # If already fully settled (paid >= amount), don't grow further
        if self.paid >= self.amount and self.amount > 0:
            return self.fine_amount

        days = (as_of - self.due_date).days
        if days < 5:
            return Decimal("0.00")

        cfg = FeeConfig.get_config()
        interval = max(1, cfg.late_fine_interval_days or 5)
        rate = cfg.late_fine_amount or Decimal("300.00")
        periods = days // interval
        return Decimal(periods) * rate

    def sync_late_fine(self, save=True, as_of=None):
        """
        Synchronizes late fine onto the fee.
        If overdue for >= 5 days, an additional ₹300 fine per 5 days is added to the amount.
        """
        if self.fine_waived:
            if (self.fine_amount or 0) > 0:
                self.amount = max(Decimal("0.00"), self.amount - (self.fine_amount or Decimal("0.00")))
                self.fine_amount = Decimal("0.00")
            if save and self.pk:
                Fee.objects.filter(pk=self.pk).update(fine_waived=True, fine_amount=Decimal("0.00"), amount=self.amount)
            return Decimal("0.00")

        expected_fine = self.calculate_late_fine(as_of=as_of)
        if (self.base_amount or 0) <= 0:
            self.base_amount = max(Decimal("0.00"), self.amount - (self.fine_amount or Decimal("0.00")))

        if expected_fine != self.fine_amount:
            self.fine_amount = expected_fine
            self.amount = (self.base_amount or Decimal("0.00")) + self.fine_amount
            if save and self.pk:
                Fee.objects.filter(pk=self.pk).update(base_amount=self.base_amount, fine_amount=self.fine_amount, amount=self.amount, fine_waived=self.fine_waived)
        return self.fine_amount

    def save(self, *args, **kwargs):
        if (self.base_amount or 0) <= 0:
            if (self.rent_amount or 0) > 0 or (self.food_amount or 0) > 0:
                self.base_amount = max(Decimal("0.00"), (self.rent_amount or Decimal("0.00")) + (self.food_amount or Decimal("0.00")) - (self.mess_cut_amount or Decimal("0.00")))
            else:
                self.base_amount = max(Decimal("0.00"), self.amount - (self.fine_amount or Decimal("0.00")))
        if self.fine_waived:
            self.fine_amount = Decimal("0.00")
        if (self.base_amount or 0) > 0 and self.amount == 0:
            self.amount = self.base_amount + (self.fine_amount or Decimal("0.00"))
        super().save(*args, **kwargs)

    @property
    def paid(self):
        return self.payments.aggregate(t=Sum("amount"))["t"] or 0

    @property
    def balance(self):
        return self.amount - self.paid

    @property
    def status(self):
        if self.balance <= 0:
            return "Paid"
        if self.paid > 0:
            return "Partly paid"
        return "Overdue" if self.due_date < date.today() else "Unpaid"


class Payment(models.Model):
    """A payment the warden records after receiving money (cash, bank transfer, etc.)."""
    fee = models.ForeignKey(Fee, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    paid_on = models.DateField(default=date.today)
    method = models.CharField(max_length=30, default="Cash")
    reference = models.CharField(max_length=60, blank=True)
    recorded_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-paid_on"]

    def clean(self):
        if self.fee_id and self.amount is not None:
            other = self.fee.payments.exclude(pk=self.pk).aggregate(t=Sum("amount"))["t"] or 0
            if self.amount <= 0 or self.amount > self.fee.amount - other:
                raise ValidationError(f"Amount must be between 0 and the balance of {self.fee.amount - other}.")


class Announcement(models.Model):
    AUDIENCE = [("all", "Everyone"), ("student", "Students"), ("parent", "Parents")]
    title = models.CharField(max_length=150)
    body = models.TextField()
    audience = models.CharField(max_length=10, choices=AUDIENCE, default="all")
    building = models.ForeignKey(Building, null=True, blank=True, on_delete=models.CASCADE, related_name="announcements")
    room = models.ForeignKey("Room", null=True, blank=True, on_delete=models.CASCADE, related_name="announcements")
    student = models.ForeignKey("Student", null=True, blank=True, on_delete=models.CASCADE, related_name="announcements")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        if self.room:
            return f"[{self.room}] {self.title}"
        if self.building_id:
            return f"[{self.building.code}] {self.title}"
        return self.title


class Notification(models.Model):
    CATEGORIES = [
        ("announcement", "Announcement"),
        ("fee", "Fee"),
        ("complaint", "Complaint"),
        ("leave", "Leave"),
        ("gate", "Gate & Visitors"),
        ("general", "General"),
    ]
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    category = models.CharField(max_length=20, choices=CATEGORIES, default="general")
    title = models.CharField(max_length=200)
    message = models.TextField()
    link = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"To {self.recipient.username}: {self.title}"

    @property
    def icon(self):
        return ""


class GatePass(models.Model):
    """Resident gate pass for Day Outing or Home Leave with curfew tracking and optional parent confirmation."""

    PASS_TYPES = [("day_outing", "Day Outing"), ("home_leave", "Home Leave")]
    PARENT_ACK = [("Pending", "Pending"), ("Confirmed", "Confirmed")]
    STATUS_CHOICES = [
        ("Pending", "Pending"),
        ("Approved", "Approved"),
        ("Checked Out", "Checked Out"),
        ("Returned", "Returned"),
        ("Late", "Late"),
        ("Rejected", "Rejected"),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="gate_passes")
    pass_type = models.CharField(max_length=15, choices=PASS_TYPES, default="day_outing")
    out_at = models.DateTimeField("Out date & time")
    expected_return_at = models.DateTimeField("Expected return date & time")
    actual_return_at = models.DateTimeField("Actual return date & time", null=True, blank=True)
    reason = models.CharField(max_length=255)
    parent_ack = models.CharField(max_length=15, choices=PARENT_ACK, default="Pending")
    parent_ack_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="Pending")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.pass_code} · {self.student} ({self.get_pass_type_display()})"

    @property
    def pass_code(self):
        return f"GP-{self.pk:04d}" if self.pk else "GP-NEW"

    @property
    def is_curfew_overdue(self):
        return bool(
            self.actual_return_at is None
            and self.status in ("Checked Out", "Late")
            and self.expected_return_at
            and timezone.now() > self.expected_return_at
        )

    @property
    def effective_status(self):
        return "Late" if self.is_curfew_overdue else self.status

    @property
    def minutes_overdue(self):
        if not self.expected_return_at:
            return 0
        if self.actual_return_at:
            ref = self.actual_return_at
        elif self.status in ("Checked Out", "Late"):
            ref = timezone.now()
        else:
            return 0
        if ref <= self.expected_return_at:
            return 0
        return max(1, int((ref - self.expected_return_at).total_seconds() // 60))

    @property
    def overdue_display(self):
        mins = self.minutes_overdue
        if mins <= 0:
            return ""
        hrs, rem = divmod(mins, 60)
        if hrs > 0:
            return f"{hrs}h {rem}m overdue"
        return f"{rem}m overdue"

    def clean(self):
        if self.out_at and self.expected_return_at and self.expected_return_at <= self.out_at:
            raise ValidationError("Expected return date & time must be after out date & time.")


class VisitorLog(models.Model):
    """Hostel visitor register tracking campus/hostel check-ins and check-outs."""

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="visitors")
    visitor_name = models.CharField(max_length=120)
    relation = models.CharField(max_length=60)
    phone = models.CharField(max_length=20)
    purpose = models.CharField(max_length=255)
    id_proof_last4 = models.CharField("ID proof (last 4 chars)", max_length=20, blank=True)
    checked_in_at = models.DateTimeField(default=timezone.now)
    checked_out_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-checked_in_at"]

    def __str__(self):
        return f"{self.visitor_name} visiting {self.student}"

    @property
    def is_active_visit(self):
        return self.checked_out_at is None

    def clean(self):
        if self.checked_in_at and self.checked_out_at and self.checked_out_at < self.checked_in_at:
            raise ValidationError("Visitor check-out time cannot be earlier than check-in time.")
