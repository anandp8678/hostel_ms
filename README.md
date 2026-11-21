# Hostel Management System

Django + SQLite + Bootstrap 5. Roles: Super Admin, Warden, Student, Parent.

## Setup
```
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo        # optional demo data + test accounts
python manage.py runserver
```
Open http://127.0.0.1:8000/

## Test accounts (from seed_demo)
| Role | Username | Password |
|---|---|---|
| Super Admin | admin | Admin@123 |
| Warden | warden | Warden@123 |
| Student | CS2101 (roll number) | Student@123 |
| Parent | p9876000000 (p + phone) | Parent@123 |

## All-In-One Combined CSV Import (Students, Parents & Room Allocations)
Students > Import CSV (or Allocations > Import CSV).
Upload **one combined CSV** containing all information:
- Columns: `name, roll_no, course, phone, parent_name, parent_phone, room, start_date`
  - `name`: Student's full name
  - `roll_no`: Roll number / username (required)
  - `course`: Course or department
  - `phone`: Student contact phone
  - `parent_name`: Parent or guardian name
  - `parent_phone`: Parent phone (creates parent login `p + phone`)
  - `room`: Room code (e.g. `A-101`) to automatically allocate
  - `start_date`: Date of room allocation (e.g. `2026-11-21`, defaults to today)
- Creates new students and parents, updates existing profiles, and allocates rooms in a single upload.
- Bad rows are skipped and reported; good rows are saved.
- Sample template file available: `student_allocations_template.csv`.

## Room Management, Filtering & Bulk Actions
Rooms page (`/rooms/`):
- **Filters**:
  - Filter by **Block** (All, A, B, etc.)
  - Filter by **Status / Cause**: Available / Vacant, Fully Occupied, Completely Empty, Active Only, All Blacklisted, or specific causes (Maintenance, Renovation, Repair, etc.)
  - Filter by **Capacity** (1, 2, 3, 4+ beds)
  - Search by room number, block, or cause keyword
- **Select All & Selection Checkboxes**:
  - Checkbox per room row and a master "Select All" checkbox in the header.
  - Live selection counter (e.g. `X selected`).
- **Bulk Actions**:
  - **🚫 Blacklist**: Mark selected rooms as blacklisted due to specific causes (Maintenance, Renovation, Repair, Quarantine, etc.). Prevents new allocations.
  - **🟢 Activate**: Re-activate selected rooms and clear blacklist status.
  - **🛏 Change Capacity**: Update bed capacity across all selected rooms in bulk.
  - **🗑 Delete**: Delete selected rooms in bulk (with built-in protection preventing deletion if active students are living in them).
- **1-Click Quick Toggle**: Quick button to toggle Blacklist/Active on any room row.

## Bulk Room Creation
Rooms > Bulk Add Rooms. Create any quantity of rooms at once instead of adding them one by one:
- **By Amount / Count**: Enter Block (e.g. `A`), Starting Room Number (e.g. `101`), Amount of Rooms (e.g. `20`), and Capacity (e.g. `2`) to instantly generate rooms `A-101` through `A-120`.
- **Multi-Floor Generator**: Specify floor range (e.g. Floors 1 to 3) and rooms per floor (e.g. 10) to generate all floors at once (`101-110, 201-210, 301-310`).
- **Room List**: Paste a comma or newline-separated list of room numbers (e.g. `101, 102, 103, 104`).
- **CSV Upload**: Upload a CSV with `block, number, capacity` columns with a downloadable template.
- Existing rooms are safely detected and skipped without error.

## Bulk Room Allocation via CSV
Allocations > Import CSV. Upload a CSV of student roll numbers and allocated rooms:
- Columns: `roll_no, room` (e.g. `CS2101, A-101`) or optionally `start_date`, `name`.
- Automatically assigns students to their rooms.
- If a student is already staying in another room, their prior room is marked as vacated and they are allocated to the new room.
- Room capacity is verified; full rooms are flagged.
- If an unregistered student is included with name and roll number, they are created and allocated automatically.
- Downloadable sample template available on the page.

## Low-effort warden workflow
- Attendance: everyone defaults to Present (or On leave if approved); change only exceptions, save once.
- Leave: one-click approve/reject. Fees: add a fee for all students in one form; record payments as received.
- Reports: occupancy, pending fees, lowest attendance, CSV download.
- Super Admin adds wardens under Wardens. Payments are recorded by staff only (no online payment).

## Supabase / hosted Postgres (optional)
```
pip install dj-database-url "psycopg[binary]"
export DATABASE_URL="postgresql://user:pass@host:5432/postgres"
python manage.py migrate
```
Without `DATABASE_URL` the app uses SQLite (`db.sqlite3`).

## Before going live
Set `DEBUG=False`, a real `SECRET_KEY` and `ALLOWED_HOSTS` in `hostel/settings.py`.
