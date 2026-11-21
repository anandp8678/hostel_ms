import random
from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand

from core import models as m
from core.services import onboard_student

NAMES = ["Asha Nair", "Rahul Menon", "Fatima Beevi", "Arjun Pillai", "Meera Thomas", "Vishnu Das", "Anjali Raj", "Sneha Kurian"]
MEALS = {"B": ["Idli & sambar", "Puttu & kadala", "Dosa & chutney", "Appam & stew", "Upma", "Poori masala", "Pongal"],
         "L": ["Rice, sambar, thoran"] * 7, "S": ["Tea & banana fry"] * 7, "D": ["Chapati & curry", "Rice & fish curry", "Fried rice", "Chapati & dal", "Rice & egg curry", "Biriyani", "Dosa"]}


class Command(BaseCommand):
    help = "Create demo accounts and data (safe to run once on an empty database)."

    def handle(self, *a, **k):
        pw = settings.DEFAULT_PASSWORD
        if m.User.objects.filter(username="admin").exists():
            self.stdout.write("Demo data already present."); return
        m.User.objects.create_superuser("admin", password="Admin@123", first_name="Super", last_name="Admin", role="admin")
        m.User.objects.create_user("warden", password="Warden@123", first_name="Meenakshi", last_name="Warden", role="warden", is_staff=True)
        rooms = [m.Room.objects.create(block=b, number=n, capacity=2) for b in "AB" for n in ("101", "102", "103")]
        for i, name in enumerate(NAMES):
            s = onboard_student(name, f"CS21{i+1:02d}", "B.Tech CSE", f"98765000{i:02d}", f"Parent of {name.split()[0]}",
                                f"98760000{i//2:02d}", rooms[i // 2] if i < 6 else None)
            s.user.must_change_password = False; s.user.set_password("Student@123"); s.user.save()
            if s.parent:
                s.parent.user.must_change_password = False; s.parent.user.set_password("Parent@123"); s.parent.user.save()
        for d, _ in m.MealMenu.DAYS:
            for mk, _ in m.MealMenu.MEALS:
                m.MealMenu.objects.create(day=d, meal=mk, items=MEALS[mk][d])
        random.seed(1)
        students = list(m.Student.objects.all())
        for s in students:
            for back in range(1, 11):
                m.Attendance.objects.create(student=s, date=date.today() - timedelta(days=back), status=random.choice("PPPPPPA"))
            fee = m.Fee.objects.create(student=s, title="Hostel fee - Term 1", amount=15000, due_date=date.today() - timedelta(days=5))
            if s.pk % 2:
                m.Payment.objects.create(fee=fee, amount=15000 if s.pk % 4 == 1 else 6000, method="Cash")
        m.Complaint.objects.create(student=students[0], title="Fan not working", description="The ceiling fan in my room makes noise and stops.")
        m.LeaveRequest.objects.create(student=students[1], from_date=date.today() + timedelta(days=2), to_date=date.today() + timedelta(days=4), reason="Family function")
        m.Announcement.objects.create(title="Welcome", body="Mess timings: breakfast 7:30, lunch 12:30, dinner 7:30.")
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
