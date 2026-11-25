from datetime import date, datetime
import calendar
from decimal import Decimal
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Building, Fee, FeeConfig, Parent, Room, RoomAllocation, Student, User


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
    if clean in ("building", "buildingcode", "buildingname", "hostelbuilding", "wing"):
        return "building"
    if clean in ("year", "yearofstudy", "academicyear", "studyyear", "yr"):
        return "year_of_study"
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


def parse_year(val, default=1):
    """Parse academic year strings such as '1', '1st Year', '2', 'Second Year' into 1..4."""
    if val is None or str(val).strip() == "":
        return default
    s = str(val).strip().lower()
    if s in ("1", "1st", "first", "1st year", "first year", "i"):
        return 1
    if s in ("2", "2nd", "second", "2nd year", "second year", "ii"):
        return 2
    if s in ("3", "3rd", "third", "3rd year", "third year", "iii"):
        return 3
    if s in ("4", "4th", "fourth", "4th year", "fourth year", "iv"):
        return 4
    digits = re.search(r"[1-4]", s)
    if digits:
        return int(digits.group())
    raise ValueError(f"Invalid academic year '{val}'. Must be 1, 2, 3, or 4.")


def find_building(val=""):
    """Resolve a Building instance from a Building object, pk, code, or name."""
    if not val:
        return None
    if isinstance(val, Building):
        return val
    s = str(val).strip()
    if not s:
        return None
    if s.isdigit():
        b = Building.objects.filter(pk=int(s)).first()
        if b:
            return b
    b = Building.objects.filter(code__iexact=s).first()
    if b:
        return b
    return Building.objects.filter(name__iexact=s).first()


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


def _pick_unambiguous_room(qs, student_year=None):
    """Return single matching room, using student_year compatibility to disambiguate across buildings."""
    cnt = qs.count()
    if cnt == 1:
        return qs.first()
    if cnt > 1 and student_year is not None:
        compatible = [r for r in qs.select_related("building") if not r.building_id or r.building.allows_year(student_year)]
        if len(compatible) == 1:
            return compatible[0]
    return None


def find_room(room_str="", block_str="", building=None, student_year=None):
    room_str = str(room_str or "").strip()
    block_str = str(block_str or "").strip()
    b_obj = find_building(building)

    # Support composite room formats like "B1 · G-101" or "B1/G-101"
    for sep in ("·", "/"):
        if sep in room_str:
            b_part, _, rest = room_str.partition(sep)
            maybe_b = find_building(b_part.strip())
            if maybe_b:
                b_obj = maybe_b
                room_str = rest.strip()
                break

    qs = Room.objects.select_related("building")
    if b_obj is not None:
        qs = qs.filter(building=b_obj)

    if block_str and room_str:
        r = _pick_unambiguous_room(qs.filter(block__iexact=block_str, number__iexact=room_str), student_year)
        if r:
            return r

    if not room_str:
        return None

    # Handle "B1-G-101" (3-part prefix)
    if room_str.count("-") >= 2:
        first_part, _, rem = room_str.partition("-")
        maybe_b = find_building(first_part.strip())
        if maybe_b:
            b_part, _, num_part = rem.partition("-")
            r = _pick_unambiguous_room(
                Room.objects.filter(building=maybe_b, block__iexact=b_part.strip(), number__iexact=num_part.strip()),
                student_year,
            )
            if r:
                return r

    if "-" in room_str:
        b, _, n = room_str.partition("-")
        r = _pick_unambiguous_room(qs.filter(block__iexact=b.strip(), number__iexact=n.strip()), student_year)
        if r:
            return r

    if " " in room_str:
        b, n = room_str.split(None, 1)
        r = _pick_unambiguous_room(qs.filter(block__iexact=b.strip(), number__iexact=n.strip()), student_year)
        if r:
            return r

    m = re.match(r"^([a-zA-Z]+)[ -]?(\w+)$", room_str)
    if m:
        b, n = m.group(1), m.group(2)
        r = _pick_unambiguous_room(qs.filter(block__iexact=b, number__iexact=n), student_year)
        if r:
            return r

    r = _pick_unambiguous_room(qs.filter(number__iexact=room_str), student_year)
    if r:
        return r

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

    if room.building_id and not room.building.allows_year(student.year_of_study):
        raise ValidationError(
            f"Anti-ragging policy violation: {student.year_label} students cannot be allocated to "
            f"{room.building.name} (restricted to {room.building.allowed_years_display})."
        )

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
def onboard_student(name, roll_no, course="", phone="", parent_name="", parent_phone="", room=None, start_date=None, year_of_study=1):
    """Create the student login, the parent login (shared between siblings) and optionally a room."""
    roll_no = (roll_no or "").strip()
    name = (name or "").strip()
    if not name or not roll_no:
        raise ValueError("Name and roll number are required.")
    if User.objects.filter(username=roll_no).exists():
        raise ValueError(f"Roll number {roll_no} already exists.")
    yr = parse_year(year_of_study, default=1)
    first, last = _split(name)
    user = User.objects.create_user(username=roll_no, password=settings.DEFAULT_PASSWORD, first_name=first,
                                    last_name=last, role="student", phone=phone, must_change_password=True)
    student = Student.objects.create(user=user, roll_no=roll_no, course=course, year_of_study=yr)
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
    Columns: name, roll_no, course, year_of_study, phone, parent_name, parent_phone, building, room, start_date.
    Handles:
    - New students + parents (with academic year)
    - Existing students updating personal / parent / year details
    - Room allocation (with anti-ragging building validation, capacity validation, and vacating previous room)
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
        building_str = r.get("building", "")
        course = r.get("course", "")
        year_raw = r.get("year_of_study", "")
        phone = r.get("phone", "")
        parent_name = r.get("parent_name", "")
        parent_phone = r.get("parent_phone", "")
        start_date = parse_date(r.get("start_date"))

        if not roll_no and not name:
            errors.append(f"Row {i}: Missing student roll number or name.")
            continue

        try:
            with transaction.atomic():
                year_val = parse_year(year_raw, default=1) if year_raw else None
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
                    if year_val is not None:
                        student.year_of_study = year_val
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
                        year_of_study=year_val if year_val is not None else 1,
                    )
                    created_count += 1

                # Handle room allocation if room is provided
                if room_str:
                    room = find_room(room_str, block_str, building=building_str, student_year=student.year_of_study)
                    if not room:
                        raise Room.DoesNotExist(f"Room '{room_str}' not found (use format A-101 or B1 · G-101).")
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


def _create_room_if_absent(building, block, number, capacity, created, skipped, errors, row_prefix=""):
    """Create a Room scoped to (building, block, number) or append to skipped/errors."""
    b_obj = find_building(building)
    exists_qs = Room.objects.filter(building=b_obj, block__iexact=block, number__iexact=number)
    label = f"{b_obj.code} · {block}-{number}" if b_obj else f"{block}-{number}"
    if exists_qs.exists():
        skipped.append(label)
        return
    try:
        r = Room(building=b_obj, block=block, number=number, capacity=capacity)
        r.full_clean()
        r.save()
        created.append(r)
    except Exception as e:
        prefix = f"{row_prefix} ({label})" if row_prefix else f"Room {label}"
        errors.append(f"{prefix}: {getattr(e, 'messages', [str(e)])[0]}")


def bulk_create_rooms(block="A", start_number="101", count=None, end_number=None, capacity=2, prefix="", suffix="", building=None):
    """
    Creates a particular amount (count) or range of rooms for a given block and building.
    Block serves as the floor prefix (e.g. G and room 101 displays like B1 · G-101).
    """
    block = (block or "A").strip()
    start_str = str(start_number or "").strip()
    prefix = (prefix or "").strip()
    suffix = (suffix or "").strip()
    b_obj = find_building(building)

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

    created, skipped, errors = [], [], []
    for num in range(start_int, end_int + 1):
        num_str = f"{num:0{pad}d}" if pad > 0 else str(num)
        full_number = f"{prefix}{num_str}{suffix}"
        _create_room_if_absent(b_obj, block, full_number, capacity, created, skipped, errors)

    return {"created": created, "skipped": skipped, "errors": errors}


def bulk_create_rooms_floors(block="A", start_floor=1, end_floor=3, rooms_per_floor=10, capacity=2, floor_padding=2, building=None):
    """
    Creates rooms floor-by-floor inside a given building.
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

    b_obj = find_building(building)
    created, skipped, errors = [], [], []

    for fl in range(start_fl, end_fl + 1):
        for rm in range(1, r_per_fl + 1):
            num_str = f"{fl}{rm:0{floor_padding}d}"
            _create_room_if_absent(b_obj, block, num_str, capacity, created, skipped, errors)

    return {"created": created, "skipped": skipped, "errors": errors}


def bulk_create_rooms_from_list(block="A", text="", default_capacity=2, building=None):
    """
    Creates rooms from a comma, semicolon, or newline separated list.
    Supports numbers like '101, 102, 103' or block-included like 'A-101, B-102'.
    """
    block = (block or "A").strip()
    capacity = int(default_capacity or 2)
    if capacity <= 0:
        raise ValueError("Room capacity must be greater than 0.")

    b_obj = find_building(building)
    tokens = re.split(r"[,;\n\r\t]+", text or "")
    created, skipped, errors = [], [], []

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
        _create_room_if_absent(b_obj, b, num, capacity, created, skipped, errors)

    return {"created": created, "skipped": skipped, "errors": errors}


def import_rooms_csv(rows, building=None):
    """
    Imports rooms from CSV rows dict.
    Headers supported: building (optional), block, number (or room), capacity.
    """
    default_b = find_building(building)
    created, skipped, errors = [], [], []

    for i, raw_r in enumerate(rows, start=2):
        r = {_normalize_key(k): (v or "").strip() for k, v in raw_r.items() if k}
        block = r.get("block", "")
        number = r.get("number", "") or r.get("room", "")
        cap_val = r.get("capacity", "") or "2"
        row_b = find_building(r.get("building", "")) if r.get("building") else default_b

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

        _create_room_if_absent(row_b, block, number, capacity, created, skipped, errors, row_prefix=f"Row {i}")

    return {"created": created, "skipped": skipped, "errors": errors}



def calculate_student_monthly_fee(student, year, month, config=None):
    """
    Calculates monthly hostel and mess fee for a student.
    - Fixed rent: default ₹1500 (from config)
    - Food charge: days_in_month * food_rate_per_day (default ₹130/day)
    - Mess cut: checks consecutive absent days.
      If absent for >= mess_cut_min_days (default 3 consecutive days),
      reduces mess_cut_rate_per_day (default ₹130, food amount for one day).
    All parameters are fully configurable via FeeConfig and editable by warden.
    """
    if config is None:
        config = FeeConfig.get_config()

    _, num_days = calendar.monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, num_days)

    # Attendance map for the month
    att_qs = student.attendance.filter(date__gte=start_date, date__lte=end_date)
    att_map = {a.date: a.status for a in att_qs}

    # Detect absent streaks
    absent_types = {"A"}
    if config.count_leaves:
        absent_types.add("L")

    all_streaks = []
    current_streak = []

    for day_num in range(1, num_days + 1):
        cur_date = date(year, month, day_num)
        st = att_map.get(cur_date)
        if st in absent_types:
            current_streak.append(cur_date)
        else:
            if current_streak:
                all_streaks.append(current_streak)
                current_streak = []
    if current_streak:
        all_streaks.append(current_streak)

    min_days = max(1, config.mess_cut_min_days)
    qualifying_streaks = [s for s in all_streaks if len(s) >= min_days]
    under_threshold_streaks = [s for s in all_streaks if len(s) < min_days]

    # Calculate mess cut days
    if config.mess_cut_mode == "per_streak":
        # 1 day cut per 3 consecutive absent days
        mess_cut_days = sum(len(s) // min_days for s in qualifying_streaks)
    else:
        # Full cut for every day in a qualifying consecutive streak
        mess_cut_days = sum(len(s) for s in qualifying_streaks)

    rent_amount = Decimal(str(config.rent_amount))
    food_rate = Decimal(str(config.food_rate_per_day))
    food_amount = Decimal(str(round(Decimal(num_days) * food_rate, 2)))
    mess_cut_rate = Decimal(str(config.mess_cut_rate_per_day))
    mess_cut_amount = Decimal(str(round(Decimal(mess_cut_days) * mess_cut_rate, 2)))
    total_amount = max(Decimal("0.00"), rent_amount + food_amount - mess_cut_amount)

    qualifying_details = []
    for s in qualifying_streaks:
        if len(s) == 1:
            qualifying_details.append(f"{s[0].strftime('%d %b')}")
        else:
            qualifying_details.append(f"{s[0].strftime('%d %b')}–{s[-1].strftime('%d %b')} ({len(s)} consecutive days)")

    under_threshold_details = []
    for s in under_threshold_streaks:
        if len(s) == 1:
            under_threshold_details.append(f"{s[0].strftime('%d %b')}")
        else:
            under_threshold_details.append(f"{s[0].strftime('%d %b')}–{s[-1].strftime('%d %b')} ({len(s)} days)")

    return {
        "year": year,
        "month": month,
        "month_str": f"{year:04d}-{month:02d}",
        "month_label": date(year, month, 1).strftime("%B %Y"),
        "num_days": num_days,
        "rent_amount": rent_amount,
        "food_rate": food_rate,
        "food_amount": food_amount,
        "total_absent_days": sum(len(s) for s in all_streaks),
        "qualifying_streaks": qualifying_streaks,
        "qualifying_details": qualifying_details,
        "under_threshold_details": under_threshold_details,
        "mess_cut_days": mess_cut_days,
        "mess_cut_rate": mess_cut_rate,
        "mess_cut_amount": mess_cut_amount,
        "total_amount": total_amount,
    }


def notify_user(recipient, title, message, category="general", link="", actor=None):
    from .models import Notification
    if not recipient:
        return None
    return Notification.objects.create(
        recipient=recipient,
        actor=actor,
        category=category,
        title=title,
        message=message,
        link=link,
    )


def notify_users(recipients, title, message, category="general", link="", actor=None):
    from .models import Notification
    objs = []
    seen = set()
    for r in recipients:
        if r and r.pk not in seen:
            seen.add(r.pk)
            objs.append(
                Notification(
                    recipient=r,
                    actor=actor,
                    category=category,
                    title=title,
                    message=message,
                    link=link,
                )
            )
    if objs:
        return Notification.objects.bulk_create(objs)
    return []

