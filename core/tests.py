import csv
import io
from datetime import date, timedelta

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Announcement,
    Attendance,
    Building,
    Complaint,
    Fee,
    FeeConfig,
    GatePass,
    LeaveRequest,
    MealMenu,
    Notification,
    Parent,
    Room,
    RoomAllocation,
    Student,
    User,
    VisitorLog,
)
from core.services import (
    allocate_student_to_room,
    bulk_create_rooms,
    bulk_create_rooms_floors,
    bulk_create_rooms_from_list,
    calculate_student_monthly_fee,
    find_room,
    find_student,
    import_allocations,
    import_rooms_csv,
    import_rows,
    onboard_student,
)


class RoomAllocationImportTests(TestCase):
    def setUp(self):
        # Create admin and warden users
        self.admin_user = User.objects.create_superuser(
            username="admin_user",
            password="AdminPassword123",
            role="admin",
            first_name="Admin",
            last_name="One",
        )
        self.warden_user = User.objects.create_user(
            username="warden_user",
            password="WardenPassword123",
            role="warden",
            is_staff=True,
            first_name="Warden",
            last_name="One",
        )
        self.student_user = User.objects.create_user(
            username="student_user",
            password="StudentPassword123",
            role="student",
            first_name="Student",
            last_name="One",
        )

        # Create rooms
        self.room_a1 = Room.objects.create(block="A", number="101", capacity=2)
        self.room_a2 = Room.objects.create(block="A", number="102", capacity=1)
        self.room_b1 = Room.objects.create(block="B", number="201", capacity=2)

        # Create students
        self.student1 = onboard_student(
            name="Rahul Sharma",
            roll_no="CS101",
            course="B.Tech",
            room=None,
        )
        self.student2 = onboard_student(
            name="Priya Patel",
            roll_no="CS102",
            course="B.Tech",
            room=None,
        )

    def test_find_room_formats(self):
        self.assertEqual(find_room("A-101"), self.room_a1)
        self.assertEqual(find_room("a-101"), self.room_a1)
        self.assertEqual(find_room("A - 101"), self.room_a1)
        self.assertEqual(find_room("A 101"), self.room_a1)
        self.assertEqual(find_room("A101"), self.room_a1)
        self.assertEqual(find_room(room_str="101", block_str="A"), self.room_a1)
        self.assertEqual(find_room("B-201"), self.room_b1)
        self.assertIsNone(find_room("Z-999"))

    def test_find_student(self):
        self.assertEqual(find_student(roll_no="CS101"), self.student1)
        self.assertEqual(find_student(roll_no="cs101"), self.student1)
        self.assertEqual(find_student(name="Rahul Sharma"), self.student1)
        self.assertIsNone(find_student(roll_no="UNKNOWN"))

    def test_allocate_student_to_room(self):
        # Initial allocation
        alloc, created = allocate_student_to_room(self.student1, self.room_a1)
        self.assertTrue(created)
        self.assertEqual(self.student1.room, self.room_a1)
        self.assertEqual(self.room_a1.occupied, 1)

        # Repeated allocation to same room is idempotent
        alloc2, created2 = allocate_student_to_room(self.student1, self.room_a1)
        self.assertFalse(created2)
        self.assertEqual(alloc.pk, alloc2.pk)
        self.assertEqual(self.room_a1.occupied, 1)

        # Re-allocation to another room vacates previous room
        alloc3, created3 = allocate_student_to_room(self.student1, self.room_b1)
        self.assertTrue(created3)
        self.assertEqual(self.student1.room, self.room_b1)
        self.assertEqual(self.room_a1.occupied, 0)
        self.assertEqual(self.room_b1.occupied, 1)

    def test_room_capacity_enforced(self):
        # room_a2 has capacity 1
        allocate_student_to_room(self.student1, self.room_a2)
        self.assertEqual(self.room_a2.occupied, 1)
        self.assertEqual(self.room_a2.vacancy, 0)

        # Trying to allocate second student to room_a2 should fail
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            allocate_student_to_room(self.student2, self.room_a2)

    def test_import_allocations_success(self):
        rows = [
            {"roll_no": "CS101", "room": "A-101"},
            {"roll_no": "CS102", "room": "A-101"},
        ]
        res = import_allocations(rows)
        self.assertEqual(res["allocated"], 2)
        self.assertEqual(len(res["errors"]), 0)
        self.assertEqual(self.student1.room, self.room_a1)
        self.assertEqual(self.student2.room, self.room_a1)
        self.assertEqual(self.room_a1.occupied, 2)

    def test_import_allocations_handles_alternate_headers(self):
        rows = [
            {"Roll Number": "CS101", "Allocated Room": "B-201", "Start Date": "2026-11-21"},
        ]
        res = import_allocations(rows)
        self.assertEqual(res["allocated"], 1)
        self.assertEqual(self.student1.room, self.room_b1)

    def test_import_allocations_capacity_and_errors(self):
        # room_a2 capacity is 1
        rows = [
            {"roll_no": "CS101", "room": "A-102"},
            {"roll_no": "CS102", "room": "A-102"},  # Should fail as room_a2 is full
            {"roll_no": "NONEXISTENT", "room": "A-101"},  # Student not found
            {"roll_no": "CS101", "room": "INVALID-999"},  # Invalid room
        ]
        res = import_allocations(rows)
        self.assertEqual(res["allocated"], 1)
        self.assertEqual(len(res["errors"]), 3)
        self.assertIn("full", res["errors"][0])
        self.assertIn("not found", res["errors"][1])
        self.assertIn("not found", res["errors"][2])

    def test_import_allocations_creates_student_if_name_provided(self):
        rows = [
            {"name": "Sneha Reddy", "roll_no": "CS103", "room": "B-201"},
        ]
        res = import_allocations(rows)
        self.assertEqual(res["allocated"], 1)
        self.assertEqual(res["created_students"], 1)
        new_student = Student.objects.get(roll_no="CS103")
        self.assertEqual(new_student.room, self.room_b1)

    def test_import_rows_updates_existing_student_allocation(self):
        # Existing student CS101 without room gets room allocated via student import CSV
        rows = [
            {"name": "Rahul Sharma", "roll_no": "CS101", "course": "B.Tech IT", "room": "A-101"},
        ]
        created, errors = import_rows(rows)
        self.assertEqual(created, 1)
        self.assertEqual(len(errors), 0)
        self.student1.refresh_from_db()
        self.assertEqual(self.student1.room, self.room_a1)
        self.assertEqual(self.student1.course, "B.Tech IT")

    def test_views_access_control(self):
        c = Client()

        # Anonymous cannot access
        resp = c.get(reverse("allocations_import"))
        self.assertEqual(resp.status_code, 302)

        # Student user gets 403 Forbidden
        c.force_login(self.student_user)
        resp = c.get(reverse("allocations_import"))
        self.assertEqual(resp.status_code, 403)

        # Warden user gets 200 OK
        c.force_login(self.warden_user)
        resp = c.get(reverse("allocations_import"))
        self.assertEqual(resp.status_code, 200)

        # Template download works
        resp = c.get(reverse("allocations_import_template"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("roll_no", resp.content.decode("utf-8"))
        self.assertIn("room", resp.content.decode("utf-8"))
        self.assertIn("parent_name", resp.content.decode("utf-8"))

    def test_combined_csv_all_information(self):
        rows = [
            {
                "name": "Kavya Menon",
                "roll_no": "CS999",
                "course": "B.Tech AI",
                "phone": "9999900001",
                "parent_name": "Ramesh Menon",
                "parent_phone": "9999900002",
                "room": "B-201",
                "start_date": "2026-11-21",
            }
        ]
        res = import_rows(rows)
        self.assertEqual(res.created, 1)
        self.assertEqual(res.allocated, 1)
        self.assertEqual(len(res.errors), 0)

        # Verify student
        student = Student.objects.get(roll_no="CS999")
        self.assertEqual(student.user.first_name, "Kavya")
        self.assertEqual(student.user.last_name, "Menon")
        self.assertEqual(student.user.phone, "9999900001")
        self.assertEqual(student.course, "B.Tech AI")

        # Verify parent
        self.assertIsNotNone(student.parent)
        self.assertEqual(student.parent.user.username, "p9999900002")
        self.assertEqual(student.parent.user.first_name, "Ramesh")
        self.assertEqual(student.parent.user.last_name, "Menon")

        # Verify room allocation
        self.assertEqual(student.room, self.room_b1)
        alloc = student.allocations.filter(end_date__isnull=True).first()
        self.assertEqual(str(alloc.start_date), "2026-11-21")

    def test_views_upload_csv(self):
        c = Client()
        c.force_login(self.warden_user)

        csv_content = "roll_no,room\nCS101,B-201\nCS102,B-201\n"
        csv_file = SimpleUploadedFile("allocations.csv", csv_content.encode("utf-8"), content_type="text/csv")

        resp = c.post(reverse("allocations_import"), {"file": csv_file})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "2")
        self.assertContains(resp, "successfully allocated")

        self.student1.refresh_from_db()
        self.student2.refresh_from_db()
        self.assertEqual(self.student1.room, self.room_b1)
        self.assertEqual(self.student2.room, self.room_b1)


class BulkRoomCreationTests(TestCase):
    def setUp(self):
        self.warden = User.objects.create_user(
            username="warden_bulk",
            password="WardenPassword123",
            role="warden",
            is_staff=True,
        )

    def test_bulk_create_rooms_by_count(self):
        # Create 10 rooms in Block C: C-101 to C-110
        res = bulk_create_rooms(block="C", start_number="101", count=10, capacity=2)
        self.assertEqual(len(res["created"]), 10)
        self.assertEqual(len(res["skipped"]), 0)
        self.assertEqual(len(res["errors"]), 0)
        self.assertTrue(Room.objects.filter(block="C", number="101").exists())
        self.assertTrue(Room.objects.filter(block="C", number="110").exists())
        self.assertFalse(Room.objects.filter(block="C", number="111").exists())

    def test_bulk_create_rooms_by_range(self):
        # Create rooms C-201 to C-205 (5 rooms)
        res = bulk_create_rooms(block="C", start_number="201", end_number="205", capacity=3)
        self.assertEqual(len(res["created"]), 5)
        self.assertEqual(Room.objects.filter(block="C", number="201").first().capacity, 3)

    def test_bulk_create_rooms_skips_existing(self):
        Room.objects.create(block="C", number="301", capacity=2)
        # Generate 301..303
        res = bulk_create_rooms(block="C", start_number="301", count=3, capacity=2)
        self.assertEqual(len(res["created"]), 2)  # 302 and 303
        self.assertEqual(len(res["skipped"]), 1)  # C-301

    def test_bulk_create_rooms_padding_and_prefix(self):
        res = bulk_create_rooms(block="D", start_number="01", count=5, prefix="G-")
        self.assertEqual(len(res["created"]), 5)
        self.assertTrue(Room.objects.filter(block="D", number="G-01").exists())
        self.assertTrue(Room.objects.filter(block="D", number="G-05").exists())

    def test_bulk_create_rooms_floors(self):
        # Floors 1 to 2, 5 rooms per floor
        res = bulk_create_rooms_floors(block="E", start_floor=1, end_floor=2, rooms_per_floor=5, capacity=2)
        self.assertEqual(len(res["created"]), 10)
        self.assertTrue(Room.objects.filter(block="E", number="101").exists())
        self.assertTrue(Room.objects.filter(block="E", number="105").exists())
        self.assertTrue(Room.objects.filter(block="E", number="201").exists())
        self.assertTrue(Room.objects.filter(block="E", number="205").exists())

    def test_bulk_create_rooms_from_list(self):
        text = "401, 402; 403\nF-501"
        res = bulk_create_rooms_from_list(block="F", text=text, default_capacity=2)
        self.assertEqual(len(res["created"]), 4)
        self.assertTrue(Room.objects.filter(block="F", number="401").exists())
        self.assertTrue(Room.objects.filter(block="F", number="501").exists())

    def test_import_rooms_csv(self):
        rows = [
            {"block": "G", "number": "101", "capacity": "2"},
            {"block": "G", "number": "102", "capacity": "3"},
        ]
        res = import_rooms_csv(rows)
        self.assertEqual(len(res["created"]), 2)
        self.assertEqual(Room.objects.get(block="G", number="102").capacity, 3)

    def test_room_bulk_add_view_get(self):
        c = Client()
        c.force_login(self.warden)
        resp = c.get(reverse("rooms_bulk_add"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Bulk Add Rooms")

    def test_room_bulk_add_view_post_count(self):
        c = Client()
        c.force_login(self.warden)
        resp = c.post(reverse("rooms_bulk_add"), {
            "mode": "count",
            "block": "H",
            "start_number": "101",
            "count": 5,
            "capacity": 2,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "5")
        self.assertContains(resp, "room(s) created successfully")
        self.assertEqual(Room.objects.filter(block="H").count(), 5)

    def test_room_bulk_template_download(self):
        c = Client()
        c.force_login(self.warden)
        resp = c.get(reverse("rooms_bulk_template"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("block,number,capacity", resp.content.decode("utf-8"))


class RoomFilterAndBulkActionsTests(TestCase):
    def setUp(self):
        self.warden = User.objects.create_user(
            username="warden_rooms",
            password="WardenPassword123",
            role="warden",
            is_staff=True,
        )
        self.room_a = Room.objects.create(block="A", number="101", capacity=2)
        self.room_b = Room.objects.create(block="A", number="102", capacity=2)
        self.room_c = Room.objects.create(block="B", number="201", capacity=3, is_blacklisted=True, blacklist_reason="Renovation")
        self.student = onboard_student(name="Test Student", roll_no="TS001", room=self.room_a)

    def test_room_blacklist_blocks_allocation(self):
        from django.core.exceptions import ValidationError
        s2 = onboard_student(name="Student Two", roll_no="TS002")
        with self.assertRaises(ValidationError) as ctx:
            allocate_student_to_room(s2, self.room_c)
        self.assertIn("blacklisted", str(ctx.exception))
        self.assertIn("Renovation", str(ctx.exception))

    def test_room_filters_in_view(self):
        c = Client()
        c.force_login(self.warden)

        # All rooms
        resp = c.get(reverse("rooms"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "A-101")
        self.assertContains(resp, "B-201")

        # Filter by block
        resp = c.get(reverse("rooms") + "?block=A")
        self.assertContains(resp, "A-101")
        self.assertNotContains(resp, "B-201")

        # Filter by blacklisted
        resp = c.get(reverse("rooms") + "?status=blacklisted")
        self.assertContains(resp, "B-201")
        self.assertNotContains(resp, "A-101")

        # Filter by specific cause
        resp = c.get(reverse("rooms") + "?cause=Renovation")
        self.assertContains(resp, "B-201")
        self.assertContains(resp, "Renovation")

        # Filter by capacity
        resp = c.get(reverse("rooms") + "?capacity=3")
        self.assertContains(resp, "B-201")
        self.assertNotContains(resp, "A-101")

        # Search query
        resp = c.get(reverse("rooms") + "?q=102")
        self.assertContains(resp, "A-102")
        self.assertNotContains(resp, "B-201")

    def test_bulk_action_blacklist(self):
        c = Client()
        c.force_login(self.warden)

        resp = c.post(reverse("rooms"), {
            "bulk_action": "blacklist",
            "blacklist_cause": "Electrical Repair",
            "selected_rooms": [self.room_a.pk, self.room_b.pk],
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.room_a.refresh_from_db()
        self.room_b.refresh_from_db()
        self.assertTrue(self.room_a.is_blacklisted)
        self.assertEqual(self.room_a.blacklist_reason, "Electrical Repair")
        self.assertTrue(self.room_b.is_blacklisted)

    def test_bulk_action_activate(self):
        c = Client()
        c.force_login(self.warden)

        resp = c.post(reverse("rooms"), {
            "bulk_action": "activate",
            "selected_rooms": [self.room_c.pk],
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.room_c.refresh_from_db()
        self.assertFalse(self.room_c.is_blacklisted)
        self.assertEqual(self.room_c.blacklist_reason, "")

    def test_bulk_action_change_capacity(self):
        c = Client()
        c.force_login(self.warden)

        resp = c.post(reverse("rooms"), {
            "bulk_action": "change_capacity",
            "new_capacity": "4",
            "selected_rooms": [self.room_b.pk],
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.room_b.refresh_from_db()
        self.assertEqual(self.room_b.capacity, 4)

    def test_bulk_action_delete_prevents_occupied(self):
        c = Client()
        c.force_login(self.warden)

        # room_a is occupied by TS001, so it shouldn't be deleted
        resp = c.post(reverse("rooms"), {
            "bulk_action": "delete",
            "selected_rooms": [self.room_a.pk],
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Room.objects.filter(pk=self.room_a.pk).exists())
        self.assertContains(resp, "Cannot delete")

        # room_b is unoccupied, so it can be deleted
        resp = c.post(reverse("rooms"), {
            "bulk_action": "delete",
            "selected_rooms": [self.room_b.pk],
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Room.objects.filter(pk=self.room_b.pk).exists())

    def test_room_toggle_blacklist_view(self):
        c = Client()
        c.force_login(self.warden)

        # Blacklist room_b
        resp = c.post(reverse("room_toggle_blacklist", args=[self.room_b.pk]), {"cause": "Pest Control"}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.room_b.refresh_from_db()
        self.assertTrue(self.room_b.is_blacklisted)
        self.assertEqual(self.room_b.blacklist_reason, "Pest Control")

        # Un-blacklist room_b
        resp = c.post(reverse("room_toggle_blacklist", args=[self.room_b.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.room_b.refresh_from_db()
        self.assertFalse(self.room_b.is_blacklisted)


class RoomPrefixAndDisplayTests(TestCase):
    def test_room_string_representation_uses_block_as_prefix(self):
        r1 = Room.objects.create(block="A", number="101", capacity=2)
        self.assertEqual(str(r1), "A-101")

        r2 = Room.objects.create(block="B", number="205", capacity=3)
        self.assertEqual(str(r2), "B-205")

    def test_room_cleans_redundant_prefix_on_save(self):
        # User enters "A-101" for Block A
        r = Room.objects.create(block="A", number="A-101", capacity=2)
        self.assertEqual(r.number, "101")
        self.assertEqual(str(r), "A-101")

        # User enters "A102" for Block A
        r2 = Room.objects.create(block="A", number="A102", capacity=2)
        self.assertEqual(r2.number, "102")
        self.assertEqual(str(r2), "A-102")

    def test_bulk_create_rooms_uses_block_as_prefix(self):
        # Generating with block="A", start_number="101"
        res = bulk_create_rooms(block="A", start_number="101", count=3)
        self.assertEqual(len(res["created"]), 3)
        self.assertEqual([str(r) for r in res["created"]], ["A-101", "A-102", "A-103"])

        # Generating with block="K", start_number="K-501" automatically parses K and 501
        res2 = bulk_create_rooms(block="K", start_number="K-501", count=2)
        self.assertEqual(len(res2["created"]), 2)
        self.assertEqual([str(r) for r in res2["created"]], ["K-501", "K-502"])

    def test_room_extracts_block_if_typed_in_number(self):
        from core.forms import RoomForm
        # Even if block is left as "A", entering "G 101" or "G-101" detects Block G
        form1 = RoomForm(data={"block": "A", "number": "G 101", "capacity": 2})
        self.assertTrue(form1.is_valid(), form1.errors)
        r1 = form1.save()
        self.assertEqual(r1.block, "G")
        self.assertEqual(r1.number, "101")
        self.assertEqual(str(r1), "G-101")

        form2 = RoomForm(data={"block": "A", "number": "G-201", "capacity": 2})
        self.assertTrue(form2.is_valid(), form2.errors)
        r2 = form2.save()
        self.assertEqual(r2.block, "G")
        self.assertEqual(r2.number, "201")
        self.assertEqual(str(r2), "G-201")

    def test_bulk_add_view_creates_g_101_to_g_110(self):
        warden = User.objects.create_user(username="warden_g", password="WardenPassword123", role="warden", is_staff=True)
        c = Client()
        c.force_login(warden)

        # 1. User enters Block G and start_number 101 for 10 rooms
        resp = c.post(reverse("rooms_bulk_add"), {
            "mode": "count",
            "block": "G",
            "start_number": "101",
            "count": "10",
            "capacity": "2",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Room.objects.filter(block="G", number="101").exists())
        self.assertTrue(Room.objects.filter(block="G", number="110").exists())
        self.assertEqual(Room.objects.filter(block="G").count(), 10)

        # 2. User enters start_number as "H 101" directly (even if block was left as default A)
        resp2 = c.post(reverse("rooms_bulk_add"), {
            "mode": "count",
            "block": "A",
            "start_number": "H 101",
            "count": "5",
            "capacity": "2",
        })
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(Room.objects.filter(block="H", number="101").exists())
        self.assertTrue(Room.objects.filter(block="H", number="105").exists())
        self.assertEqual(Room.objects.filter(block="H").count(), 5)

        # 3. Simulate browser form submission with multiple block values from tabs ['J', 'A', 'A']
        resp3 = c.post(reverse("rooms_bulk_add"), {
            "mode": "count",
            "block": ["J", "A", "A"],
            "start_number": "101",
            "count": "10",
            "capacity": ["2", "2", "2"],
        })
        self.assertEqual(resp3.status_code, 200)
        self.assertTrue(Room.objects.filter(block="J", number="101").exists())
        self.assertTrue(Room.objects.filter(block="J", number="110").exists())
        self.assertEqual(Room.objects.filter(block="J").count(), 10)
        self.assertEqual(Room.objects.filter(block="A").count(), 0)


class AttendanceMonthlyHistoryAndFilterTests(TestCase):
    def setUp(self):
        self.warden = User.objects.create_user(
            username="warden_att",
            password="WardenPassword123",
            role="warden",
            is_staff=True,
        )
        self.room_a = Room.objects.create(block="A", number="101", capacity=2)
        self.room_b = Room.objects.create(block="B", number="201", capacity=2)

        self.student_a = onboard_student(name="Anil Kumar", roll_no="AK101", room=self.room_a)
        self.student_b = onboard_student(name="Bhavna Sharma", roll_no="BS201", room=self.room_b)

        # November 2026 records
        Attendance.objects.create(student=self.student_a, date=date(2026, 11, 1), status="P")
        Attendance.objects.create(student=self.student_a, date=date(2026, 11, 2), status="A")
        Attendance.objects.create(student=self.student_b, date=date(2026, 11, 1), status="L")
        Attendance.objects.create(student=self.student_b, date=date(2026, 11, 2), status="P")

        # October 2026 records
        Attendance.objects.create(student=self.student_a, date=date(2026, 10, 15), status="P")
        Attendance.objects.create(student=self.student_b, date=date(2026, 10, 15), status="A")

    def test_attendance_history_categorized_by_month(self):
        c = Client()
        c.force_login(self.warden)

        # November view
        resp = c.get(reverse("attendance_history") + "?month=2026-11")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "November 2026")
        self.assertContains(resp, "Anil Kumar")
        self.assertContains(resp, "Bhavna Sharma")
        self.assertEqual(resp.context["total_records"], 4)

        # October view
        resp2 = c.get(reverse("attendance_history") + "?month=2026-10")
        self.assertEqual(resp2.status_code, 200)
        self.assertContains(resp2, "October 2026")
        self.assertEqual(resp2.context["total_records"], 2)

    def test_attendance_history_filter_by_room(self):
        c = Client()
        c.force_login(self.warden)

        # Filter by Room A
        resp = c.get(reverse("attendance_history") + f"?month=2026-11&room={self.room_a.pk}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "AK101")
        self.assertNotContains(resp, "BS201")

        # Filter by Room B
        resp2 = c.get(reverse("attendance_history") + f"?month=2026-11&room={self.room_b.pk}")
        self.assertEqual(resp2.status_code, 200)
        self.assertContains(resp2, "BS201")
        self.assertNotContains(resp2, "AK101")

    def test_attendance_history_filter_by_name(self):
        c = Client()
        c.force_login(self.warden)

        resp = c.get(reverse("attendance_history") + "?month=2026-11&q=Anil")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Anil Kumar")
        self.assertNotContains(resp, "Bhavna Sharma")

    def test_attendance_history_filter_absent_and_leave(self):
        c = Client()
        c.force_login(self.warden)

        # Those who are absent
        resp_absent = c.get(reverse("attendance_history") + "?month=2026-11&status=A")
        self.assertEqual(resp_absent.status_code, 200)
        self.assertEqual(resp_absent.context["total_filtered_logs"], 1)
        self.assertContains(resp_absent, "AK101")
        self.assertContains(resp_absent, "Absent")

        # Those who are on leave
        resp_leave = c.get(reverse("attendance_history") + "?month=2026-11&status=L")
        self.assertEqual(resp_leave.status_code, 200)
        self.assertEqual(resp_leave.context["total_filtered_logs"], 1)
        self.assertContains(resp_leave, "BS201")
        self.assertContains(resp_leave, "On Leave")

    def test_attendance_history_summary_view_mode(self):
        c = Client()
        c.force_login(self.warden)

        resp = c.get(reverse("attendance_history") + "?month=2026-11&view_mode=summary")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Student Monthly Summary")
        self.assertContains(resp, "AK101")
        self.assertContains(resp, "BS201")

    def test_attendance_history_csv_export(self):
        c = Client()
        c.force_login(self.warden)

        resp = c.get(reverse("attendance_history") + "?month=2026-11&export=csv")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        content = resp.content.decode("utf-8")
        self.assertIn("Date,Roll no,Name,Room,Status", content)
        self.assertIn("AK101", content)
        self.assertIn("BS201", content)

    def test_daily_attendance_filters(self):
        c = Client()
        c.force_login(self.warden)

        # Filter daily attendance by Room A
        resp = c.get(reverse("attendance") + f"?date=2026-11-01&room={self.room_a.pk}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "AK101")
        self.assertNotContains(resp, "BS201")

        # Filter daily attendance by Name
        resp2 = c.get(reverse("attendance") + "?date=2026-11-01&q=Bhavna")
        self.assertEqual(resp2.status_code, 200)
        self.assertContains(resp2, "BS201")
        self.assertNotContains(resp2, "AK101")


class StudentRoommatesAndDirectoryTests(TestCase):
    def setUp(self):
        # Create Room
        self.room = Room.objects.create(block="A", number="101", capacity=2)
        self.other_room = Room.objects.create(block="B", number="201", capacity=2)

        # Create Parent 1 & Student 1
        p_user1 = User.objects.create_user(username="p_mohan", password="Pw1", first_name="Mohan", last_name="Nair", phone="9876543210", role="parent")
        self.parent1 = Parent.objects.create(user=p_user1)
        s_user1 = User.objects.create_user(username="s_asha", password="Pw1", first_name="Asha", last_name="Nair", phone="9111111111", role="student")
        self.student1 = Student.objects.create(user=s_user1, roll_no="CS101", course="CSE", parent=self.parent1)
        RoomAllocation.objects.create(student=self.student1, room=self.room, start_date=date.today())

        # Create Parent 2 & Student 2 (Roommate of Student 1)
        p_user2 = User.objects.create_user(username="p_suresh", password="Pw2", first_name="Suresh", last_name="Menon", phone="9876543211", role="parent")
        self.parent2 = Parent.objects.create(user=p_user2)
        s_user2 = User.objects.create_user(username="s_rahul", password="Pw2", first_name="Rahul", last_name="Menon", phone="9222222222", role="student")
        self.student2 = Student.objects.create(user=s_user2, roll_no="CS102", course="CSE", parent=self.parent2)
        RoomAllocation.objects.create(student=self.student2, room=self.room, start_date=date.today())

        # Create Student 3 in other room
        s_user3 = User.objects.create_user(username="s_kiran", password="Pw3", first_name="Kiran", last_name="Kumar", role="student")
        self.student3 = Student.objects.create(user=s_user3, roll_no="EC201", course="ECE")
        RoomAllocation.objects.create(student=self.student3, room=self.other_room, start_date=date.today())

    def test_student_dashboard_displays_roommate_emergency_and_parent_info(self):
        c = Client()
        c.force_login(self.student1.user)

        resp = c.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)

        # Should show allocated room
        self.assertContains(resp, "A-101")
        # Should show roommate Rahul Menon
        self.assertContains(resp, "Rahul Menon")
        # Should include roommate's roll number
        self.assertContains(resp, "CS102")
        # Should include roommate's parent name for urgency
        self.assertContains(resp, "Suresh Menon")
        # Should include roommate's parent emergency phone number
        self.assertContains(resp, "9876543211")
        # Should have call parent button link
        self.assertContains(resp, "tel:9876543211")

    def test_student_rooms_directory_access_and_search(self):
        c = Client()
        c.force_login(self.student1.user)

        # Access directory
        resp = c.get(reverse("student_rooms"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Hostel Rooms Directory")
        # Contains room numbers
        self.assertContains(resp, "A-101")
        self.assertContains(resp, "B-201")
        # Contains resident names
        self.assertContains(resp, "Asha Nair")
        self.assertContains(resp, "Rahul Menon")
        self.assertContains(resp, "Kiran Kumar")

        # Search by student name
        resp_search_name = c.get(reverse("student_rooms") + "?q=Kiran")
        self.assertEqual(resp_search_name.status_code, 200)
        self.assertContains(resp_search_name, "Room B-201")
        self.assertContains(resp_search_name, "Kiran Kumar")
        self.assertNotContains(resp_search_name, "Room A-101")

        # Search by room number
        resp_search_room = c.get(reverse("student_rooms") + "?q=A-101")
        self.assertEqual(resp_search_room.status_code, 200)
        self.assertContains(resp_search_room, "Room A-101")
        self.assertNotContains(resp_search_room, "Room B-201")

        # Filter by block
        resp_block = c.get(reverse("student_rooms") + "?block=A")
        self.assertEqual(resp_block.status_code, 200)
        self.assertContains(resp_block, "Room A-101")
        self.assertNotContains(resp_block, "Room B-201")

    def test_unauthenticated_user_redirected(self):
        c = Client()
        resp = c.get(reverse("student_rooms"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.url)


class StudentComplaintResolutionTests(TestCase):
    def setUp(self):
        # Create Student 1
        u1 = User.objects.create_user(username="student_c1", password="Pw1", role="student", first_name="Asha")
        self.student1 = Student.objects.create(user=u1, roll_no="CMP101")

        # Create Student 2
        u2 = User.objects.create_user(username="student_c2", password="Pw2", role="student", first_name="Rahul")
        self.student2 = Student.objects.create(user=u2, roll_no="CMP102")

        # Create Complaint for Student 1
        self.comp1 = Complaint.objects.create(
            student=self.student1,
            title="Fan not working",
            description="The regulator is broken and fan is stuck at high speed.",
            status="Submitted"
        )

    def test_student_can_view_complaints_page_and_submit_new(self):
        c = Client()
        c.force_login(self.student1.user)

        resp = c.get(reverse("my_complaints"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Fan not working")
        self.assertContains(resp, "Submitted")
        self.assertContains(resp, "Mark as Solved")

        # Submit new complaint
        post_resp = c.post(reverse("my_complaints"), {
            "title": "Water leakage in tap",
            "description": "Bathroom tap is leaking continuously."
        })
        self.assertEqual(post_resp.status_code, 302)
        self.assertTrue(Complaint.objects.filter(title="Water leakage in tap", student=self.student1).exists())

    def test_student_can_mark_complaint_as_solved_after_admin_status_change(self):
        c = Client()
        c.force_login(self.student1.user)

        # Admin / Warden notices and changes status to 'In Progress' with a staff note
        self.comp1.status = "In Progress"
        self.comp1.staff_note = "Electrician scheduled for today at 3:00 PM."
        self.comp1.save()

        # Student views complaint
        resp = c.get(reverse("my_complaints"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "In Progress")
        self.assertContains(resp, "Electrician scheduled for today at 3:00 PM.")
        self.assertContains(resp, "Mark as Solved")

        # Student marks complaint as resolved
        resolve_resp = c.post(reverse("my_complaint_resolve", args=[self.comp1.pk]))
        self.assertEqual(resolve_resp.status_code, 302)

        # Verify status in DB
        self.comp1.refresh_from_db()
        self.assertEqual(self.comp1.status, "Resolved")

        # Page now displays resolved badge
        resp_after = c.get(reverse("my_complaints"))
        self.assertContains(resp_after, "Resolved")

    def test_student_cannot_resolve_other_student_complaint(self):
        c = Client()
        c.force_login(self.student2.user)  # Student 2 attempts to resolve Student 1's complaint

        resp = c.post(reverse("my_complaint_resolve", args=[self.comp1.pk]))
        self.assertEqual(resp.status_code, 404)
        self.comp1.refresh_from_db()
        self.assertEqual(self.comp1.status, "Submitted")  # remains unchanged








class FeeStructureAndMessCutTests(TestCase):
    def setUp(self):
        # Create Warden
        self.warden_user = User.objects.create_user(
            username="warden1", password="password123", role="warden", is_staff=True, first_name="Warden", last_name="Chief"
        )
        # Create Student
        self.student_user = User.objects.create_user(
            username="stud101", password="password123", role="student", first_name="Rahul", last_name="Sharma"
        )
        self.student = Student.objects.create(user=self.student_user, roll_no="CS101", is_active=True)
        # Room
        self.room = Room.objects.create(block="G", number="101", capacity=2)
        RoomAllocation.objects.create(student=self.student, room=self.room, start_date=date(2026, 1, 1))

    def test_fee_config_defaults_and_warden_edit(self):
        cfg = FeeConfig.get_config()
        self.assertEqual(cfg.rent_amount, Decimal("1500.00"))
        self.assertEqual(cfg.food_rate_per_day, Decimal("130.00"))
        self.assertEqual(cfg.mess_cut_min_days, 3)
        self.assertEqual(cfg.mess_cut_rate_per_day, Decimal("130.00"))

        c = Client()
        c.force_login(self.warden_user)
        resp = c.get(reverse("fee_structure"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "1500.00")
        self.assertContains(resp, "130.00")

        # Warden updates fee parameters
        post_resp = c.post(reverse("fee_structure"), {
            "rent_amount": "1650.00",
            "food_rate_per_day": "140.00",
            "mess_cut_min_days": "3",
            "mess_cut_rate_per_day": "140.00",
            "mess_cut_mode": "per_streak",
            "count_leaves": True,
        })
        self.assertEqual(post_resp.status_code, 302)
        cfg.refresh_from_db()
        self.assertEqual(cfg.rent_amount, Decimal("1650.00"))
        self.assertEqual(cfg.food_rate_per_day, Decimal("140.00"))
        self.assertEqual(cfg.mess_cut_rate_per_day, Decimal("140.00"))

    def test_mess_cut_consecutive_absent_days_rule(self):
        # Month: November 2026 (30 days); self.room is capacity=2 (2 Sharing -> ₹2000.00 default rent)
        # 1. Full attendance -> no mess cut
        calc_full = calculate_student_monthly_fee(self.student, 2026, 11)
        self.assertEqual(calc_full["rent_amount"], Decimal("2000.00"))
        self.assertEqual(calc_full["food_amount"], Decimal("3900.00"))  # 30 * 130
        self.assertEqual(calc_full["mess_cut_days"], 0)
        self.assertEqual(calc_full["mess_cut_amount"], Decimal("0.00"))
        self.assertEqual(calc_full["total_amount"], Decimal("5900.00"))

        # 2. Absent for 2 consecutive days -> below threshold of 3 days -> 0 mess cut
        Attendance.objects.create(student=self.student, date=date(2026, 11, 4), status="A")
        Attendance.objects.create(student=self.student, date=date(2026, 11, 5), status="A")
        calc_2d = calculate_student_monthly_fee(self.student, 2026, 11)
        self.assertEqual(calc_2d["mess_cut_days"], 0)
        self.assertEqual(calc_2d["mess_cut_amount"], Decimal("0.00"))

        # 3. Absent for 3 consecutive days -> qualifies!
        Attendance.objects.create(student=self.student, date=date(2026, 11, 6), status="A")
        calc_3d = calculate_student_monthly_fee(self.student, 2026, 11)
        # Per streak mode (default): 1 day cut of ₹130
        self.assertEqual(calc_3d["mess_cut_days"], 1)
        self.assertEqual(calc_3d["mess_cut_amount"], Decimal("130.00"))
        self.assertEqual(calc_3d["total_amount"], Decimal("5770.00"))  # 2000 + 3900 - 130

        # 4. Another single absence later in the month (e.g. Nov 20) -> does not form 3 consecutive days
        Attendance.objects.create(student=self.student, date=date(2026, 11, 20), status="A")
        calc_extra = calculate_student_monthly_fee(self.student, 2026, 11)
        self.assertEqual(calc_extra["mess_cut_days"], 1)  # still 1 from the 3-day streak
        self.assertEqual(calc_extra["mess_cut_amount"], Decimal("130.00"))

    def test_mess_cut_full_cut_mode(self):
        cfg = FeeConfig.get_config()
        cfg.mess_cut_mode = "per_day"
        cfg.save()

        # Absent for 3 consecutive days: Nov 10, 11, 12
        for d in (10, 11, 12):
            Attendance.objects.create(student=self.student, date=date(2026, 11, d), status="A")

        calc = calculate_student_monthly_fee(self.student, 2026, 11, config=cfg)
        self.assertEqual(calc["mess_cut_days"], 3)
        self.assertEqual(calc["mess_cut_amount"], Decimal("390.00"))  # 3 * 130
        self.assertEqual(calc["total_amount"], Decimal("5510.00"))  # 2000 + 3900 - 390

    def test_warden_generate_monthly_fees_and_manual_override(self):
        # Mark 3 consecutive absences
        for d in (5, 6, 7):
            Attendance.objects.create(student=self.student, date=date(2026, 11, d), status="A")

        c = Client()
        c.force_login(self.warden_user)

        # GET calculation page
        resp = c.get(reverse("fees_calculate_monthly") + "?month=2026-11")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Rahul Sharma")
        self.assertContains(resp, "1500.00")
        self.assertContains(resp, "3900.00")
        self.assertContains(resp, "130.00")

        # Warden modifies the rent to 1400 and mess cut to 260 for this student
        post_data = {
            "save_fees": "1",
            "month": "2026-11",
            "title": "Hostel & Mess Fee - November 2026",
            "due_date": "2026-12-10",
            "selected_students": [str(self.student.pk)],
            f"rent_{self.student.pk}": "1400.00",
            f"food_{self.student.pk}": "3900.00",
            f"mess_days_{self.student.pk}": "2",
            f"mess_cut_{self.student.pk}": "260.00",
            f"total_{self.student.pk}": "5040.00",
        }
        gen_resp = c.post(reverse("fees_calculate_monthly"), post_data)
        self.assertEqual(gen_resp.status_code, 302)

        # Verify Fee created in DB with warden's edited values
        fee = Fee.objects.filter(student=self.student, month="2026-11").first()
        self.assertIsNotNone(fee)
        self.assertEqual(fee.rent_amount, Decimal("1400.00"))
        self.assertEqual(fee.food_amount, Decimal("3900.00"))
        self.assertEqual(fee.mess_cut_days, 2)
        self.assertEqual(fee.mess_cut_amount, Decimal("260.00"))
        self.assertEqual(fee.amount, Decimal("5040.00"))

    def test_student_can_view_fee_breakdown_with_mess_cut(self):
        fee = Fee.objects.create(
            student=self.student,
            title="Hostel & Mess Fee - November 2026",
            month="2026-11",
            rent_amount=Decimal("1500.00"),
            food_amount=Decimal("3900.00"),
            mess_cut_days=1,
            mess_cut_amount=Decimal("130.00"),
            amount=Decimal("5270.00"),
            due_date=date(2026, 12, 10),
        )

        c = Client()
        c.force_login(self.student_user)

        # Student checks /me/fees/
        resp = c.get(reverse("my_fees"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Hostel &amp; Mess Fee - November 2026")
        self.assertContains(resp, "₹1500.00")
        self.assertContains(resp, "₹3900.00")
        self.assertContains(resp, "-₹130.00")
        self.assertContains(resp, "₹5270.00")

    def test_warden_can_edit_existing_fee(self):
        fee = Fee.objects.create(
            student=self.student,
            title="Hostel Fee",
            rent_amount=Decimal("1500.00"),
            food_amount=Decimal("3900.00"),
            mess_cut_amount=Decimal("0.00"),
            amount=Decimal("5400.00"),
            due_date=date(2026, 12, 10),
        )

        c = Client()
        c.force_login(self.warden_user)

        # Warden edits the fee to grant a mess cut
        edit_resp = c.post(reverse("fees_edit", args=[fee.pk]), {
            "title": "Hostel Fee (Adjusted)",
            "rent_amount": "1500.00",
            "food_amount": "3900.00",
            "mess_cut_days": "1",
            "mess_cut_amount": "130.00",
            "amount": "5270.00",
            "due_date": "2026-12-10",
        })
        self.assertEqual(edit_resp.status_code, 302)
        fee.refresh_from_db()
        self.assertEqual(fee.mess_cut_days, 1)
        self.assertEqual(fee.mess_cut_amount, Decimal("130.00"))
        self.assertEqual(fee.amount, Decimal("5270.00"))

    def test_late_fine_accumulation_every_5_days(self):
        # Bill with base rent ₹1500 + food ₹3900 - mess cut ₹130 = ₹5270
        due = date(2026, 11, 10)
        fee = Fee.objects.create(
            student=self.student,
            title="Hostel & Mess Fee - November 2026",
            rent_amount=Decimal("1500.00"),
            food_amount=Decimal("3900.00"),
            mess_cut_amount=Decimal("130.00"),
            base_amount=Decimal("5270.00"),
            amount=Decimal("5270.00"),
            due_date=due,
        )

        # 0 days overdue (on due date): fine is 0
        self.assertEqual(fee.calculate_late_fine(as_of=due), Decimal("0.00"))

        # 4 days overdue: fine is 0
        self.assertEqual(fee.calculate_late_fine(as_of=date(2026, 11, 14)), Decimal("0.00"))

        # 5 days overdue (after 5 days): fine of ₹300 added
        self.assertEqual(fee.calculate_late_fine(as_of=date(2026, 11, 15)), Decimal("300.00"))
        fee.sync_late_fine(as_of=date(2026, 11, 15))
        self.assertEqual(fee.fine_amount, Decimal("300.00"))
        self.assertEqual(fee.amount, Decimal("5570.00"))
        self.assertEqual(fee.balance, Decimal("5570.00"))

        # 9 days overdue: fine remains ₹300
        self.assertEqual(fee.calculate_late_fine(as_of=date(2026, 11, 19)), Decimal("300.00"))

        # 10 days overdue (another 5 days): fine becomes ₹600
        self.assertEqual(fee.calculate_late_fine(as_of=date(2026, 11, 20)), Decimal("600.00"))
        fee.sync_late_fine(as_of=date(2026, 11, 20))
        self.assertEqual(fee.fine_amount, Decimal("600.00"))
        self.assertEqual(fee.amount, Decimal("5870.00"))

        # 15 days overdue: fine becomes ₹900
        self.assertEqual(fee.calculate_late_fine(as_of=date(2026, 11, 25)), Decimal("900.00"))
        fee.sync_late_fine(as_of=date(2026, 11, 25))
        self.assertEqual(fee.fine_amount, Decimal("900.00"))
        self.assertEqual(fee.amount, Decimal("6170.00"))

    def test_warden_can_waive_late_fine(self):
        due = date(2026, 11, 10)
        fee = Fee.objects.create(
            student=self.student,
            title="Hostel & Mess Fee - November 2026",
            base_amount=Decimal("5270.00"),
            amount=Decimal("5270.00"),
            due_date=due,
        )
        # 5 days overdue -> fine is 300
        fee.sync_late_fine(as_of=date(2026, 11, 15))
        self.assertEqual(fee.fine_amount, Decimal("300.00"))
        self.assertEqual(fee.amount, Decimal("5570.00"))

        c = Client()
        c.force_login(self.warden_user)

        # Warden waives fine
        resp = c.post(reverse("fee_waive_fine", args=[fee.pk]))
        self.assertEqual(resp.status_code, 302)

        fee.refresh_from_db()
        self.assertTrue(fee.fine_waived)
        self.assertEqual(fee.fine_amount, Decimal("0.00"))
        self.assertEqual(fee.amount, Decimal("5270.00"))

    def test_payment_with_late_fine(self):
        due = date.today() - timedelta(days=5)
        fee = Fee.objects.create(
            student=self.student,
            title="Hostel & Mess Fee - November 2026",
            base_amount=Decimal("5270.00"),
            amount=Decimal("5270.00"),
            due_date=due,
        )
        fee.sync_late_fine()
        self.assertEqual(fee.fine_amount, Decimal("300.00"))
        self.assertEqual(fee.amount, Decimal("5570.00"))

        c = Client()
        c.force_login(self.warden_user)

        # Payment initial balance includes fine
        get_resp = c.get(reverse("payment_new", args=[fee.pk]))
        self.assertEqual(get_resp.status_code, 200)
        self.assertContains(get_resp, "5570.00")

        # Record payment of 5570
        pay_resp = c.post(reverse("payment_new", args=[fee.pk]), {
            "amount": "5570.00",
            "paid_on": str(date.today()),
            "method": "Bank Transfer",
            "reference": "TXN12345",
        })
        self.assertEqual(pay_resp.status_code, 302)

        fee.refresh_from_db()
        self.assertEqual(fee.balance, Decimal("0.00"))
        self.assertEqual(fee.status, "Paid")

    def test_warden_can_export_fees_pdf_report(self):
        Fee.objects.create(
            student=self.student,
            title="Hostel Fee - November 2026",
            month="2026-11",
            amount=Decimal("5270.00"),
            due_date=date(2026, 12, 10),
        )

        c = Client()
        c.force_login(self.warden_user)

        # 1. Master export PDF
        resp = c.get(reverse("fees_export_pdf"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF"))
        self.assertIn("attachment; filename=", resp["Content-Disposition"])

        # 2. Filtered by month
        resp_m = c.get(reverse("fees_export_pdf") + "?month=2026-11")
        self.assertEqual(resp_m.status_code, 200)
        self.assertTrue(resp_m.content.startswith(b"%PDF"))

        # 3. Reports page PDF export
        resp_rep = c.get(reverse("reports") + "?export=fees_pdf")
        self.assertEqual(resp_rep.status_code, 200)
        self.assertEqual(resp_rep["Content-Type"], "application/pdf")
        self.assertTrue(resp_rep.content.startswith(b"%PDF"))

    def test_student_and_parent_fee_pdf_receipt(self):
        parent_user = User.objects.create_user(username="parent_p1", password="password123", role="parent")
        parent = Parent.objects.create(user=parent_user)
        self.student.parent = parent
        self.student.save()

        fee = Fee.objects.create(
            student=self.student,
            title="Hostel & Mess Fee - November 2026",
            month="2026-11",
            rent_amount=Decimal("1500.00"),
            food_amount=Decimal("3900.00"),
            mess_cut_amount=Decimal("130.00"),
            amount=Decimal("5270.00"),
            due_date=date(2026, 12, 10),
        )

        c = Client()
        # Student 1 logs in and downloads receipt
        c.force_login(self.student_user)
        resp = c.get(reverse("fee_pdf", args=[fee.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF"))
        self.assertIn(f"fee_receipt_{self.student.roll_no}_{fee.pk}.pdf", resp["Content-Disposition"])

        # Student also exports their full statement PDF
        stmt_resp = c.get(reverse("my_fees_export_pdf"))
        self.assertEqual(stmt_resp.status_code, 200)
        self.assertEqual(stmt_resp["Content-Type"], "application/pdf")
        self.assertTrue(stmt_resp.content.startswith(b"%PDF"))

        # Parent logs in and can view my_fees page with ward fee breakdown
        c.force_login(parent_user)
        parent_fee_resp = c.get(reverse("my_fees"))
        self.assertEqual(parent_fee_resp.status_code, 200)
        self.assertContains(parent_fee_resp, "Mess Fee - November 2026")
        self.assertContains(parent_fee_resp, "5270.00")
        self.assertContains(parent_fee_resp, "Receipt")
        self.assertContains(parent_fee_resp, reverse("fee_pdf", args=[fee.pk]))

        # Parent can download child's fee receipt PDF
        p_receipt_resp = c.get(reverse("fee_pdf", args=[fee.pk]))
        self.assertEqual(p_receipt_resp.status_code, 200)
        self.assertEqual(p_receipt_resp["Content-Type"], "application/pdf")
        self.assertTrue(p_receipt_resp.content.startswith(b"%PDF"))

        # Parent can export child's full statement PDF
        p_stmt_resp = c.get(reverse("my_fees_export_pdf"))
        self.assertEqual(p_stmt_resp.status_code, 200)
        self.assertEqual(p_stmt_resp["Content-Type"], "application/pdf")
        self.assertTrue(p_stmt_resp.content.startswith(b"%PDF"))

        # Another student cannot download Student 1's fee receipt
        other_user = User.objects.create_user(username="other_stud", password="p", role="student")
        Student.objects.create(user=other_user, roll_no="CS999")
        c.force_login(other_user)
        blocked_resp = c.get(reverse("fee_pdf", args=[fee.pk]))
        self.assertEqual(blocked_resp.status_code, 403)

        # Another parent cannot download Student 1's fee receipt or full statement
        other_p_user = User.objects.create_user(username="other_par", password="p", role="parent")
        Parent.objects.create(user=other_p_user)
        c.force_login(other_p_user)
        self.assertEqual(c.get(reverse("fee_pdf", args=[fee.pk])).status_code, 403)
        self.assertEqual(c.get(reverse("my_fees_export_pdf")).status_code, 403)

    def test_parent_with_multiple_children_can_switch_fee_views(self):
        parent_user = User.objects.create_user(username="parent_multi", password="password123", role="parent")
        parent = Parent.objects.create(user=parent_user)

        # Child 1: self.student
        self.student.parent = parent
        self.student.save()
        fee1 = Fee.objects.create(
            student=self.student,
            title="Fee Child 1",
            amount=Decimal("5000.00"),
            due_date=date(2026, 12, 10),
        )

        # Child 2:
        u2 = User.objects.create_user(username="child2_user", password="p", role="student", first_name="Rohan", last_name="Sharma")
        s2 = Student.objects.create(user=u2, roll_no="CS102", parent=parent)
        fee2 = Fee.objects.create(
            student=s2,
            title="Fee Child 2",
            amount=Decimal("6000.00"),
            due_date=date(2026, 12, 10),
        )

        c = Client()
        c.force_login(parent_user)

        # Default view shows child 1 with child switcher
        resp_default = c.get(reverse("my_fees"))
        self.assertEqual(resp_default.status_code, 200)
        self.assertContains(resp_default, "Fee Child 1")
        self.assertContains(resp_default, "Select Ward:")
        self.assertContains(resp_default, "Rohan Sharma")

        # Switching to child 2 via ?child=<pk>
        resp_child2 = c.get(reverse("my_fees") + f"?child={s2.pk}")
        self.assertEqual(resp_child2.status_code, 200)
        self.assertContains(resp_child2, "Fee Child 2")
        self.assertContains(resp_child2, "6000.00")

        # Statement export for child 2
        resp_pdf = c.get(reverse("my_fees_export_pdf") + f"?child={s2.pk}")
        self.assertEqual(resp_pdf.status_code, 200)
        self.assertEqual(resp_pdf["Content-Type"], "application/pdf")
        self.assertTrue(resp_pdf.content.startswith(b"%PDF"))


class RoomSectionWardenAdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin_user", password="p", role="admin")
        self.warden = User.objects.create_user(username="warden_user", password="p", role="warden")
        self.student_user = User.objects.create_user(
            username="stud_arjun", password="p", role="student", first_name="Arjun", last_name="Verma"
        )
        self.parent_user = User.objects.create_user(
            username="parent_ramesh", password="p", role="parent", first_name="Ramesh", last_name="Verma"
        )
        self.parent = Parent.objects.create(user=self.parent_user)
        self.student = Student.objects.create(
            user=self.student_user, roll_no="CS301", course="B.Tech Computer Science", parent=self.parent
        )

        # Create rooms on different floors
        self.room_g1 = Room.objects.create(block="G", number="101", capacity=2)
        self.room_g2 = Room.objects.create(block="G", number="102", capacity=2)
        self.room_f1 = Room.objects.create(block="F", number="201", capacity=2)

        # Allocate Arjun to G-101
        self.alloc = RoomAllocation.objects.create(
            student=self.student, room=self.room_g1, start_date=date(2026, 1, 1)
        )

        # Create active complaint from Arjun
        self.active_comp = Complaint.objects.create(
            student=self.student,
            title="Fan regulator broken",
            description="Speed control is stuck at 5 in room G-101.",
            status="Under Review",
        )

        # Create resolved complaint
        self.resolved_comp = Complaint.objects.create(
            student=self.student,
            title="Tube light blinking",
            description="Tube light on bed 1 blinks continuously.",
            status="Resolved",
            staff_note="Replaced choke and starter.",
        )

    def test_warden_room_list_floor_filter_and_grid(self):
        c = Client()
        c.force_login(self.warden)

        # 1. Main Rooms section (grid view)
        resp = c.get(reverse("rooms"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Hostel Rooms Directory")
        self.assertContains(resp, "G-101")
        self.assertContains(resp, "F-201")
        self.assertContains(resp, "Ground Floor")
        self.assertContains(resp, "Arjun Verma")

        # 2. Filter by floor G
        resp_g = c.get(reverse("rooms") + "?block=G")
        self.assertEqual(resp_g.status_code, 200)
        self.assertContains(resp_g, "G-101")
        self.assertNotContains(resp_g, "F-201")

        # 3. Table view
        resp_tbl = c.get(reverse("rooms") + "?view=table")
        self.assertEqual(resp_tbl.status_code, 200)
        self.assertContains(resp_tbl, "Table & Bulk Select")

    def test_warden_room_detail_inmates_and_complaints(self):
        c = Client()
        c.force_login(self.warden)

        # Visit room detail for G-101
        resp = c.get(reverse("room_detail", args=[self.room_g1.pk]))
        self.assertEqual(resp.status_code, 200)

        # Verify Room info
        self.assertContains(resp, "Room G-101")
        self.assertContains(resp, "Ground Floor")

        # Verify Inmate info with clickable profile link
        self.assertContains(resp, "Arjun Verma")
        self.assertContains(resp, reverse("student_detail", args=[self.student.pk]))
        self.assertContains(resp, "CS301")
        self.assertContains(resp, "Ramesh Verma")

        # Verify Active Complaint with clickable link to manage complaint
        self.assertContains(resp, "Fan regulator broken")
        self.assertContains(resp, reverse("complaints_edit", args=[self.active_comp.pk]))

        # Verify Resolved Complaint history
        self.assertContains(resp, "Tube light blinking")
        self.assertContains(resp, "Replaced choke and starter")

        # Verify Vacant bed slot for bed 2
        self.assertContains(resp, "Bed Slot Available")
        self.assertContains(resp, reverse("allocations_new") + f"?room={self.room_g1.pk}")

    def test_warden_student_detail_profile(self):
        c = Client()
        c.force_login(self.warden)

        resp = c.get(reverse("student_detail", args=[self.student.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Arjun Verma")
        self.assertContains(resp, "CS301")
        self.assertContains(resp, "Room G-101")
        self.assertContains(resp, reverse("room_detail", args=[self.room_g1.pk]))

    def test_student_cannot_access_staff_room_and_student_details(self):
        c = Client()
        c.force_login(self.student_user)

        # Student cannot access /rooms/<pk>/
        resp = c.get(reverse("room_detail", args=[self.room_g1.pk]))
        self.assertEqual(resp.status_code, 403)

        # Student cannot access /students/<pk>/
        resp2 = c.get(reverse("student_detail", args=[self.student.pk]))
        self.assertEqual(resp2.status_code, 403)


class RoomAnnouncementAndFloorFilterTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_anc", password="AdminPassword123", role="admin", is_staff=True
        )
        self.warden = User.objects.create_user(
            username="warden_anc", password="WardenPassword123", role="warden", is_staff=True
        )

        self.room_g1 = Room.objects.create(block="G", number="101", capacity=2)
        self.room_g2 = Room.objects.create(block="G", number="102", capacity=2)
        self.room_f1 = Room.objects.create(block="F", number="101", capacity=2)
        self.room_f2 = Room.objects.create(block="F", number="102", capacity=2)

        self.student_g1 = onboard_student(name="Gaurav Sharma", roll_no="G101", room=self.room_g1)
        self.student_g1.user.must_change_password = False
        self.student_g1.user.save()
        self.student_f1 = onboard_student(name="Farhan Khan", roll_no="F101", room=self.room_f1)
        self.student_f1.user.must_change_password = False
        self.student_f1.user.save()

    def test_warden_can_post_and_view_room_announcement(self):
        c = Client()
        c.force_login(self.warden)

        # 1. Post an announcement to Room G-101
        resp = c.post(
            reverse("room_announcement_create", args=[self.room_g1.pk]),
            {
                "title": "Room G-101 Cleanliness Inspection",
                "body": "Inspection scheduled tomorrow morning at 10:00 AM.",
                "audience": "student",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Announcement successfully posted to Room G-101 inmates!")

        # 2. Verify announcement appears in Room G-101 detail
        self.assertContains(resp, "Room G-101 Cleanliness Inspection")
        self.assertContains(resp, "Inspection scheduled tomorrow morning at 10:00 AM.")
        self.assertContains(resp, f"Posted by <b>{self.warden.display_name}</b>")

        # 3. Verify database record
        anc = Announcement.objects.filter(room=self.room_g1).first()
        self.assertIsNotNone(anc)
        self.assertEqual(anc.title, "Room G-101 Cleanliness Inspection")
        self.assertEqual(anc.created_by, self.warden)

    def test_room_announcement_visible_to_inmate_and_isolated_from_other_rooms(self):
        # Create an announcement targeted to Room G-101
        Announcement.objects.create(
            room=self.room_g1,
            title="Keep G-101 Windows Closed Tonight",
            body="Rainstorm warning for Ground floor.",
            audience="student",
            created_by=self.warden,
        )
        # Create a general broadcast announcement
        Announcement.objects.create(
            room=None,
            title="General Hostel Library Timings",
            body="Library open until 11 PM.",
            audience="all",
            created_by=self.warden,
        )

        c = Client()

        # Inmate of G-101 logs in:
        c.force_login(self.student_g1.user)
        resp_g1 = c.get(reverse("home"))
        self.assertEqual(resp_g1.status_code, 200)
        # Must see both the room-specific notice and the general notice
        self.assertContains(resp_g1, "Keep G-101 Windows Closed Tonight")
        self.assertContains(resp_g1, "Room G-101 Notice")
        self.assertContains(resp_g1, "General Hostel Library Timings")
        self.assertContains(resp_g1, "Hostel Notice")

        # Inmate of F-101 logs in:
        c.force_login(self.student_f1.user)
        resp_f1 = c.get(reverse("home"))
        self.assertEqual(resp_f1.status_code, 200)
        # Must see the general notice
        self.assertContains(resp_f1, "General Hostel Library Timings")
        # Must NOT see Room G-101's private notice
        self.assertNotContains(resp_f1, "Keep G-101 Windows Closed Tonight")

    def test_warden_can_delete_room_announcement(self):
        c = Client()
        c.force_login(self.warden)

        anc = Announcement.objects.create(
            room=self.room_g1,
            title="Temporary Notice to Delete",
            body="This will be deleted.",
            audience="student",
            created_by=self.warden,
        )

        resp = c.post(reverse("room_announcement_delete", args=[anc.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Room announcement removed.")
        self.assertFalse(Announcement.objects.filter(pk=anc.pk).exists())

    def test_floor_pills_have_no_duplicates_in_views(self):
        c = Client()
        c.force_login(self.warden)

        # In rooms directory (staff)
        resp = c.get(reverse("rooms"))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        # Ensure only 1 button pill for Ground Floor (G) and 1 for First Floor (F)
        # Check by counting block filter links with block=G and block=F
        self.assertEqual(content.count("&block=G"), 1)
        self.assertEqual(content.count("&block=F"), 1)

        # In student rooms directory
        resp2 = c.get(reverse("student_rooms"))
        self.assertEqual(resp2.status_code, 200)
        content2 = resp2.content.decode("utf-8")
        self.assertEqual(content2.count("?block=G"), 1)
        self.assertEqual(content2.count("?block=F"), 1)


class NotificationSystemTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_notif", password="AdminPassword123", role="admin", is_staff=True
        )
        self.warden = User.objects.create_user(
            username="warden_notif", password="WardenPassword123", role="warden", is_staff=True
        )

        self.room = Room.objects.create(block="G", number="201", capacity=2)
        self.student = onboard_student(name="Rahul Sharma", roll_no="RS201", room=self.room)
        self.student.user.must_change_password = False
        self.student.user.save()

    def test_notification_bell_rendered_in_topbar(self):
        Notification.objects.create(
            recipient=self.student.user,
            title="Important Mess Timing Change",
            message="Dinner will be served from 7:00 PM tonight.",
            category="announcement",
            link="/",
        )

        c = Client()
        c.force_login(self.student.user)

        resp = c.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "notifDropdown")
        self.assertContains(resp, "Important Mess Timing Change")
        self.assertContains(resp, "unread notifications")

    def test_announcements_trigger_notifications(self):
        c = Client()
        c.force_login(self.warden)

        # 1. General announcement
        resp = c.post(
            reverse("announcements_new"),
            {
                "title": "Annual Hostel Day",
                "body": "Hostel celebration will be held this Saturday evening.",
                "audience": "all",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.student.user,
                category="announcement",
                title__contains="Annual Hostel Day",
            ).exists()
        )

        # 2. Room-specific announcement
        resp2 = c.post(
            reverse("room_announcement_create", args=[self.room.pk]),
            {
                "title": "Room G-201 Balcony Inspection",
                "body": "Please keep the balcony clean for inspection.",
                "audience": "student",
            },
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.student.user,
                category="announcement",
                title__contains="Room G-201",
            ).exists()
        )

    def test_fee_actions_trigger_notifications(self):
        c = Client()
        c.force_login(self.warden)

        # 1. Manual Fee Creation
        resp = c.post(
            reverse("fees_new"),
            {
                "student": self.student.pk,
                "title": "Hostel Maintenance Fee",
                "amount": "1500.00",
                "due_date": "2026-12-10",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.student.user,
                category="fee",
                title__contains="Hostel Maintenance Fee",
            ).exists()
        )

        # 2. Monthly Fee Calculation Release
        resp2 = c.post(
            reverse("fees_calculate_monthly"),
            {
                "save_fees": "1",
                "selected_students": [str(self.student.pk)],
                "due_date": "2026-12-15",
                "title": "Monthly Mess & Room Rent - November 2026",
                f"rent_{self.student.pk}": "1500.00",
                f"food_{self.student.pk}": "3900.00",
                f"mess_days_{self.student.pk}": "0",
                f"mess_cut_{self.student.pk}": "0.00",
                f"total_{self.student.pk}": "5400.00",
            },
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.student.user,
                category="fee",
                message__contains="Monthly Mess & Room Rent",
            ).exists()
        )

    def test_complaint_workflow_triggers_notifications(self):
        c_student = Client()
        c_student.force_login(self.student.user)

        # 1. Student submits complaint
        resp = c_student.post(
            reverse("my_complaints"),
            {
                "title": "Water heater not heating",
                "description": "Geyser in bathroom G-201 is cold.",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)

        comp = Complaint.objects.filter(student=self.student, title="Water heater not heating").first()
        self.assertIsNotNone(comp)

        # Staff (warden) must have received notification
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.warden,
                category="complaint",
                title__contains="Water heater not heating",
            ).exists()
        )

        # 2. Staff updates complaint and adds a reply note
        c_staff = Client()
        c_staff.force_login(self.warden)

        resp2 = c_staff.post(
            reverse("complaints_edit", args=[comp.pk]),
            {
                "status": "In Progress",
                "staff_note": "Plumber scheduled to visit at 3 PM.",
            },
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)

        # Student must have received notification with the reply note
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.student.user,
                category="complaint",
                message__contains="Plumber scheduled to visit",
            ).exists()
        )

        # 3. Student marks complaint as solved
        resp3 = c_student.post(reverse("my_complaint_resolve", args=[comp.pk]), follow=True)
        self.assertEqual(resp3.status_code, 200)

        # Staff notified that student resolved the issue
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.warden,
                category="complaint",
                title__contains="Complaint Solved",
            ).exists()
        )

    def test_notifications_page_read_and_delete(self):
        n1 = Notification.objects.create(
            recipient=self.student.user,
            title="Notice 1",
            message="Notice 1 body",
            link="/me/fees/",
        )
        n2 = Notification.objects.create(
            recipient=self.student.user,
            title="Notice 2",
            message="Notice 2 body",
            link="",
        )

        c = Client()
        c.force_login(self.student.user)

        # 1. Notifications list page
        resp = c.get(reverse("notifications"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Notice 1")
        self.assertContains(resp, "Notice 2")

        # 2. Open notification n1 (marks read and redirects to link)
        read_resp = c.get(reverse("notification_read", args=[n1.pk]))
        self.assertRedirects(read_resp, "/me/fees/")
        n1.refresh_from_db()
        self.assertTrue(n1.is_read)

        # 3. Mark all read
        all_read_resp = c.post(reverse("notifications_mark_all_read"), follow=True)
        self.assertEqual(all_read_resp.status_code, 200)
        n2.refresh_from_db()
        self.assertTrue(n2.is_read)

        # 4. Delete notification
        del_resp = c.post(reverse("notification_delete", args=[n2.pk]), follow=True)
        self.assertEqual(del_resp.status_code, 200)
        self.assertFalse(Notification.objects.filter(pk=n2.pk).exists())


class StudentAnnouncementSectionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_anc_sec", password="AdminPassword123", role="admin", is_staff=True
        )
        self.warden = User.objects.create_user(
            username="warden_anc_sec", password="WardenPassword123", role="warden", is_staff=True
        )

        self.room_g1 = Room.objects.create(block="G", number="101", capacity=2)
        self.room_f1 = Room.objects.create(block="F", number="101", capacity=2)

        self.student_g1 = onboard_student(
            name="Vikram Seth", roll_no="VS101", room=self.room_g1, parent_name="Ramesh Seth", parent_phone="9876543210"
        )
        self.student_g1.refresh_from_db()
        self.student_g1.user.must_change_password = False
        self.student_g1.user.save()
        if self.student_g1.parent:
            self.student_g1.parent.user.must_change_password = False
            self.student_g1.parent.user.save()

        self.student_f1 = onboard_student(name="Ananya Roy", roll_no="AR101", room=self.room_f1)
        self.student_f1.user.must_change_password = False
        self.student_f1.user.save()

        # 1. General announcement
        self.gen_anc = Announcement.objects.create(
            room=None,
            title="Hostel Night Dinner Timings",
            body="Dinner on Sunday will start at 8:00 PM.",
            audience="all",
            created_by=self.warden,
        )

        # 2. Room G-101 specific announcement
        self.room_g1_anc = Announcement.objects.create(
            room=self.room_g1,
            title="Room G-101 Deep Cleaning Schedule",
            body="Cleaning staff will visit G-101 on Wednesday morning at 10 AM.",
            audience="student",
            created_by=self.warden,
        )

        # 3. Room F-101 specific announcement
        self.room_f1_anc = Announcement.objects.create(
            room=self.room_f1,
            title="Room F-101 AC Maintenance",
            body="Technician servicing AC unit in F-101.",
            audience="student",
            created_by=self.warden,
        )

    def test_student_sidebar_contains_announcements_link(self):
        c = Client()
        c.force_login(self.student_g1.user)

        resp = c.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("my_announcements"))
        self.assertContains(resp, "Announcements")

    def test_student_announcements_page_view_and_room_isolation(self):
        c = Client()
        c.force_login(self.student_g1.user)

        resp = c.get(reverse("my_announcements"))
        self.assertEqual(resp.status_code, 200)

        # Inmate of G-101 must see general broadcast and G-101 notice
        self.assertContains(resp, "Hostel Night Dinner Timings")
        self.assertContains(resp, "Room G-101 Deep Cleaning Schedule")
        self.assertContains(resp, "Room G-101 Notice")

        # Inmate of G-101 must NOT see F-101's private announcement
        self.assertNotContains(resp, "Room F-101 AC Maintenance")

    def test_student_announcements_filtering_and_search(self):
        c = Client()
        c.force_login(self.student_g1.user)

        # 1. Filter by room notices
        resp_room = c.get(reverse("my_announcements") + "?type=room")
        self.assertEqual(resp_room.status_code, 200)
        self.assertContains(resp_room, "Room G-101 Deep Cleaning Schedule")
        self.assertNotContains(resp_room, "Hostel Night Dinner Timings")

        # 2. Filter by general circulars
        resp_gen = c.get(reverse("my_announcements") + "?type=general")
        self.assertEqual(resp_gen.status_code, 200)
        self.assertContains(resp_gen, "Hostel Night Dinner Timings")
        self.assertNotContains(resp_gen, "Room G-101 Deep Cleaning Schedule")

        # 3. Search matching
        resp_search = c.get(reverse("my_announcements") + "?q=Dinner")
        self.assertEqual(resp_search.status_code, 200)
        self.assertContains(resp_search, "Hostel Night Dinner Timings")
        self.assertNotContains(resp_search, "Room G-101 Deep Cleaning Schedule")

        # 4. Search no match
        resp_nomatch = c.get(reverse("my_announcements") + "?q=NonExistentKeywordXYZ")
        self.assertEqual(resp_nomatch.status_code, 200)
        self.assertContains(resp_nomatch, "No Announcements Found")

    def test_parent_can_view_announcements_for_child_room(self):
        c = Client()
        c.force_login(self.student_g1.parent.user)

        resp = c.get(reverse("my_announcements"))
        self.assertEqual(resp.status_code, 200)
        # Parent sees general broadcast
        self.assertContains(resp, "Hostel Night Dinner Timings")


class WeeklyFoodMenuTableTests(TestCase):
    def setUp(self):
        self.room = Room.objects.create(block="G", number="301", capacity=2)
        self.student = onboard_student(name="Divya Nair", roll_no="DN301", room=self.room)
        self.student.user.must_change_password = False
        self.student.user.save()

        # Seed menu items for Monday
        MealMenu.objects.create(day=0, meal="B", items="Idli, Sambar, Coconut Chutney, Tea")
        MealMenu.objects.create(day=0, meal="L", items="Rice, Dal Tadka, Paneer Butter Masala, Curd")
        MealMenu.objects.create(day=0, meal="S", items="Samosa, Ginger Tea")
        MealMenu.objects.create(day=0, meal="D", items="Chapati, Mixed Veg Curry, Gulab Jamun")

    def test_student_can_view_weekly_food_menu_table(self):
        c = Client()
        c.force_login(self.student.user)

        resp = c.get(reverse("menu"))
        self.assertEqual(resp.status_code, 200)

        # Ensure table structure is present
        self.assertContains(resp, "<table")
        self.assertContains(resp, "Weekly Hostel Mess & Food Menu")
        self.assertContains(resp, "Full Weekly Meal Timetable")
        self.assertContains(resp, "Breakfast")
        self.assertContains(resp, "Lunch")
        self.assertContains(resp, "Snacks")
        self.assertContains(resp, "Dinner")

        # Ensure food items appear inside the table
        self.assertContains(resp, "Idli")
        self.assertContains(resp, "Sambar")
        self.assertContains(resp, "Paneer Butter Masala")
        self.assertContains(resp, "Samosa")
        self.assertContains(resp, "Gulab Jamun")

        # Ensure today is highlighted
        self.assertContains(resp, "TODAY")

    def test_anonymous_redirected_from_menu(self):
        c = Client()
        resp = c.get(reverse("menu"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.url)


class AdminSuspensionProcedureTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="super_admin", password="password123", role="admin", is_staff=True, first_name="Super", last_name="Admin"
        )
        self.warden = User.objects.create_user(
            username="hostel_warden", password="password123", role="warden", is_staff=True, first_name="Hostel", last_name="Warden"
        )
        self.parent_user = User.objects.create_user(
            username="parent_user", password="password123", role="parent", first_name="Parent", last_name="Kumar"
        )
        self.parent = Parent.objects.create(user=self.parent_user)
        self.student_user = User.objects.create_user(
            username="stud_susp", password="password123", role="student", first_name="Rohan", last_name="Kumar"
        )
        self.student = Student.objects.create(
            user=self.student_user, roll_no="SUSP001", course="B.Tech", parent=self.parent, is_active=True
        )
        self.room = Room.objects.create(block="A", number="201", capacity=2)
        RoomAllocation.objects.create(student=self.student, room=self.room, start_date=date(2026, 1, 1))

    def test_admin_can_suspend_student_and_notifies_stakeholders(self):
        c = Client()
        c.force_login(self.admin)

        resp = c.post(
            reverse("student_suspend", args=[self.student.pk]),
            {
                "reason": "Hostel curfew violation and unauthorized damage to property.",
                "start_date": str(date.today()),
                "end_date": str(date.today() + timedelta(days=14)),
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)

        self.student.refresh_from_db()
        self.assertTrue(self.student.is_suspended)
        self.assertTrue(self.student.is_currently_suspended)
        self.assertEqual(self.student.suspension_reason, "Hostel curfew violation and unauthorized damage to property.")
        self.assertEqual(self.student.suspended_by, self.admin)

        # Student notification
        s_notif = Notification.objects.filter(recipient=self.student_user).first()
        self.assertIsNotNone(s_notif)
        self.assertIn("Suspended", s_notif.title)
        self.assertIn("curfew violation", s_notif.message)

        # Parent notification
        p_notif = Notification.objects.filter(recipient=self.parent_user).first()
        self.assertIsNotNone(p_notif)
        self.assertIn("Ward Suspended", p_notif.title)
        self.assertIn("Rohan Kumar", p_notif.message)

        # Announcement created for students
        anc = Announcement.objects.filter(student=self.student, audience="student").first()
        self.assertIsNotNone(anc)
        self.assertIn("Suspension Notice", anc.title)
        self.assertIn("curfew violation", anc.body)

        # Profile displays suspension banner and lift suspension button for Admin
        prof_resp = c.get(reverse("student_detail", args=[self.student.pk]))
        self.assertEqual(prof_resp.status_code, 200)
        self.assertContains(prof_resp, "HOSTEL ENTRY BARRED")
        self.assertContains(prof_resp, "Lift Suspension")

    def test_warden_cannot_suspend_or_reinstate_student(self):
        c = Client()
        c.force_login(self.warden)

        # Warden tries to suspend -> 403 Forbidden
        suspend_resp = c.post(
            reverse("student_suspend", args=[self.student.pk]),
            {"reason": "Attempt by warden"},
        )
        self.assertEqual(suspend_resp.status_code, 403)
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_suspended)

        # If student is suspended, warden tries to reinstate -> 403 Forbidden
        self.student.is_suspended = True
        self.student.save()
        reinstate_resp = c.post(reverse("student_reinstate", args=[self.student.pk]))
        self.assertEqual(reinstate_resp.status_code, 403)

    def test_suspended_student_cannot_be_marked_present_in_attendance(self):
        # Admin suspends student
        self.student.is_suspended = True
        self.student.suspension_reason = "Misconduct"
        self.student.save()

        c = Client()
        c.force_login(self.warden)

        # Check attendance page renders Barred badge for student
        today = date.today()
        get_att = c.get(reverse("attendance") + f"?date={today.isoformat()}")
        self.assertEqual(get_att.status_code, 200)
        self.assertContains(get_att, "Suspended (Entry Barred)")
        self.assertContains(get_att, "Barred")

        # Staff attempts to submit "P" (Present) for the suspended student
        post_att = c.post(
            reverse("attendance"),
            {
                "date": today.isoformat(),
                f"s{self.student.pk}": "P",
            },
            follow=True,
        )
        self.assertEqual(post_att.status_code, 200)

        # System enforces restriction: recorded as "A" (Absent) rather than "P"
        att = Attendance.objects.filter(student=self.student, date=today).first()
        self.assertIsNotNone(att)
        self.assertEqual(att.status, "A")
        self.assertContains(post_att, "barred from hostel entry")

    def test_suspended_student_barred_from_submitting_leaves(self):
        self.student.is_suspended = True
        self.student.suspension_reason = "Suspension rule test"
        self.student.save()

        c = Client()
        c.force_login(self.student_user)

        # Student accesses leave request page
        leave_resp = c.get(reverse("my_leaves"))
        self.assertEqual(leave_resp.status_code, 200)
        self.assertContains(leave_resp, "hostel resident access is currently suspended")

        # Student attempts to post a leave request
        post_leave = c.post(
            reverse("my_leaves"),
            {
                "from_date": str(date.today()),
                "to_date": str(date.today() + timedelta(days=2)),
                "reason": "Going home",
            },
        )
        # Should not create any leave request
        self.assertEqual(LeaveRequest.objects.filter(student=self.student).count(), 0)

    def test_admin_can_reinstate_student_and_lifts_restriction(self):
        self.student.is_suspended = True
        self.student.suspension_reason = "Disciplinary action"
        self.student.save()

        c = Client()
        c.force_login(self.admin)

        resp = c.post(reverse("student_reinstate", args=[self.student.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)

        self.student.refresh_from_db()
        self.assertFalse(self.student.is_suspended)
        self.assertFalse(self.student.is_currently_suspended)
        self.assertEqual(self.student.suspension_reason, "")

        # Notifications sent
        s_notif = Notification.objects.filter(recipient=self.student_user, title__contains="Suspension Lifted").first()
        self.assertIsNotNone(s_notif)

        p_notif = Notification.objects.filter(recipient=self.parent_user, title__contains="Suspension Lifted").first()
        self.assertIsNotNone(p_notif)

    def test_parent_sees_suspension_status_in_portal(self):
        self.student.is_suspended = True
        self.student.suspension_reason = "Disciplinary violation"
        self.student.save()

        c = Client()
        c.force_login(self.parent_user)

        # Single child: home redirects to child overview dashboard
        resp = c.get(reverse("home"), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "HOSTEL ENTRY BARRED")
        self.assertContains(resp, "Resident Access Suspended")

        # Multi-children: parent sees children list with suspension badge
        u2 = User.objects.create_user(username="second_c", password="p", role="student", first_name="Aman", last_name="Kumar")
        Student.objects.create(user=u2, roll_no="SUSP002", parent=self.parent)
        resp_multi = c.get(reverse("home"))
        self.assertEqual(resp_multi.status_code, 200)
        self.assertContains(resp_multi, "Suspended")
        self.assertContains(resp_multi, "Hostel Entry Barred")

    def test_students_list_renders_properly_with_suspension_badge(self):
        c = Client()
        c.force_login(self.admin)

        # Active student
        resp = c.get(reverse("students"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Staying")

        # Suspended student
        self.student.is_suspended = True
        self.student.suspension_reason = "Disciplinary violation"
        self.student.save()

        resp2 = c.get(reverse("students"))
        self.assertEqual(resp2.status_code, 200)
        self.assertContains(resp2, "Suspended")

    def test_students_list_filtering_by_floor_and_allocation(self):
        c = Client()
        c.force_login(self.admin)

        # Create unallocated student
        u_unalloc = User.objects.create_user(username="unalloc_std", password="p", role="student", first_name="Unalloc", last_name="Student")
        unalloc_student = Student.objects.create(user=u_unalloc, roll_no="UNALLOC01")

        # Test filter by allocation: allocated (self.student has room from setUp)
        resp_alloc = c.get(reverse("students") + "?allocation=allocated")
        self.assertEqual(resp_alloc.status_code, 200)
        self.assertContains(resp_alloc, self.student.roll_no)
        self.assertNotContains(resp_alloc, unalloc_student.roll_no)

        # Test filter by allocation: unallocated
        resp_unalloc = c.get(reverse("students") + "?allocation=unallocated")
        self.assertEqual(resp_unalloc.status_code, 200)
        self.assertContains(resp_unalloc, unalloc_student.roll_no)
        self.assertNotContains(resp_unalloc, self.student.roll_no)

        # Test filter by floor
        resp_floor = c.get(reverse("students") + f"?floor={self.room.block}")
        self.assertEqual(resp_floor.status_code, 200)
        self.assertContains(resp_floor, self.student.roll_no)
        self.assertNotContains(resp_floor, unalloc_student.roll_no)

    def test_warden_can_allocate_unallocated_student_from_profile_page(self):
        c = Client()
        c.force_login(self.warden)

        # Create unallocated student and vacant room
        u_target = User.objects.create_user(username="unalloc_target", password="p", role="student", first_name="Tarun", last_name="Verma")
        target_student = Student.objects.create(user=u_target, roll_no="UNALLOC99", parent=self.parent, course="MCA")
        vacant_room = Room.objects.create(block="B", number="105", capacity=2)

        self.assertIsNone(target_student.room)

        # GET student overview as warden
        resp = c.get(reverse("student_detail", args=[target_student.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "allocateModal")
        self.assertContains(resp, "Allocate Room")

        # POST allocate to vacant_room
        post_resp = c.post(
            reverse("student_allocate", args=[target_student.pk]),
            {"room_id": str(vacant_room.pk), "start_date": str(date.today())},
            follow=True,
        )
        self.assertEqual(post_resp.status_code, 200)

        # Verify allocation created
        target_student.refresh_from_db()
        self.assertEqual(target_student.room, vacant_room)

        # Notification sent to student and parent
        notif = Notification.objects.filter(recipient=u_target, title="Hostel Room Allocated").first()
        self.assertIsNotNone(notif)
        self.assertIn(str(vacant_room), notif.message)

        p_notif = Notification.objects.filter(recipient=self.parent_user, title="Ward Room Allocated").first()
        self.assertIsNotNone(p_notif)

        # GET overview now shows room
        resp_after = c.get(reverse("student_detail", args=[target_student.pk]))
        self.assertEqual(resp_after.status_code, 200)
        self.assertContains(resp_after, f"Room {vacant_room}")
        self.assertNotContains(resp_after, "allocateModal")

    def test_suspension_announcement_visibility_for_students_and_parents(self):
        # Admin suspends student with specific description "Suspended due to ragging"
        c = Client()
        c.force_login(self.admin)
        start_d = date.today()
        end_d = date.today() + timedelta(days=7)
        resp = c.post(
            reverse("student_suspend", args=[self.student.pk]),
            {
                "reason": "Suspended due to ragging",
                "start_date": str(start_d),
                "end_date": str(end_d),
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)

        # Announcement exists with details
        anc = Announcement.objects.filter(student=self.student).first()
        self.assertIsNotNone(anc)
        self.assertEqual(anc.audience, "student")
        self.assertIn("Suspended due to ragging", anc.body)
        self.assertIn(start_d.strftime("%d %b %Y"), anc.body)
        self.assertIn(end_d.strftime("%d %b %Y"), anc.body)

        # 1. Other student logs in: SEES the announcement
        other_u = User.objects.create_user(username="fellow_std", password="p", role="student", first_name="Kiran", last_name="Raj")
        Student.objects.create(user=other_u, roll_no="FELLOW01")
        c_std = Client()
        c_std.force_login(other_u)
        resp_std = c_std.get(reverse("my_announcements"))
        self.assertEqual(resp_std.status_code, 200)
        self.assertContains(resp_std, "Suspended due to ragging")
        self.assertContains(resp_std, self.student.user.display_name)

        # 2. Suspended student's parent logs in: SEES the announcement
        c_parent = Client()
        c_parent.force_login(self.parent_user)
        resp_par = c_parent.get(reverse("my_announcements"))
        self.assertEqual(resp_par.status_code, 200)
        self.assertContains(resp_par, "Suspended due to ragging")

        # In child overview, suspended student's parent also sees the notice
        resp_child = c_parent.get(reverse("child", args=[self.student.pk]))
        self.assertEqual(resp_child.status_code, 200)
        self.assertContains(resp_child, "Suspended due to ragging")

        # 3. Another student's parent logs in: DOES NOT see the announcement
        other_p_user = User.objects.create_user(username="other_parent", password="p", role="parent", first_name="Other", last_name="Parent")
        other_parent = Parent.objects.create(user=other_p_user)
        u_kid = User.objects.create_user(username="kid_std", password="p", role="student", first_name="Kid", last_name="P")
        kid = Student.objects.create(user=u_kid, roll_no="KID01", parent=other_parent)

        c_other_p = Client()
        c_other_p.force_login(other_p_user)
        resp_other_p = c_other_p.get(reverse("my_announcements"))
        self.assertEqual(resp_other_p.status_code, 200)
        self.assertNotContains(resp_other_p, "Suspended due to ragging")

        # In kid's overview, other parent DOES NOT see the suspension announcement
        resp_kid_overview = c_other_p.get(reverse("child", args=[kid.pk]))
        self.assertEqual(resp_kid_overview.status_code, 200)
        self.assertNotContains(resp_kid_overview, "Suspended due to ragging")

        # 4. Lift suspension: Announcement is removed
        c.post(reverse("student_reinstate", args=[self.student.pk]), follow=True)
        self.assertEqual(Announcement.objects.filter(student=self.student).count(), 0)


class StudentSelectionAndBulkActionsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="sel_admin", password="p", role="admin", is_staff=True, first_name="Sel", last_name="Admin"
        )
        self.warden = User.objects.create_user(
            username="sel_warden", password="p", role="warden", is_staff=True, first_name="Sel", last_name="Warden"
        )
        self.parent_user = User.objects.create_user(
            username="sel_parent", password="p", role="parent", first_name="Sel", last_name="Parent"
        )
        self.parent = Parent.objects.create(user=self.parent_user)

        self.u1 = User.objects.create_user(username="sel_s1", password="p", role="student", first_name="Asha", last_name="Nair")
        self.s1 = Student.objects.create(user=self.u1, roll_no="SEL001", course="MCA", parent=self.parent)

        self.u2 = User.objects.create_user(username="sel_s2", password="p", role="student", first_name="Binu", last_name="Mathew")
        self.s2 = Student.objects.create(user=self.u2, roll_no="SEL002", course="MCA")

        self.room = Room.objects.create(block="G", number="101", capacity=3)

    def test_students_page_renders_select_and_select_all_controls(self):
        c = Client()
        c.force_login(self.warden)

        # Grid view
        resp_grid = c.get(reverse("students"))
        self.assertEqual(resp_grid.status_code, 200)
        self.assertContains(resp_grid, 'id="selectToggleBtn"')
        self.assertContains(resp_grid, 'id="selectAllBtn"')
        self.assertContains(resp_grid, "Select All")
        self.assertContains(resp_grid, f'value="{self.s1.pk}"')
        self.assertContains(resp_grid, f'value="{self.s2.pk}"')
        self.assertNotContains(resp_grid, '<option value="suspend">')

        # Table view also renders Select and Select All
        resp_table = c.get(reverse("students") + "?view=table")
        self.assertEqual(resp_table.status_code, 200)
        self.assertContains(resp_table, 'id="selectToggleBtn"')
        self.assertContains(resp_table, 'id="selectAllBtn"')
        self.assertContains(resp_table, f'id="student-cb-{self.s1.pk}"')

        # Admin sees suspend options
        c.force_login(self.admin)
        resp_admin = c.get(reverse("students"))
        self.assertEqual(resp_admin.status_code, 200)
        self.assertContains(resp_admin, '<option value="suspend">')

    def test_bulk_allocate_vacate_and_notify_selected_students(self):
        c = Client()
        c.force_login(self.warden)

        # Allocate both unallocated students to self.room
        resp_alloc = c.post(
            reverse("students"),
            {
                "selected_students": [str(self.s1.pk), str(self.s2.pk)],
                "bulk_action": "allocate_room",
                "target_room_id": str(self.room.pk),
            },
            follow=True,
        )
        self.assertEqual(resp_alloc.status_code, 200)
        self.s1.refresh_from_db()
        self.s2.refresh_from_db()
        self.assertEqual(self.s1.room, self.room)
        self.assertEqual(self.s2.room, self.room)

        # Bulk notify with parents included
        resp_notify = c.post(
            reverse("students"),
            {
                "selected_students": [str(self.s1.pk), str(self.s2.pk)],
                "bulk_action": "notify",
                "notify_title": "Inspection",
                "notify_message": "Room inspection at 6 PM",
                "notify_parents": "1",
            },
            follow=True,
        )
        self.assertEqual(resp_notify.status_code, 200)
        self.assertTrue(Notification.objects.filter(recipient=self.u1, title="Inspection").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.u2, title="Inspection").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.parent_user, title="Inspection").exists())

        # Bulk vacate active rooms
        resp_vacate = c.post(
            reverse("students"),
            {
                "selected_students": [str(self.s1.pk), str(self.s2.pk)],
                "bulk_action": "vacate",
            },
            follow=True,
        )
        self.assertEqual(resp_vacate.status_code, 200)
        self.assertIsNone(self.s1.room)
        self.assertIsNone(self.s2.room)

    def test_bulk_suspend_restricted_to_admin_and_bulk_delete_cascades(self):
        c = Client()
        c.force_login(self.warden)

        # Warden cannot bulk suspend -> 403
        resp_warden = c.post(
            reverse("students"),
            {
                "selected_students": [str(self.s1.pk)],
                "bulk_action": "suspend",
                "suspend_reason": "Unauthorized attempt",
            },
        )
        self.assertEqual(resp_warden.status_code, 403)
        self.s1.refresh_from_db()
        self.assertFalse(self.s1.is_suspended)

        # Admin can bulk suspend and reinstate
        c.force_login(self.admin)
        resp_susp = c.post(
            reverse("students"),
            {
                "selected_students": [str(self.s1.pk), str(self.s2.pk)],
                "bulk_action": "suspend",
                "suspend_reason": "Group curfew violation",
            },
            follow=True,
        )
        self.assertEqual(resp_susp.status_code, 200)
        self.s1.refresh_from_db()
        self.s2.refresh_from_db()
        self.assertTrue(self.s1.is_currently_suspended)
        self.assertTrue(self.s2.is_currently_suspended)
        self.assertEqual(Announcement.objects.filter(student__in=[self.s1, self.s2], audience="student").count(), 2)

        resp_reinstate = c.post(
            reverse("students"),
            {
                "selected_students": [str(self.s1.pk), str(self.s2.pk)],
                "bulk_action": "reinstate",
            },
            follow=True,
        )
        self.assertEqual(resp_reinstate.status_code, 200)
        self.s1.refresh_from_db()
        self.s2.refresh_from_db()
        self.assertFalse(self.s1.is_suspended)
        self.assertFalse(self.s2.is_suspended)
        self.assertEqual(Announcement.objects.filter(student__in=[self.s1, self.s2]).count(), 0)

        # Bulk mark left and delete cascades to User accounts
        resp_left = c.post(
            reverse("students"),
            {"selected_students": [str(self.s2.pk)], "bulk_action": "mark_left"},
            follow=True,
        )
        self.assertEqual(resp_left.status_code, 200)
        self.s2.refresh_from_db()
        self.assertFalse(self.s2.is_active)

        resp_del = c.post(
            reverse("students"),
            {"selected_students": [str(self.s2.pk)], "bulk_action": "delete"},
            follow=True,
        )
        self.assertEqual(resp_del.status_code, 200)
        self.assertFalse(Student.objects.filter(pk=self.s2.pk).exists())
        self.assertFalse(User.objects.filter(pk=self.u2.pk).exists())









class BuildingSegregationTests(TestCase):
    def setUp(self):
        self.b1, _ = Building.objects.get_or_create(
            code="B1",
            defaults={
                "name": "First Year Block",
                "allowed_years": "1",
                "is_first_year_only": True,
            },
        )
        self.b2, _ = Building.objects.get_or_create(
            code="B2",
            defaults={
                "name": "Senior Block",
                "allowed_years": "2,3,4",
                "is_first_year_only": False,
            },
        )
        self.admin_u = User.objects.create_superuser(
            username="bld_admin", password="Password@123", role="admin",
            first_name="Admin", last_name="Hostel", must_change_password=False,
        )
        self.head_warden = User.objects.create_user(
            username="headwarden", password="Password@123", role="warden", is_staff=True,
            first_name="Meenakshi", last_name="Head", assigned_building=None, must_change_password=False,
        )
        self.fy_warden = User.objects.create_user(
            username="fywarden", password="Password@123", role="warden", is_staff=True,
            first_name="Karthik", last_name="FY", assigned_building=self.b1, must_change_password=False,
        )

        # Identical floor & room number across B1 and B2 must coexist without collision
        self.r_b1 = Room.objects.create(building=self.b1, block="G", number="101", capacity=2)
        self.r_b2 = Room.objects.create(building=self.b2, block="G", number="101", capacity=2)

        self.s1_y1 = onboard_student(
            "Aarav Kumar", "FY2601", "B.Tech CSE", "9800000001",
            "Ramesh Kumar", "9800000011", room=self.r_b1, year_of_study=1,
        )
        self.s1_y1.user.must_change_password = False
        self.s1_y1.user.save()

        self.s1_unalloc = onboard_student(
            "Diya Nair", "FY2602", "B.Tech CSE", "9800000002",
            "Latha Nair", "9800000012", room=None, year_of_study=1,
        )

        self.s2_y3 = onboard_student(
            "Vikram Rao", "SR2401", "B.Tech ECE", "9800000003",
            "Suresh Rao", "9800000013", room=self.r_b2, year_of_study=3,
        )
        self.s2_y3.user.must_change_password = False
        self.s2_y3.user.save()

        self.s2_unalloc = onboard_student(
            "Neha Verma", "SR2502", "B.Tech ME", "9800000004",
            "Anil Verma", "9800000014", room=None, year_of_study=2,
        )

    def test_duplicate_floor_rooms_and_string_formatting(self):
        self.assertEqual(str(self.r_b1), "B1 · G-101")
        self.assertEqual(str(self.r_b2), "B2 · G-101")
        self.assertEqual(self.r_b1.short_code, "G-101")
        self.assertEqual(self.r_b2.short_code, "G-101")

    def test_antiragging_validation_blocks_cross_building_allocations(self):
        # Model validation blocks 1st year -> Senior Block and 3rd year -> First Year Block
        with self.assertRaises(ValidationError):
            RoomAllocation(student=self.s1_unalloc, room=self.r_b2).full_clean()
        with self.assertRaises(ValidationError):
            RoomAllocation(student=self.s2_unalloc, room=self.r_b1).full_clean()

        # Service layer rejects cross-building allocations
        with self.assertRaises(ValidationError):
            allocate_student_to_room(self.s1_unalloc, self.r_b2)
        with self.assertRaises(ValidationError):
            allocate_student_to_room(self.s2_unalloc, self.r_b1)

        # Head warden HTTP POST allocate rejects incompatible year -> building
        c = Client()
        c.force_login(self.head_warden)
        resp = c.post(
            reverse("student_allocate", args=[self.s1_unalloc.pk]),
            {"room_id": str(self.r_b2.pk), "start_date": date.today().isoformat()},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.s1_unalloc.refresh_from_db()
        self.assertIsNone(self.s1_unalloc.room)

        # Compatible allocation succeeds cleanly
        resp_ok = c.post(
            reverse("student_allocate", args=[self.s1_unalloc.pk]),
            {"room_id": str(self.r_b1.pk), "start_date": date.today().isoformat()},
            follow=True,
        )
        self.assertEqual(resp_ok.status_code, 200)
        self.s1_unalloc.refresh_from_db()
        self.assertEqual(self.s1_unalloc.room, self.r_b1)

    def test_bulk_set_year_skips_incompatible_occupied_building(self):
        c = Client()
        c.force_login(self.admin_u)
        # Updating allocated B2 3rd-year to 1st-year is blocked while residing in B2
        c.post(
            reverse("students"),
            {
                "selected_students": [str(self.s2_y3.pk), str(self.s2_unalloc.pk)],
                "bulk_action": "set_year",
                "bulk_year": "1",
            },
            follow=True,
        )
        self.s2_y3.refresh_from_db()
        self.s2_unalloc.refresh_from_db()
        self.assertEqual(self.s2_y3.year_of_study, 3)
        self.assertEqual(self.s2_unalloc.year_of_study, 1)

    def test_two_tier_warden_hierarchy_and_strict_first_year_lock(self):
        c = Client()
        c.force_login(self.fy_warden)

        # FY Warden views only Building 1
        dash = c.get(reverse("home"))
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(c.get(reverse("home") + "?building=B2").status_code, 403)
        self.assertEqual(c.get(reverse("buildings")).status_code, 403)

        rooms_resp = c.get(reverse("rooms"))
        self.assertEqual(rooms_resp.status_code, 200)
        self.assertContains(rooms_resp, "B1 · G-101")
        self.assertNotContains(rooms_resp, "B2 · G-101")
        self.assertEqual(c.get(reverse("room_detail", args=[self.r_b2.pk])).status_code, 403)

        st_resp = c.get(reverse("students"))
        self.assertEqual(st_resp.status_code, 200)
        self.assertContains(st_resp, self.s1_y1.roll_no)
        self.assertNotContains(st_resp, self.s2_y3.roll_no)
        self.assertEqual(c.get(reverse("student_detail", args=[self.s2_y3.pk])).status_code, 403)
        self.assertEqual(
            c.post(reverse("student_allocate", args=[self.s1_unalloc.pk]), {"room_id": str(self.r_b2.pk)}).status_code,
            403,
        )
        self.assertEqual(c.get(reverse("attendance") + "?building=B2").status_code, 403)
        self.assertEqual(c.get(reverse("fees_export_pdf") + "?building=B2").status_code, 403)

        # Head Warden has unrestricted access across B1 and B2
        c.force_login(self.head_warden)
        self.assertEqual(c.get(reverse("home") + "?building=B2").status_code, 200)
        self.assertEqual(c.get(reverse("room_detail", args=[self.r_b2.pk])).status_code, 200)
        self.assertEqual(c.get(reverse("student_detail", args=[self.s2_y3.pk])).status_code, 200)

    def test_building_scoped_announcements_only_reach_target_building(self):
        c = Client()
        c.force_login(self.admin_u)
        resp = c.post(
            reverse("announcements_new"),
            {
                "title": "B1 Curfew Drill",
                "body": "First-year residents assemble in B1 courtyard at 6 PM.",
                "audience": "all",
                "building": str(self.b1.pk),
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Notification.objects.filter(recipient=self.s1_y1.user, title__icontains="B1 Curfew Drill").exists())
        self.assertFalse(Notification.objects.filter(recipient=self.s2_y3.user, title__icontains="B1 Curfew Drill").exists())

        c.force_login(self.s1_y1.user)
        self.assertContains(c.get(reverse("my_announcements")), "B1 Curfew Drill")
        c.force_login(self.s2_y3.user)
        self.assertNotContains(c.get(reverse("my_announcements")), "B1 Curfew Drill")

    def test_year_and_building_fee_calculation_reports_and_pdfs(self):
        c = Client()
        c.force_login(self.head_warden)
        month_str = date.today().strftime("%Y-%m")

        # Generate monthly bills ONLY for Year 1 / Building 1
        resp_calc = c.post(
            f"{reverse('fees_calculate_monthly')}?building=B1&year=1",
            {
                "month": month_str,
                "building": "B1",
                "year": "1",
                "title": "First Year November Fee",
                "due_date": (date.today() + timedelta(days=10)).isoformat(),
                "selected_students": [str(self.s1_y1.pk)],
                f"rent_{self.s1_y1.pk}": "8000.00",
                f"food_{self.s1_y1.pk}": "4500.00",
                f"mess_days_{self.s1_y1.pk}": "0",
                f"mess_cut_{self.s1_y1.pk}": "0.00",
                f"total_{self.s1_y1.pk}": "12500.00",
                "save_fees": "1",
            },
            follow=True,
        )
        self.assertEqual(resp_calc.status_code, 200)
        self.assertTrue(Fee.objects.filter(student=self.s1_y1, month=month_str).exists())
        self.assertFalse(Fee.objects.filter(student=self.s2_y3, month=month_str).exists())

        # Create a B2 / 3rd-year fee directly to verify filtered queries & PDFs
        b2_fee = Fee.objects.create(
            student=self.s2_y3, title="Senior Year Fee", month=month_str,
            rent_amount=9000, food_amount=5000, amount=14000, due_date=date.today(),
        )
        b1_fee = Fee.objects.get(student=self.s1_y1, month=month_str)

        # Ledger and reports views load cleanly
        self.assertEqual(c.get(reverse("fees") + "?year=1&building=B1").status_code, 200)
        self.assertContains(c.get(reverse("fees") + "?year=1"), self.s1_y1.roll_no)
        self.assertNotContains(c.get(reverse("fees") + "?year=1"), self.s2_y3.roll_no)
        self.assertEqual(c.get(reverse("reports") + "?building=B2&year=3").status_code, 200)

        # Year- and building-specific PDF exports return valid %PDF documents
        for url in [
            reverse("fees_export_pdf") + "?year=1",
            reverse("fees_export_pdf") + "?year=3&building=B2&status=pending",
            reverse("fee_pdf", args=[b1_fee.pk]),
            reverse("fee_pdf", args=[b2_fee.pk]),
            reverse("reports") + "?export=fees_pdf&building=B1",
        ]:
            pdf_resp = c.get(url)
            self.assertEqual(pdf_resp.status_code, 200, url)
            self.assertEqual(pdf_resp["Content-Type"], "application/pdf", url)
            self.assertTrue(pdf_resp.content.startswith(b"%PDF"), url)


class RoomTypePricingTests(TestCase):
    """Verify 1/2/3 sharing room types, separate pricing, unified monthly PDF, and room-type allocation."""

    def setUp(self):
        self.b1, _ = Building.objects.get_or_create(
            code="B1",
            defaults={
                "name": "First Year Block",
                "allowed_years": "1",
                "is_first_year_only": True,
            },
        )
        self.b2, _ = Building.objects.get_or_create(
            code="B2",
            defaults={
                "name": "Senior Block",
                "allowed_years": "2,3,4",
                "is_first_year_only": False,
            },
        )
        self.admin_u = User.objects.create_superuser(
            username="rt_admin", password="Pass@1234", role="admin", first_name="Super", last_name="Admin"
        )
        self.head_warden = User.objects.create_user(
            username="rt_headwarden", password="Pass@1234", role="warden", is_staff=True, first_name="Head", last_name="Warden"
        )
        self.fy_warden = User.objects.create_user(
            username="rt_fywarden", password="Pass@1234", role="warden", is_staff=True, assigned_building=self.b1, first_name="FY", last_name="Warden"
        )
        self.cfg = FeeConfig.get_config()
        self.cfg.rent_1_sharing = Decimal("3500.00")
        self.cfg.rent_2_sharing = Decimal("2200.00")
        self.cfg.rent_3_sharing = Decimal("1600.00")
        self.cfg.rent_amount = Decimal("1200.00")
        self.cfg.food_rate_per_day = Decimal("100.00")
        self.cfg.save()

        self.r_b1_1s = Room.objects.create(building=self.b1, block="G", number="801", room_type="1_sharing")
        self.r_b1_2s = Room.objects.create(building=self.b1, block="G", number="802", room_type="2_sharing")
        self.r_b2_3s = Room.objects.create(building=self.b2, block="1", number="803", room_type="3_sharing")

        self.s_1s = onboard_student(
            "Single Resident", "RT101", "CSE", "9800000101", room=self.r_b1_1s, year_of_study=1, preferred_room_type="1_sharing"
        )
        self.s_2s = onboard_student(
            "Double Resident", "RT102", "CSE", "9800000102", room=self.r_b1_2s, year_of_study=1, preferred_room_type="2_sharing"
        )
        self.s_3s = onboard_student(
            "Triple Resident", "RT201", "ECE", "9800000201", room=self.r_b2_3s, year_of_study=3, preferred_room_type="3_sharing"
        )
        self.s_unalloc = onboard_student(
            "Unalloc Resident", "RT103", "MECH", "9800000103", room=None, year_of_study=1, preferred_room_type="1_sharing"
        )

    def test_room_type_and_capacity_sync_with_legacy_compat(self):
        legacy_room = Room.objects.create(building=self.b2, block="F", number="901", capacity=3)
        self.assertEqual(legacy_room.room_type, "3_sharing")
        self.assertEqual(legacy_room.room_type_label, "3 Sharing")

        single_room = Room.objects.create(building=self.b2, block="F", number="902", room_type="1_sharing")
        self.assertEqual(single_room.capacity, 1)
        self.assertEqual(single_room.room_type_label, "1 Sharing")
        self.assertEqual(single_room.monthly_rent(self.cfg), Decimal("3500.00"))

        single_room.room_type = "3_sharing"
        single_room.save()
        single_room.refresh_from_db()
        self.assertEqual(single_room.capacity, 3)

    def test_tiered_pricing_calculation_and_fee_structure_settings(self):
        today = date.today()
        calc_1s = calculate_student_monthly_fee(self.s_1s, today.year, today.month, config=self.cfg)
        calc_2s = calculate_student_monthly_fee(self.s_2s, today.year, today.month, config=self.cfg)
        calc_3s = calculate_student_monthly_fee(self.s_3s, today.year, today.month, config=self.cfg)
        calc_un = calculate_student_monthly_fee(self.s_unalloc, today.year, today.month, config=self.cfg)

        self.assertEqual(calc_1s["rent_amount"], Decimal("3500.00"))
        self.assertEqual(calc_1s["room_type"], "1 Sharing")
        self.assertEqual(calc_2s["rent_amount"], Decimal("2200.00"))
        self.assertEqual(calc_3s["rent_amount"], Decimal("1600.00"))
        self.assertEqual(calc_un["rent_amount"], Decimal("1200.00"))
        self.assertEqual(calc_un["room_type"], "Unallocated")

        c = Client()
        c.force_login(self.admin_u)
        resp = c.post(
            reverse("fee_structure"),
            {
                "rent_1_sharing": "4000.00",
                "rent_2_sharing": "2500.00",
                "rent_3_sharing": "1800.00",
                "rent_amount": "1500.00",
                "food_rate_per_day": "120.00",
                "mess_cut_min_days": "3",
                "mess_cut_rate_per_day": "120.00",
                "mess_cut_mode": "per_streak",
                "late_fine_amount": "300.00",
                "late_fine_interval_days": "5",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        updated = FeeConfig.get_config()
        self.assertEqual(updated.rent_1_sharing, Decimal("4000.00"))
        self.assertEqual(updated.rent_2_sharing, Decimal("2500.00"))
        self.assertEqual(updated.rent_3_sharing, Decimal("1800.00"))

    def test_single_unified_monthly_billing_pdf_combines_all_room_types(self):
        c = Client()
        c.force_login(self.head_warden)
        today = date.today()
        month_str = today.strftime("%Y-%m")
        all_targets = [self.s_1s, self.s_2s, self.s_3s, self.s_unalloc]
        post_data = {
            "month": month_str,
            "title": f"Unified Monthly Bill - {month_str}",
            "due_date": (today + timedelta(days=10)).isoformat(),
            "selected_students": [str(s.pk) for s in all_targets],
            "save_fees": "1",
        }
        # Leave explicit rent fields blank for half of students so backend tiered lookup is exercised too
        for s in all_targets:
            c_res = calculate_student_monthly_fee(s, today.year, today.month, config=self.cfg)
            post_data[f"food_{s.pk}"] = str(c_res["food_amount"])
            post_data[f"mess_days_{s.pk}"] = "0"
            post_data[f"mess_cut_{s.pk}"] = "0.00"
            post_data[f"total_{s.pk}"] = str(c_res["total_amount"])

        resp = c.post(reverse("fees_calculate_monthly"), post_data, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Fee.objects.get(student=self.s_1s, month=month_str).rent_amount, Decimal("3500.00"))
        self.assertEqual(Fee.objects.get(student=self.s_2s, month=month_str).rent_amount, Decimal("2200.00"))
        self.assertEqual(Fee.objects.get(student=self.s_3s, month=month_str).rent_amount, Decimal("1600.00"))
        self.assertEqual(Fee.objects.get(student=self.s_unalloc, month=month_str).rent_amount, Decimal("1200.00"))

        # Single unified monthly PDF combines 1/2/3 sharing and unallocated residents in one document
        pdf_resp = c.get(f"{reverse('fees_export_pdf')}?month={month_str}")
        self.assertEqual(pdf_resp.status_code, 200)
        self.assertEqual(pdf_resp["Content-Type"], "application/pdf")
        self.assertTrue(pdf_resp.content.startswith(b"%PDF"))

        single_inv = c.get(reverse("fee_pdf", args=[Fee.objects.get(student=self.s_1s, month=month_str).pk]))
        self.assertEqual(single_inv.status_code, 200)
        self.assertTrue(single_inv.content.startswith(b"%PDF"))

        # Fee list ledger does not contain separate per-room-type PDF export links
        fees_page = c.get(reverse("fees"))
        self.assertEqual(fees_page.status_code, 200)
        self.assertContains(fees_page, "Export Filtered PDF")
        self.assertNotContains(fees_page, "1 Sharing PDF")

    def test_room_type_bulk_creation_csv_and_directory_filter(self):
        bulk_res = bulk_create_rooms(
            block="S", start_number="301", count=2, building=self.b2, room_type="1_sharing"
        )
        self.assertEqual(len(bulk_res["created"]), 2)
        for r in bulk_res["created"]:
            self.assertEqual(r.room_type, "1_sharing")
            self.assertEqual(r.capacity, 1)

        csv_res = import_rooms_csv(
            [{"building": "B2", "block": "S", "number": "305", "room_type": "3 Sharing"}],
            building=self.b2,
        )
        self.assertEqual(len(csv_res["created"]), 1)
        self.assertEqual(csv_res["created"][0].room_type, "3_sharing")
        self.assertEqual(csv_res["created"][0].capacity, 3)

        c = Client()
        c.force_login(self.head_warden)
        resp_3s = c.get(f"{reverse('rooms')}?room_type=3_sharing")
        self.assertContains(resp_3s, "S-305")
        self.assertNotContains(resp_3s, "S-301")

    def test_room_type_allocation_and_anti_ragging_segregation(self):
        r_b1_new_1s = Room.objects.create(building=self.b1, block="1", number="105", room_type="1_sharing")
        c = Client()
        c.force_login(self.fy_warden)

        # FY warden sees only B1 rooms even when filtering by 1_sharing
        resp_rooms = c.get(f"{reverse('rooms')}?room_type=1_sharing")
        self.assertContains(resp_rooms, str(r_b1_new_1s))
        self.assertNotContains(resp_rooms, "B2")

        # Profile allocation modal renders sharing-type filter and student's preferred tier
        prof_resp = c.get(reverse("student_detail", args=[self.s_unalloc.pk]))
        self.assertEqual(prof_resp.status_code, 200)
        self.assertContains(prof_resp, "Prefers 1 Sharing")
        self.assertContains(prof_resp, "modalRoomTypeFilter")

        # Assign unallocated student to the vacant B1 1-sharing room
        alloc_resp = c.post(
            reverse("student_allocate", args=[self.s_unalloc.pk]),
            {"room_id": str(r_b1_new_1s.pk), "start_date": date.today().isoformat()},
            follow=True,
        )
        self.assertEqual(alloc_resp.status_code, 200)
        self.s_unalloc.refresh_from_db()
        self.assertEqual(self.s_unalloc.room, r_b1_new_1s)

        # Head warden dashboard & reports show room-type breakdown nested inside each building card
        c.force_login(self.head_warden)
        dash_resp = c.get(reverse("home"))
        self.assertContains(dash_resp, "building=B1&room_type=1_sharing")
        self.assertNotContains(dash_resp, "1 Sharing Rooms")
        rep_resp = c.get(reverse("reports"))
        self.assertContains(rep_resp, "building=B2&room_type=3_sharing")
        self.assertNotContains(rep_resp, "Room Sharing Tiers")

    def test_seed_demo_command_populates_varied_room_types(self):
        from django.core.management import call_command

        call_command("seed_demo")
        self.assertTrue(Room.objects.filter(room_type="1_sharing").exists())
        self.assertTrue(Room.objects.filter(room_type="2_sharing").exists())
        self.assertTrue(Room.objects.filter(room_type="3_sharing").exists())
        self.assertTrue(Student.objects.filter(preferred_room_type="1_sharing").exists())
        self.assertTrue(Fee.objects.filter(rent_amount=Decimal("2200.00")).exists() or Fee.objects.filter(rent_amount=Decimal("3000.00")).exists())
        self.assertTrue(Fee.objects.filter(rent_amount=Decimal("3500.00")).exists() or Fee.objects.filter(rent_amount=Decimal("2000.00")).exists())


class BuildingRoomTypePlacementTests(TestCase):
    """Verify 1/2/3 sharing tiers are nested inside every building card on Dashboard, Buildings, and Reports."""

    def setUp(self):
        self.b1, _ = Building.objects.get_or_create(
            code="B1",
            defaults={"name": "First Year Block", "allowed_years": "1", "is_first_year_only": True},
        )
        self.b2, _ = Building.objects.get_or_create(
            code="B2",
            defaults={"name": "Senior Block", "allowed_years": "2,3,4", "is_first_year_only": False},
        )
        self.admin_u = User.objects.create_superuser(
            username="brt_admin", password="Pass@1234", role="admin", first_name="Super", last_name="Admin"
        )
        self.head_warden = User.objects.create_user(
            username="brt_headwarden", password="Pass@1234", role="warden", is_staff=True, first_name="Head", last_name="Warden"
        )
        self.fy_warden = User.objects.create_user(
            username="brt_fywarden", password="Pass@1234", role="warden", is_staff=True, assigned_building=self.b1, first_name="FY", last_name="Warden"
        )
        self.cfg = FeeConfig.get_config()
        self.cfg.rent_1_sharing = Decimal("3500.00")
        self.cfg.rent_2_sharing = Decimal("2200.00")
        self.cfg.rent_3_sharing = Decimal("1600.00")
        self.cfg.save()

        self.r_b1_1s = Room.objects.create(building=self.b1, block="G", number="701", room_type="1_sharing")
        self.r_b1_2s = Room.objects.create(building=self.b1, block="G", number="702", room_type="2_sharing")
        self.r_b2_3s = Room.objects.create(building=self.b2, block="1", number="703", room_type="3_sharing")

        onboard_student("FY Single", "BRT101", "CSE", "9800001101", room=self.r_b1_1s, year_of_study=1)
        onboard_student("SR Triple", "BRT201", "ECE", "9800002101", room=self.r_b2_3s, year_of_study=3)

    def test_dashboard_shows_room_types_inside_building_cards(self):
        c = Client()
        c.force_login(self.admin_u)

        resp_all = c.get(reverse("home"))
        self.assertEqual(resp_all.status_code, 200)
        self.assertContains(resp_all, "building=B1&room_type=1_sharing")
        self.assertContains(resp_all, "building=B2&room_type=3_sharing")
        self.assertNotContains(resp_all, "1 Sharing Rooms")

        # Filtering to a single building keeps its card + nested 1/2/3 sharing tiers visible
        resp_b1 = c.get(f"{reverse('home')}?building=B1")
        self.assertEqual(resp_b1.status_code, 200)
        self.assertContains(resp_b1, "building=B1&room_type=1_sharing")
        self.assertContains(resp_b1, "building=B1&room_type=2_sharing")
        self.assertNotContains(resp_b1, "building=B2&room_type=3_sharing")

        # Building-locked First-Year Warden sees B1 card with nested sharing tiers and zero B2 leakage
        c.force_login(self.fy_warden)
        resp_fy = c.get(reverse("home"))
        self.assertEqual(resp_fy.status_code, 200)
        self.assertContains(resp_fy, "building=B1&room_type=1_sharing")
        self.assertNotContains(resp_fy, "building=B2&room_type=3_sharing")

    def test_buildings_page_and_reports_nest_room_types_per_building(self):
        c = Client()
        c.force_login(self.admin_u)

        bld_resp = c.get(reverse("buildings"))
        self.assertEqual(bld_resp.status_code, 200)
        self.assertContains(bld_resp, "Room Types &amp; Pricing Inside B1:")
        self.assertContains(bld_resp, "Room Types &amp; Pricing Inside B2:")
        self.assertContains(bld_resp, "building=B1&room_type=1_sharing")
        self.assertContains(bld_resp, "building=B2&room_type=3_sharing")

        # Bulk-add link from building card works cleanly with ?building=B1
        bulk_resp = c.get(f"{reverse('rooms_bulk_add')}?building=B1")
        self.assertEqual(bulk_resp.status_code, 200)

        rep_resp = c.get(reverse("reports"))
        self.assertEqual(rep_resp.status_code, 200)
        self.assertContains(rep_resp, "building=B1&room_type=1_sharing")
        self.assertContains(rep_resp, "building=B2&room_type=3_sharing")
        self.assertContains(rep_resp, "Export Unified Fees PDF")
        self.assertNotContains(rep_resp, "Room Sharing Tiers")

        # FY Warden reports view shows only B1 nested room types
        c.force_login(self.fy_warden)
        fy_rep = c.get(reverse("reports"))
        self.assertEqual(fy_rep.status_code, 200)
        self.assertContains(fy_rep, "building=B1&room_type=1_sharing")
        self.assertNotContains(fy_rep, "building=B2&room_type=3_sharing")


class GatePassAndVisitorTests(TestCase):
    def setUp(self):
        self.b1, _ = Building.objects.get_or_create(
            code="B1",
            defaults={
                "name": "First Year Block",
                "description": "Dedicated residential block for 1st-year students",
                "is_first_year_only": True,
            },
        )
        self.b2, _ = Building.objects.get_or_create(
            code="B2",
            defaults={
                "name": "Senior Block",
                "description": "Residential block for 2nd, 3rd, and 4th-year students",
                "is_first_year_only": False,
            },
        )
        self.admin_u = User.objects.create_superuser(
            username="gate_admin", password="Pass@1234", role="admin", first_name="Gate", last_name="Admin"
        )
        self.head_warden = User.objects.create_user(
            username="gate_warden", password="Pass@1234", role="warden", is_staff=True, first_name="Head", last_name="Warden"
        )
        self.fy_warden = User.objects.create_user(
            username="gate_fywarden",
            password="Pass@1234",
            role="warden",
            is_staff=True,
            assigned_building=self.b1,
            first_name="First",
            last_name="Warden",
        )
        self.r_b1 = Room.objects.create(building=self.b1, block="G", number="801", room_type="2_sharing")
        self.r_b2 = Room.objects.create(building=self.b2, block="1", number="802", room_type="3_sharing")

        self.parent1_u = User.objects.create_user(
            username="gp_parent_1", password="Pass@1234", role="parent", phone="9811112222", first_name="Ramesh", last_name="Sharma"
        )
        self.parent1 = Parent.objects.create(user=self.parent1_u)
        self.s1_u = User.objects.create_user(
            username="gp_student_1", password="Pass@1234", role="student", phone="9800001001", first_name="Aarav", last_name="Sharma"
        )
        self.s1 = Student.objects.create(
            user=self.s1_u,
            roll_no="GP101",
            course="B.Tech CSE",
            year_of_study=1,
            parent=self.parent1,
        )
        allocate_student_to_room(self.s1, self.r_b1)

        self.parent2_u = User.objects.create_user(
            username="gp_parent_2", password="Pass@1234", role="parent", phone="9822223333", first_name="Sunita", last_name="Verma"
        )
        self.parent2 = Parent.objects.create(user=self.parent2_u)
        self.s2_u = User.objects.create_user(
            username="gp_student_2", password="Pass@1234", role="student", phone="9800002001", first_name="Karan", last_name="Verma"
        )
        self.s2 = Student.objects.create(
            user=self.s2_u,
            roll_no="GP201",
            course="B.Tech ECE",
            year_of_study=3,
            parent=self.parent2,
        )
        allocate_student_to_room(self.s2, self.r_b2)

    def test_gate_pass_and_visitor_model_validation(self):
        now = timezone.now()
        invalid_gp = GatePass(
            student=self.s1,
            pass_type="day_outing",
            out_at=now + timedelta(hours=3),
            expected_return_at=now + timedelta(hours=1),
            reason="Return before out",
        )
        with self.assertRaises(ValidationError):
            invalid_gp.full_clean()

        valid_gp = GatePass.objects.create(
            student=self.s1,
            pass_type="day_outing",
            out_at=now + timedelta(hours=1),
            expected_return_at=now + timedelta(hours=5),
            reason="Bookstore visit",
        )
        self.assertEqual(valid_gp.pass_code, f"GP-{valid_gp.pk:04d}")

        from core.forms import GatePassRequestForm, VisitorLogForm
        local_now = timezone.localtime(now)
        bad_gp_form = GatePassRequestForm(
            data={
                "pass_type": "day_outing",
                "out_at": (local_now + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M"),
                "expected_return_at": (local_now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                "reason": "Invalid interval",
            }
        )
        self.assertFalse(bad_gp_form.is_valid())

        bad_visitor_form = VisitorLogForm(
            data={
                "student": str(self.s1.pk),
                "visitor_name": "",
                "relation": "Uncle",
                "phone": "",
                "purpose": "Package dropoff",
                "id_proof_last4": "4821",
            }
        )
        self.assertFalse(bad_visitor_form.is_valid())
        self.assertIn("visitor_name", bad_visitor_form.errors)
        self.assertIn("phone", bad_visitor_form.errors)

    def test_full_gate_pass_lifecycle_optional_parent_ack_and_notifications(self):
        c_student = Client()
        c_student.force_login(self.s1_u)
        c_parent = Client()
        c_parent.force_login(self.parent1_u)
        c_warden = Client()
        c_warden.force_login(self.fy_warden)

        now = timezone.localtime(timezone.now())
        out_dt = now - timedelta(minutes=10)
        exp_dt = now + timedelta(hours=4)

        resp_sub = c_student.post(
            reverse("my_gate_passes"),
            {
                "pass_type": "day_outing",
                "out_at": out_dt.strftime("%Y-%m-%dT%H:%M"),
                "expected_return_at": exp_dt.strftime("%Y-%m-%dT%H:%M"),
                "reason": "City hackathon event",
            },
            follow=True,
        )
        self.assertEqual(resp_sub.status_code, 200)
        gp = GatePass.objects.get(student=self.s1, reason="City hackathon event")
        self.assertEqual(gp.status, "Pending")
        self.assertEqual(gp.parent_ack, "Pending")
        self.assertTrue(
            Notification.objects.filter(recipient=self.fy_warden, category="gate", title__contains=gp.pass_code).exists()
        )
        self.assertTrue(Notification.objects.filter(recipient=self.parent1_u, category="gate").exists())

        # Parent Portal optional confirmation button
        parent_portal = c_parent.get(reverse("my_gate_passes"))
        self.assertEqual(parent_portal.status_code, 200)
        self.assertContains(parent_portal, gp.pass_code)
        ack_resp = c_parent.post(reverse("gate_pass_parent_ack", args=[gp.pk]), follow=True)
        self.assertEqual(ack_resp.status_code, 200)
        gp.refresh_from_db()
        self.assertEqual(gp.parent_ack, "Confirmed")
        self.assertIsNotNone(gp.parent_ack_at)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.fy_warden, category="gate", title=f"Parent Confirmed Gate Pass: {gp.pass_code}"
            ).exists()
        )

        # Warden approves pass 1
        dec_resp = c_warden.post(reverse("gate_pass_decide", args=[gp.pk, "approve"]), follow=True)
        self.assertEqual(dec_resp.status_code, 200)
        gp.refresh_from_db()
        self.assertEqual(gp.status, "Approved")
        self.assertEqual(gp.approved_by, self.fy_warden)

        # Optional: pass 2 stays Pending parent_ack and Warden can still approve & reject
        gp_no_ack = GatePass.objects.create(
            student=self.s1,
            pass_type="home_leave",
            out_at=now + timedelta(hours=2),
            expected_return_at=now + timedelta(days=1),
            reason="Urgent family travel",
        )
        dec2 = c_warden.post(reverse("gate_pass_decide", args=[gp_no_ack.pk, "approve"]), follow=True)
        self.assertEqual(dec2.status_code, 200)
        gp_no_ack.refresh_from_db()
        self.assertEqual(gp_no_ack.parent_ack, "Pending")
        self.assertEqual(gp_no_ack.status, "Approved")

        gp_rej = GatePass.objects.create(
            student=self.s1,
            pass_type="day_outing",
            out_at=now + timedelta(hours=3),
            expected_return_at=now + timedelta(hours=7),
            reason="Late night movie",
        )
        c_warden.post(reverse("gate_pass_decide", args=[gp_rej.pk, "reject"]), follow=True)
        gp_rej.refresh_from_db()
        self.assertEqual(gp_rej.status, "Rejected")

        # Parent receives instant notifications on Check Out and Check In
        parent_notif_before = Notification.objects.filter(recipient=self.parent1_u, category="gate").count()
        out_resp = c_warden.post(reverse("gate_pass_checkout", args=[gp.pk]), follow=True)
        self.assertEqual(out_resp.status_code, 200)
        gp.refresh_from_db()
        self.assertEqual(gp.status, "Checked Out")
        parent_notif_after_out = Notification.objects.filter(recipient=self.parent1_u, category="gate").count()
        self.assertEqual(parent_notif_after_out, parent_notif_before + 1)

        in_resp = c_warden.post(reverse("gate_pass_checkin", args=[gp.pk]), follow=True)
        self.assertEqual(in_resp.status_code, 200)
        gp.refresh_from_db()
        self.assertEqual(gp.status, "Returned")
        self.assertIsNotNone(gp.actual_return_at)
        parent_notif_after_in = Notification.objects.filter(recipient=self.parent1_u, category="gate").count()
        self.assertEqual(parent_notif_after_in, parent_notif_after_out + 1)

    def test_curfew_overdue_sync_and_dashboard_late_returns_banner(self):
        now = timezone.now()
        late_b1 = GatePass.objects.create(
            student=self.s1,
            pass_type="day_outing",
            out_at=now - timedelta(hours=5),
            expected_return_at=now - timedelta(minutes=75),
            reason="Stationery errand",
            approved_by=self.fy_warden,
            status="Checked Out",
        )
        late_b2 = GatePass.objects.create(
            student=self.s2,
            pass_type="day_outing",
            out_at=now - timedelta(hours=4),
            expected_return_at=now - timedelta(minutes=45),
            reason="Lab component pickup",
            approved_by=self.head_warden,
            status="Checked Out",
        )

        # Head Warden sees red Late Returns banner and both passes sync to Late
        c_head = Client()
        c_head.force_login(self.head_warden)
        dash_head = c_head.get(reverse("home"))
        self.assertEqual(dash_head.status_code, 200)
        late_b1.refresh_from_db()
        late_b2.refresh_from_db()
        self.assertEqual(late_b1.status, "Late")
        self.assertEqual(late_b2.status, "Late")
        self.assertContains(dash_head, "lateReturnsBanner")
        self.assertContains(dash_head, "LATE RETURNS")
        self.assertContains(dash_head, self.s1.roll_no)
        self.assertContains(dash_head, self.s2.roll_no)

        # FY Warden sees only B1 late pass in dashboard alert
        c_fy = Client()
        c_fy.force_login(self.fy_warden)
        dash_fy = c_fy.get(reverse("home"))
        self.assertEqual(dash_fy.status_code, 200)
        self.assertContains(dash_fy, "lateReturnsBanner")
        self.assertContains(dash_fy, self.s1.roll_no)
        self.assertNotContains(dash_fy, self.s2.roll_no)

        # Check In records actual_return_at while preserving Late status & removes from banner
        notif_before = Notification.objects.filter(recipient=self.parent1_u, category="gate").count()
        c_fy.post(reverse("gate_pass_checkin", args=[late_b1.pk]), {"next": reverse("home")}, follow=True)
        late_b1.refresh_from_db()
        self.assertEqual(late_b1.status, "Late")
        self.assertIsNotNone(late_b1.actual_return_at)
        self.assertEqual(Notification.objects.filter(recipient=self.parent1_u, category="gate").count(), notif_before + 1)

        dash_fy_after = c_fy.get(reverse("home"))
        self.assertNotContains(dash_fy_after, "lateReturnsBanner")

    def test_suspension_blocks_gate_pass_request_and_approval(self):
        now = timezone.localtime(timezone.now())
        existing_gp = GatePass.objects.create(
            student=self.s1,
            pass_type="day_outing",
            out_at=now + timedelta(hours=1),
            expected_return_at=now + timedelta(hours=4),
            reason="Before suspension",
            status="Pending",
        )
        self.s1.is_suspended = True
        self.s1.suspension_reason = "Rule violation"
        self.s1.save()

        c_student = Client()
        c_student.force_login(self.s1_u)
        c_student.post(
            reverse("my_gate_passes"),
            {
                "pass_type": "day_outing",
                "out_at": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
                "expected_return_at": (now + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M"),
                "reason": "Blocked outing",
            },
            follow=True,
        )
        self.assertFalse(GatePass.objects.filter(student=self.s1, reason="Blocked outing").exists())

        c_warden = Client()
        c_warden.force_login(self.fy_warden)
        c_warden.post(reverse("gate_pass_decide", args=[existing_gp.pk, "approve"]), follow=True)
        existing_gp.refresh_from_db()
        self.assertEqual(existing_gp.status, "Pending")

    def test_gate_pass_pdf_and_building_parent_isolation(self):
        now = timezone.now()
        gp_b1 = GatePass.objects.create(
            student=self.s1,
            pass_type="day_outing",
            out_at=now + timedelta(hours=1),
            expected_return_at=now + timedelta(hours=5),
            reason="Exam center visit",
            approved_by=self.fy_warden,
            status="Approved",
        )
        gp_b2 = GatePass.objects.create(
            student=self.s2,
            pass_type="home_leave",
            out_at=now + timedelta(hours=2),
            expected_return_at=now + timedelta(days=1),
            reason="Senior family trip",
            approved_by=self.head_warden,
            status="Pending",
        )

        # PDF authorized for B1 FY Warden, Head Warden, student 1, parent 1
        for user in (self.fy_warden, self.head_warden, self.s1_u, self.parent1_u):
            c = Client()
            c.force_login(user)
            resp = c.get(reverse("gate_pass_pdf", args=[gp_b1.pk]))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp["Content-Type"], "application/pdf")
            self.assertTrue(resp.content.startswith(b"%PDF"))

        # Unrelated student 2 and parent 2 cannot access gp_b1 PDF or parent ack
        for other_u in (self.s2_u, self.parent2_u):
            c = Client()
            c.force_login(other_u)
            self.assertEqual(c.get(reverse("gate_pass_pdf", args=[gp_b1.pk])).status_code, 403)

        c_p2 = Client()
        c_p2.force_login(self.parent2_u)
        self.assertEqual(c_p2.post(reverse("gate_pass_parent_ack", args=[gp_b1.pk])).status_code, 404)

        # FY Warden (B1 locked) cannot view or act on B2 student's passes
        c_fy = Client()
        c_fy.force_login(self.fy_warden)
        gate_list = c_fy.get(reverse("gate_passes"))
        self.assertEqual(gate_list.status_code, 200)
        self.assertContains(gate_list, self.s1.roll_no)
        self.assertNotContains(gate_list, self.s2.roll_no)

        self.assertEqual(c_fy.get(reverse("gate_pass_pdf", args=[gp_b2.pk])).status_code, 403)
        self.assertEqual(c_fy.post(reverse("gate_pass_decide", args=[gp_b2.pk, "approve"])).status_code, 403)
        self.assertEqual(c_fy.post(reverse("gate_pass_checkout", args=[gp_b2.pk])).status_code, 403)
        self.assertEqual(c_fy.post(reverse("gate_pass_checkin", args=[gp_b2.pk])).status_code, 403)

    def test_visitor_register_checkin_filtering_checkout_and_isolation(self):
        c_fy = Client()
        c_fy.force_login(self.fy_warden)

        resp = c_fy.post(
            reverse("visitors"),
            {
                "student": str(self.s1.pk),
                "visitor_name": "Mahesh Sharma",
                "relation": "Father",
                "phone": "9876543210",
                "purpose": "Dropping textbooks",
                "id_proof_last4": "4821",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        visit = VisitorLog.objects.get(student=self.s1, visitor_name="Mahesh Sharma")
        self.assertTrue(visit.is_active_visit)
        self.assertEqual(visit.recorded_by, self.fy_warden)
        self.assertTrue(Notification.objects.filter(recipient=self.s1_u, category="gate").exists())

        # Active filter vs Checked-out filter
        active_resp = c_fy.get(reverse("visitors") + "?status=active")
        self.assertEqual(active_resp.status_code, 200)
        self.assertContains(active_resp, "Mahesh Sharma")
        done_resp = c_fy.get(reverse("visitors") + "?status=checked_out")
        self.assertNotContains(done_resp, "Mahesh Sharma")

        # Cross-building isolation on Visitor Register
        b2_visit = VisitorLog.objects.create(
            student=self.s2,
            visitor_name="Senior Guest",
            relation="Uncle",
            phone="9800009999",
            purpose="Project courier",
            id_proof_last4="9999",
            recorded_by=self.head_warden,
        )
        self.assertNotContains(c_fy.get(reverse("visitors")), "Senior Guest")
        self.assertEqual(c_fy.post(reverse("visitor_checkout", args=[b2_visit.pk])).status_code, 403)

        # 1-click check-out for B1 visitor
        out_resp = c_fy.post(reverse("visitor_checkout", args=[visit.pk]), follow=True)
        self.assertEqual(out_resp.status_code, 200)
        visit.refresh_from_db()
        self.assertFalse(visit.is_active_visit)
        self.assertIsNotNone(visit.checked_out_at)
        self.assertContains(c_fy.get(reverse("visitors") + "?status=checked_out"), "Mahesh Sharma")

    def test_seed_demo_populates_gate_passes_and_visitors(self):
        call_command("seed_demo")
        self.assertTrue(GatePass.objects.filter(status="Late").exists())
        self.assertTrue(GatePass.objects.filter(status="Checked Out").exists())
        self.assertTrue(GatePass.objects.filter(status="Pending").exists())
        self.assertTrue(VisitorLog.objects.filter(checked_out_at__isnull=True).exists())

        warden = User.objects.get(username="warden")
        c = Client()
        c.force_login(warden)
        dash = c.get(reverse("home"))
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "lateReturnsBanner")
        self.assertContains(dash, "LATE RETURNS")
