import re
from datetime import date

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum


class User(AbstractUser):
    ROLES = [("admin", "Super Admin"), ("warden", "Warden"), ("student", "Student"), ("parent", "Parent")]
    role = models.CharField(max_length=10, choices=ROLES, default="student")
    phone = models.CharField(max_length=20, blank=True)
    must_change_password = models.BooleanField(default=False)

    @property
    def is_staff_role(self):
        return self.role in ("admin", "warden")

    @property
    def display_name(self):
        return self.get_full_name() or self.username


class Parent(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="parent")

    def __str__(self):
        return self.user.display_name


class Student(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="student")
    roll_no = models.CharField("Roll no.", max_length=30, unique=True)
    course = models.CharField(max_length=100, blank=True)
    parent = models.ForeignKey(Parent, null=True, blank=True, on_delete=models.SET_NULL, related_name="children")
    is_active = models.BooleanField("Currently staying", default=True)

    class Meta:
        ordering = ["roll_no"]

    def __str__(self):
        return f"{self.user.display_name} ({self.roll_no})"

    def delete(self, *a, **k):
        self.user.delete()  # cascades to the student record

    @property
    def room(self):
        a = self.allocations.filter(end_date__isnull=True).select_related("room").first()
        return a.room if a else None

    @property
    def fee_balance(self):
        return sum(f.balance for f in self.fees.all())

    def attendance_percent(self):
        qs = self.attendance.exclude(status="L")
        total = qs.count()
        return round(100 * qs.filter(status="P").count() / total) if total else None


class Room(models.Model):
    block = models.CharField(max_length=20, default="A")
    number = models.CharField(max_length=20)
    capacity = models.PositiveSmallIntegerField(default=2)
    is_blacklisted = models.BooleanField("Blacklisted", default=False)
    blacklist_reason = models.CharField("Blacklist cause", max_length=150, blank=True)

    class Meta:
        unique_together = ("block", "number")
        ordering = ["block", "number"]

    def __str__(self):
        if self.number and self.block:
            b = str(self.block).strip()
            num = str(self.number).strip()
            if num.lower().startswith(f"{b.lower()}-"):
                return f"{b}-{num[len(b)+1:].strip()}"
            if num.lower().startswith(b.lower()) and len(num) > len(b) and num[len(b):][0].isdigit():
                return f"{b}-{num[len(b):].strip()}"
            return f"{b}-{num}"
        return self.number or ""

    @property
    def occupied(self):
        return self.allocations.filter(end_date__isnull=True).count()

    @property
    def vacancy(self):
        if self.is_blacklisted:
            return 0
        return max(0, self.capacity - self.occupied)

    @property
    def raw_vacancy(self):
        return max(0, self.capacity - self.occupied)

    def clean(self):
        if self.number and self.block:
            b = str(self.block).strip()
            num = str(self.number).strip()
            if num.lower().startswith(f"{b.lower()}-"):
                self.number = num[len(b)+1:].strip()
            elif num.lower().startswith(f"{b.lower()} "):
                self.number = num[len(b)+1:].strip()
            elif num.lower().startswith(b.lower()) and len(num) > len(b) and num[len(b):][0].isdigit():
                self.number = num[len(b):].strip()
        if self.pk and self.capacity < self.occupied:
            raise ValidationError(f"{self.occupied} students already live here; capacity cannot be lower.")

    def save(self, *args, **kwargs):
        if self.number and self.block:
            b = str(self.block).strip()
            num = str(self.number).strip()
            if num.lower().startswith(f"{b.lower()}-"):
                self.number = num[len(b)+1:].strip()
            elif num.lower().startswith(f"{b.lower()} "):
                self.number = num[len(b)+1:].strip()
            elif num.lower().startswith(b.lower()) and len(num) > len(b) and num[len(b):][0].isdigit():
                self.number = num[len(b):].strip()
        super().save(*args, **kwargs)


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


class Fee(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="fees")
    title = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    due_date = models.DateField()

    class Meta:
        ordering = ["due_date"]

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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
