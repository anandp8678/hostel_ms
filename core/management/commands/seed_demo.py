import random
from datetime import date, timedelta
from django.utils import timezone

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

        b1, _ = m.Building.objects.get_or_create(
            code="B1",
            defaults={
                "name": "First Year Block",
                "allowed_years": "1",
                "is_first_year_only": True,
                "description": "Dedicated anti-ragging building exclusively for 1st-year students.",
            },
        )
        b2, _ = m.Building.objects.get_or_create(
            code="B2",
            defaults={
                "name": "Senior Block",
                "allowed_years": "2,3,4",
                "is_first_year_only": False,
                "description": "Residential block for 2nd, 3rd, and 4th-year students.",
            },
        )

        cfg = m.FeeConfig.get_config()

        # Two-tier warden hierarchy: Head Warden (all buildings) & First-Year Warden (B1 only)
        m.User.objects.create_user("warden", password="Warden@123", first_name="Meenakshi", last_name="HeadWarden", role="warden", is_staff=True, assigned_building=None)
        m.User.objects.create_user("fywarden", password="Warden@123", first_name="Karthik", last_name="FYWarden", role="warden", is_staff=True, assigned_building=b1)

        b1_specs = [("G", "101", "1_sharing"), ("G", "102", "2_sharing"), ("1", "101", "3_sharing"), ("1", "102", "2_sharing")]
        b2_specs = [("G", "101", "1_sharing"), ("G", "102", "2_sharing"), ("1", "101", "3_sharing"), ("1", "102", "2_sharing"), ("2", "101", "3_sharing"), ("2", "102", "1_sharing")]
        b1_rooms = [m.Room.objects.create(building=b1, block=b, number=n, room_type=rt) for b, n, rt in b1_specs]
        b2_rooms = [m.Room.objects.create(building=b2, block=b, number=n, room_type=rt) for b, n, rt in b2_specs]

        years = [1, 1, 1, 2, 2, 3, 4, 2]
        prefs = ["1_sharing", "2_sharing", "3_sharing", "1_sharing", "2_sharing", "3_sharing", "3_sharing", "2_sharing"]
        b1_assign = {0: b1_rooms[0], 1: b1_rooms[1]}
        b2_assign = {3: b2_rooms[0], 4: b2_rooms[1], 5: b2_rooms[2], 6: b2_rooms[2]}
        for i, name in enumerate(NAMES):
            yr = years[i]
            room = b1_assign.get(i) if yr == 1 else b2_assign.get(i)
            s = onboard_student(
                name,
                f"CS2{7-yr}{i+1:02d}",
                "B.Tech CSE",
                f"98765000{i:02d}",
                f"Parent of {name.split()[0]}",
                f"98760000{i//2:02d}",
                room,
                year_of_study=yr,
                preferred_room_type=prefs[i],
            )
            s.user.must_change_password = False; s.user.set_password("Student@123"); s.user.save()
            if s.parent:
                s.parent.user.must_change_password = False; s.parent.user.set_password("Parent@123"); s.parent.user.save()
        for d, _ in m.MealMenu.DAYS:
            for mk, _ in m.MealMenu.MEALS:
                m.MealMenu.objects.create(day=d, meal=mk, items=MEALS[mk][d])
        random.seed(1)
        students = list(m.Student.objects.all())
        cur_month = date.today().strftime("%Y-%m")
        for s in students:
            for back in range(1, 11):
                m.Attendance.objects.create(student=s, date=date.today() - timedelta(days=back), status=random.choice("PPPPPPA"))
            rent = cfg.get_rent_for_room(s.room)
            food = cfg.food_rate_per_day * 30
            fee = m.Fee.objects.create(
                student=s,
                title=f"Hostel & Mess Fee - {date.today().strftime('%B %Y')}",
                month=cur_month,
                rent_amount=rent,
                food_amount=food,
                amount=rent + food,
                due_date=date.today() - timedelta(days=5),
            )
            if s.pk % 2:
                m.Payment.objects.create(fee=fee, amount=(rent + food) if s.pk % 4 == 1 else rent, method="Cash")
        m.Complaint.objects.create(student=students[0], title="Fan not working", description="The ceiling fan in my room makes noise and stops.")
        m.LeaveRequest.objects.create(student=students[1], from_date=date.today() + timedelta(days=2), to_date=date.today() + timedelta(days=4), reason="Family function")
        m.Announcement.objects.create(title="Welcome", body="Mess timings: breakfast 7:30, lunch 12:30, dinner 7:30.")
        m.Announcement.objects.create(building=b1, title="First Year Orientation", body="Mandatory anti-ragging orientation in First Year Block courtyard at 5 PM.")

        now = timezone.now()
        warden_user = m.User.objects.filter(username="warden").first()
        fy_warden_user = m.User.objects.filter(username="fywarden").first()
        b1_students = [s for s in students if s.building_id == b1.id] or students[:2]
        b2_students = [s for s in students if s.building_id != b1.id] or students[2:4]
        s_fy_1 = b1_students[0]
        s_fy_2 = b1_students[1] if len(b1_students) > 1 else b1_students[0]
        s_sr_1 = b2_students[0]
        s_sr_2 = b2_students[1] if len(b2_students) > 1 else b2_students[0]

        m.GatePass.objects.create(
            student=s_fy_1,
            pass_type="day_outing",
            out_at=now - timedelta(hours=6),
            expected_return_at=now - timedelta(minutes=90),
            reason="Library book purchase and stationery",
            parent_ack="Confirmed",
            parent_ack_at=now - timedelta(hours=7),
            approved_by=fy_warden_user or warden_user,
            status="Late",
        )
        m.GatePass.objects.create(
            student=s_sr_1,
            pass_type="day_outing",
            out_at=now - timedelta(hours=2),
            expected_return_at=now + timedelta(hours=3),
            reason="Department project printouts",
            parent_ack="Pending",
            approved_by=warden_user,
            status="Checked Out",
        )
        m.GatePass.objects.create(
            student=s_fy_2,
            pass_type="home_leave",
            out_at=now + timedelta(hours=5),
            expected_return_at=now + timedelta(days=2),
            reason="Sister graduation ceremony at home",
            parent_ack="Confirmed",
            parent_ack_at=now - timedelta(minutes=30),
            status="Pending",
        )
        m.GatePass.objects.create(
            student=s_sr_2,
            pass_type="day_outing",
            out_at=now + timedelta(hours=1),
            expected_return_at=now + timedelta(hours=6),
            reason="Dental appointment in city clinic",
            parent_ack="Pending",
            approved_by=warden_user,
            status="Approved",
        )
        m.GatePass.objects.create(
            student=students[0],
            pass_type="day_outing",
            out_at=now - timedelta(days=2, hours=5),
            expected_return_at=now - timedelta(days=2, hours=1),
            actual_return_at=now - timedelta(days=2, hours=1, minutes=15),
            reason="Optician visit",
            parent_ack="Confirmed",
            parent_ack_at=now - timedelta(days=2, hours=6),
            approved_by=warden_user,
            status="Returned",
        )
        m.VisitorLog.objects.create(
            student=s_fy_1,
            visitor_name="Ramesh Sharma",
            relation="Father",
            phone="9876543210",
            purpose="Delivering winter clothes and medicines",
            id_proof_last4="4821",
            checked_in_at=now - timedelta(minutes=35),
            recorded_by=fy_warden_user or warden_user,
        )
        m.VisitorLog.objects.create(
            student=s_sr_1,
            visitor_name="Anita Verma",
            relation="Mother",
            phone="9822334455",
            purpose="Weekend family visit",
            id_proof_last4="7390",
            checked_in_at=now - timedelta(days=1, hours=3),
            checked_out_at=now - timedelta(days=1, hours=1),
            recorded_by=warden_user,
        )

        self.stdout.write(self.style.SUCCESS("Demo data ready."))
