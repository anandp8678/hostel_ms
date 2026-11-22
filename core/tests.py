import csv
import io
from datetime import date, timedelta

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Attendance, Complaint, Fee, FeeConfig, Parent, Room, RoomAllocation, Student, User
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
        self.assertContains(resp_after, "✓ Resolved")

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
        # Month: November 2026 (30 days)
        # 1. Full attendance -> no mess cut
        calc_full = calculate_student_monthly_fee(self.student, 2026, 11)
        self.assertEqual(calc_full["rent_amount"], Decimal("1500.00"))
        self.assertEqual(calc_full["food_amount"], Decimal("3900.00"))  # 30 * 130
        self.assertEqual(calc_full["mess_cut_days"], 0)
        self.assertEqual(calc_full["mess_cut_amount"], Decimal("0.00"))
        self.assertEqual(calc_full["total_amount"], Decimal("5400.00"))

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
        self.assertEqual(calc_3d["total_amount"], Decimal("5270.00"))  # 1500 + 3900 - 130

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
        self.assertEqual(calc["total_amount"], Decimal("5010.00"))  # 1500 + 3900 - 390

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

        # Another student cannot download Student 1's fee receipt
        other_user = User.objects.create_user(username="other_stud", password="p", role="student")
        Student.objects.create(user=other_user, roll_no="CS999")
        c.force_login(other_user)
        blocked_resp = c.get(reverse("fee_pdf", args=[fee.pk]))
        self.assertEqual(blocked_resp.status_code, 403)


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
