from datetime import date, datetime
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Parent, Room, RoomAllocation, Student, User


def _split(name):
    parts = (name or "").strip().split(" ", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _normalize_key(k):
    if not k:
        return ""
    clean = re.sub(r"[^a-zA-Z0-9]", "", str(k)).lower()
    if clean in ("rollno", "rollnumber", "roll", "studentid", "regno", "registrationno", "studentrollno", "username"):
        return "roll_no"
    if clean in ("room", "roomno", "roomnumber", "allocatedroom", "roomallocated", "assignedroom", "roomassigned"):
        return "room"
    if clean in ("number", "no", "roomnum", "roomdigit", "roomid"):
        return "number"
    if clean in ("capacity", "cap", "beds", "bedcount"):
        return "capacity"
    if clean in ("block", "hostelblock", "blockno"):
        return "block"
    if clean in ("startdate", "fromdate", "from", "start", "date", "allocateddate", "allocationdate"):
        return "start_date"
    if clean in ("name", "studentname", "student"):
        return "name"
    if clean in ("course", "branch", "department", "dept", "programme", "program"):
        return "course"
    if clean in ("phone", "mobile", "contact", "studentphone", "phonenumber", "mobilephone", "cell"):
        return "phone"
    if clean in ("parentname", "fathername", "guardianname", "parent", "father", "mother"):
        return "parent_name"
    if clean in ("parentphone", "parentmobile", "parentcontact", "fatherphone", "guardianphone", "parentcell"):
        return "parent_phone"
    return clean


def parse_date(val):
    if not val:
        return date.today()
    if isinstance(val, date):
        return val
    s = str(val).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return date.today()


def find_room(room_str="", block_str=""):
    room_str = str(room_str or "").strip()
    block_str = str(block_str or "").strip()

    if block_str and room_str:
        r = Room.objects.filter(block__iexact=block_str, number__iexact=room_str).first()
        if r:
            return r

    if not room_str:
        return None

    if "-" in room_str:
        b, _, n = room_str.partition("-")
        r = Room.objects.filter(block__iexact=b.strip(), number__iexact=n.strip()).first()
        if r:
            return r

    if " " in room_str:
        b, n = room_str.split(None, 1)
        r = Room.objects.filter(block__iexact=b.strip(), number__iexact=n.strip()).first()
        if r:
            return r

    m = re.match(r"^([a-zA-Z]+)[ -]?(\w+)$", room_str)
    if m:
        b, n = m.group(1), m.group(2)
        r = Room.objects.filter(block__iexact=b, number__iexact=n).first()
        if r:
            return r

    rooms_by_num = Room.objects.filter(number__iexact=room_str)
    if rooms_by_num.count() == 1:
        return rooms_by_num.first()

    return None


def find_student(roll_no="", name=""):
    roll_no = str(roll_no or "").strip()
    if roll_no:
        s = Student.objects.filter(roll_no__iexact=roll_no).select_related("user").first()
        if s:
            return s

    name = str(name or "").strip()
    if name:
        first, last = _split(name)
        if last:
            qs = Student.objects.filter(user__first_name__iexact=first, user__last_name__iexact=last).select_related("user")
        else:
            qs = Student.objects.filter(user__first_name__iexact=first).select_related("user")
        if qs.count() == 1:
            return qs.first()

    return None


@transaction.atomic
def allocate_student_to_room(student, room, start_date=None, vacate_previous=True):
    start_date = start_date or date.today()
    active_alloc = student.allocations.filter(end_date__isnull=True).first()

    if room.is_blacklisted:
        cause = f" ({room.blacklist_reason})" if room.blacklist_reason else ""
        raise ValidationError(f"Room {room} is blacklisted{cause} and cannot accept allocations.")

    if active_alloc and active_alloc.room_id == room.id:
        if not student.is_active:
            student.is_active = True
            student.save(update_fields=["is_active"])
        return active_alloc, False

    if room.occupied >= room.capacity:
        raise ValidationError(f"Room {room} is full (capacity: {room.capacity}, occupied: {room.occupied}).")

    if active_alloc:
        if vacate_previous:
            end_d = start_date if start_date >= active_alloc.start_date else active_alloc.start_date
            active_alloc.end_date = end_d
            active_alloc.save(update_fields=["end_date"])
        else:
            raise ValidationError("This student already has an active room. Vacate it first.")

    alloc = RoomAllocation(student=student, room=room, start_date=start_date)
    alloc.full_clean()
    alloc.save()

    if not student.is_active:
        student.is_active = True
        student.save(update_fields=["is_active"])

    return alloc, True


def link_or_create_parent(student, parent_name="", parent_phone=""):
    digits = re.sub(r"\D", "", parent_phone or "")
    if digits:
        puser, created = User.objects.get_or_create(username=f"p{digits}", defaults=dict(
            role="parent", phone=parent_phone, first_name=_split(parent_name or "Parent")[0],
            last_name=_split(parent_name or "")[1], must_change_password=True))
        if created:
            puser.set_password(settings.DEFAULT_PASSWORD)
            puser.save()
        elif parent_name:
            first, last = _split(parent_name)
            puser.first_name = first
            puser.last_name = last
            puser.phone = parent_phone
            puser.save(update_fields=["first_name", "last_name", "phone"])
        parent, _ = Parent.objects.get_or_create(user=puser)
        student.parent = parent
        student.save(update_fields=["parent"])
        return parent
    return None


class ImportResult(dict):
    """
    Result container that supports:
    - 2-tuple unpacking: `created, errors = import_rows(...)`
    - Tuple-like indexing: `result[0]` (total count), `result[1]` (errors list)
    - Attribute / Dict access: result.created, result.updated, result.allocated, result.errors
    """
    def __init__(self, created=0, updated=0, allocated=0, already_allocated=0, errors=None):
        errors = errors or []
        super().__init__(
            created=created,
            updated=updated,
            allocated=allocated,
            already_allocated=already_allocated,
            created_students=created,
            errors=errors,
            total=created + updated,
        )
        self.created = created
        self.updated = updated
        self.allocated = allocated
        self.already_allocated = already_allocated
        self.created_students = created
        self.errors = errors
        self.total = created + updated

    def __iter__(self):
        yield self.total
        yield self.errors

    def __len__(self):
        return 2

    def __getitem__(self, item):
        if item == 0:
            return self.total
        elif item == 1:
            return self.errors
        return super().__getitem__(item)


@transaction.atomic
def onboard_student(name, roll_no, course="", phone="", parent_name="", parent_phone="", room=None, start_date=None):
    """Create the student login, the parent login (shared between siblings) and optionally a room."""
    roll_no = (roll_no or "").strip()
    name = (name or "").strip()
    if not name or not roll_no:
        raise ValueError("Name and roll number are required.")
    if User.objects.filter(username=roll_no).exists():
        raise ValueError(f"Roll number {roll_no} already exists.")
    first, last = _split(name)
    user = User.objects.create_user(username=roll_no, password=settings.DEFAULT_PASSWORD, first_name=first,
                                    last_name=last, role="student", phone=phone, must_change_password=True)
    student = Student.objects.create(user=user, roll_no=roll_no, course=course)
    if parent_phone or parent_name:
        link_or_create_parent(student, parent_name, parent_phone)
    if room:
        alloc = RoomAllocation(student=student, room=room, start_date=start_date or date.today())
        alloc.full_clean()
        alloc.save()
    return student


def import_rows(rows):
    """
    Unified import for combined CSV:
    Columns: name, roll_no, course, phone, parent_name, parent_phone, room, start_date.
    Handles:
    - New students + parents
    - Existing students updating personal / parent details
    - Room allocation (with start_date, capacity validation, and vacating previous room)
    """
    created_count = 0
    updated_count = 0
    allocated_count = 0
    already_allocated_count = 0
    errors = []

    for i, raw_r in enumerate(rows, start=2):
        r = {_normalize_key(k): (v or "").strip() for k, v in raw_r.items() if k}
        roll_no = r.get("roll_no", "")
        name = r.get("name", "")
        room_str = r.get("room", "")
        block_str = r.get("block", "")
        course = r.get("course", "")
        phone = r.get("phone", "")
        parent_name = r.get("parent_name", "")
        parent_phone = r.get("parent_phone", "")
        start_date = parse_date(r.get("start_date"))

        if not roll_no and not name:
            errors.append(f"Row {i}: Missing student roll number or name.")
            continue

        try:
            with transaction.atomic():
                student = find_student(roll_no=roll_no, name=name)
                if student:
                    # Update existing student
                    if name:
                        first, last = _split(name)
                        if student.user.first_name != first or student.user.last_name != last:
                            student.user.first_name = first
                            student.user.last_name = last
                            student.user.save(update_fields=["first_name", "last_name"])
                    if course:
                        student.course = course
                    if not student.is_active:
                        student.is_active = True
                    student.save()
                    if phone and hasattr(student, "user") and student.user:
                        student.user.phone = phone
                        student.user.save(update_fields=["phone"])
                    if parent_phone or parent_name:
                        link_or_create_parent(student, parent_name, parent_phone)
                    updated_count += 1
                else:
                    # Student not found in database
                    if not name:
                        raise ValueError(f"Student with roll no '{roll_no}' not found.")
                    if not roll_no:
                        raise ValueError(f"Roll number is required for student '{name}'.")
                    student = onboard_student(
                        name=name,
                        roll_no=roll_no,
                        course=course,
                        phone=phone,
                        parent_name=parent_name,
                        parent_phone=parent_phone,
                        room=None,
                    )
                    created_count += 1

                # Handle room allocation if room is provided
                if room_str:
                    room = find_room(room_str, block_str)
                    if not room:
                        raise Room.DoesNotExist(f"Room '{room_str}' not found (use format A-101).")
                    alloc, newly_allocated = allocate_student_to_room(student, room, start_date=start_date)
                    if newly_allocated:
                        allocated_count += 1
                    else:
                        already_allocated_count += 1

        except Room.DoesNotExist as e:
            errors.append(f"Row {i} ({roll_no or name}): {e}")
        except ValidationError as e:
            errors.append(f"Row {i} ({roll_no or name}): {getattr(e, 'messages', [str(e)])[0]}")
        except Exception as e:
            errors.append(f"Row {i} ({roll_no or name}): {getattr(e, 'messages', [str(e)])[0]}")

    return ImportResult(
        created=created_count,
        updated=updated_count,
        allocated=allocated_count,
        already_allocated=already_allocated_count,
        errors=errors,
    )


def import_allocations(rows):
    """Unified allocation import, delegates to import_rows."""
    return import_rows(rows)


def bulk_create_rooms(block="A", start_number="101", count=None, end_number=None, capacity=2, prefix="", suffix=""):
    """
    Creates a particular amount (count) or range of rooms for a given block.
    Block serves as the room prefix (e.g. Block A and room 101 displays like A-101).
    """
    block = (block or "A").strip()
    start_str = str(start_number or "").strip()
    prefix = (prefix or "").strip()
    suffix = (suffix or "").strip()

    if not start_str:
        raise ValueError("Starting room number is required.")

    # If user passed e.g. "A-101", extract block "A" and start number "101"
    if "-" in start_str:
        b_part, _, n_part = start_str.partition("-")
        if b_part.strip().isalpha():
            block = b_part.strip().upper()
            start_str = n_part.strip()

    m = re.match(r"^([a-zA-Z]+)[ -]?(\d+)$", start_str)
    if m and not prefix:
        block = m.group(1).upper()
        digits = m.group(2)
        start_int = int(digits)
        pad = len(digits) if digits.startswith("0") else 0
    else:
        digits_match = re.search(r"\d+", start_str)
        if not digits_match:
            raise ValueError(f"Starting room number '{start_str}' must contain numeric digits.")
        digits = digits_match.group()
        start_int = int(digits)
        pad = len(digits) if digits.startswith("0") else 0

    block = block.upper()

    if count is not None and int(count) > 0:
        end_int = start_int + int(count) - 1
    elif end_number is not None and str(end_number).strip():
        end_str = str(end_number).strip()
        if "-" in end_str:
            _, _, end_str = end_str.partition("-")
        end_match = re.search(r"\d+", end_str)
        if not end_match:
            raise ValueError(f"Ending room number '{end_str}' must contain numeric digits.")
        end_int = int(end_match.group())
        if end_int < start_int:
            raise ValueError("Ending room number cannot be smaller than starting room number.")
    else:
        raise ValueError("Please provide either the amount of rooms to add (count) or the ending room number.")

    capacity = int(capacity or 2)
    if capacity <= 0:
        raise ValueError("Room capacity must be greater than 0.")

    created = []
    skipped = []
    errors = []

    for num in range(start_int, end_int + 1):
        num_str = f"{num:0{pad}d}" if pad > 0 else str(num)
        full_number = f"{prefix}{num_str}{suffix}"

        if Room.objects.filter(block__iexact=block, number__iexact=full_number).exists():
            skipped.append(f"{block}-{full_number}")
            continue

        try:
            r = Room(block=block, number=full_number, capacity=capacity)
            r.full_clean()
            r.save()
            created.append(r)
        except Exception as e:
            errors.append(f"Room {block}-{full_number}: {getattr(e, 'messages', [str(e)])[0]}")

    return {
        "created": created,
        "skipped": skipped,
        "errors": errors,
    }


def bulk_create_rooms_floors(block="A", start_floor=1, end_floor=3, rooms_per_floor=10, capacity=2, floor_padding=2):
    """
    Creates rooms floor-by-floor.
    Example: floors 1 to 3 with 10 rooms each creates 101..110, 201..210, 301..310.
    """
    block = (block or "A").strip()
    start_fl = int(start_floor or 1)
    end_fl = int(end_floor or 1)
    if end_fl < start_fl:
        raise ValueError("Ending floor cannot be smaller than starting floor.")

    r_per_fl = int(rooms_per_floor or 10)
    if r_per_fl <= 0:
        raise ValueError("Rooms per floor must be greater than 0.")

    capacity = int(capacity or 2)
    if capacity <= 0:
        raise ValueError("Room capacity must be greater than 0.")

    created = []
    skipped = []
    errors = []

    for fl in range(start_fl, end_fl + 1):
        for rm in range(1, r_per_fl + 1):
            num_str = f"{fl}{rm:0{floor_padding}d}"
            if Room.objects.filter(block__iexact=block, number__iexact=num_str).exists():
                skipped.append(f"{block}-{num_str}")
                continue

            try:
                r = Room(block=block, number=num_str, capacity=capacity)
                r.full_clean()
                r.save()
                created.append(r)
            except Exception as e:
                errors.append(f"Room {block}-{num_str}: {getattr(e, 'messages', [str(e)])[0]}")

    return {
        "created": created,
        "skipped": skipped,
        "errors": errors,
    }


def bulk_create_rooms_from_list(block="A", text="", default_capacity=2):
    """
    Creates rooms from a comma, semicolon, or newline separated list.
    Supports numbers like '101, 102, 103' or block-included like 'A-101, B-102'.
    """
    block = (block or "A").strip()
    capacity = int(default_capacity or 2)
    if capacity <= 0:
        raise ValueError("Room capacity must be greater than 0.")

    tokens = re.split(r"[,;\n\r\t]+", text or "")
    created = []
    skipped = []
    errors = []

    for token in tokens:
        token = token.strip()
        if not token:
            continue

        b = block
        num = token
        if "-" in token:
            b_part, _, n_part = token.partition("-")
            b = b_part.strip() or block
            num = n_part.strip()

        if Room.objects.filter(block__iexact=b, number__iexact=num).exists():
            skipped.append(f"{b}-{num}")
            continue

        try:
            r = Room(block=b, number=num, capacity=capacity)
            r.full_clean()
            r.save()
            created.append(r)
        except Exception as e:
            errors.append(f"Room {b}-{num}: {getattr(e, 'messages', [str(e)])[0]}")

    return {
        "created": created,
        "skipped": skipped,
        "errors": errors,
    }


def import_rooms_csv(rows):
    """
    Imports rooms from CSV rows dict.
    Headers supported: block, number (or room), capacity.
    """
    created = []
    skipped = []
    errors = []

    for i, raw_r in enumerate(rows, start=2):
        r = {_normalize_key(k): (v or "").strip() for k, v in raw_r.items() if k}
        block = r.get("block", "")
        number = r.get("number", "") or r.get("room", "")
        cap_val = r.get("capacity", "") or "2"

        if "-" in number and not block:
            b, _, n = number.partition("-")
            block = b.strip()
            number = n.strip()

        block = block or "A"

        if not number:
            errors.append(f"Row {i}: Missing room number.")
            continue

        try:
            capacity = int(cap_val)
            if capacity <= 0:
                raise ValueError("Capacity must be positive.")
        except ValueError:
            errors.append(f"Row {i}: Invalid capacity '{cap_val}'.")
            continue

        if Room.objects.filter(block__iexact=block, number__iexact=number).exists():
            skipped.append(f"{block}-{number}")
            continue

        try:
            r = Room(block=block, number=number, capacity=capacity)
            r.full_clean()
            r.save()
            created.append(r)
        except Exception as e:
            errors.append(f"Row {i} ({block}-{number}): {getattr(e, 'messages', [str(e)])[0]}")

    return {
        "created": created,
        "skipped": skipped,
        "errors": errors,
    }

