import csv
import io
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Room, RoomAllocation, Student, User
from core.services import (
    allocate_student_to_room,
    bulk_create_rooms,
    bulk_create_rooms_floors,
    bulk_create_rooms_from_list,
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


