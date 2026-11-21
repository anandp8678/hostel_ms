import csv
from datetime import date, timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from . import forms as f
from . import models as m
from .services import (
    bulk_create_rooms,
    bulk_create_rooms_floors,
    bulk_create_rooms_from_list,
    import_allocations,
    import_rooms_csv,
    import_rows,
)


def decode_csv_file(file_obj):
    raw = file_obj.read()
    try:
        return raw.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        return raw.decode("latin-1").splitlines()

STAFF = ("admin", "warden")
BADGE = {"Paid": "success", "Resolved": "success", "Approved": "success", "Present": "success", "Unpaid": "secondary",
         "Submitted": "secondary", "Pending": "warning", "Under Review": "info", "In Progress": "primary",
         "Partly paid": "info", "Overdue": "danger", "Rejected": "danger", "Absent": "danger", "On leave": "info"}


def badge(t):
    return format_html('<span class="badge text-bg-{}">{}</span>', BADGE.get(str(t), "secondary"), t)


def role_required(*roles):
    def deco(view):
        @wraps(view)
        @login_required
        def inner(request, *a, **k):
            if request.user.role not in roles:
                raise PermissionDenied
            return view(request, *a, **k)
        return inner
    return deco


staff = role_required(*STAFF)


def show(request, template, **ctx):
    return render(request, f"core/{template}.html", ctx)


class PwChange(PasswordChangeView):
    template_name = "core/form.html"
    success_url = reverse_lazy("home")
    extra_context = {"title": "Choose a new password", "hide_nav": True}

    def form_valid(self, form):
        r = super().form_valid(form)
        self.request.user.must_change_password = False
        self.request.user.save(update_fields=["must_change_password"])
        return r

    def get_form(self, *a, **k):
        form = super().get_form(*a, **k)
        for fld in form.fields.values():
            fld.widget.attrs["class"] = "form-control"
        return form


# ---------- generic CRUD ----------
def crud(key, model, form_class, title, columns, roles=STAFF, create=True, delete=True, qs=None, actions=None, add_label="Add"):
    def objs():
        return qs() if qs else model.objects.all()

    @role_required(*roles)
    def lst(request):
        rows = []
        for o in objs():
            rows.append({"cells": [c(o) for _, c in columns], "edit": reverse(f"{key}_edit", args=[o.pk]),
                         "delete": reverse(f"{key}_delete", args=[o.pk]) if delete else None,
                         "actions": actions(o) if actions else []})
        return show(request, "list", title=title, headers=[h for h, _ in columns], rows=rows,
                    add=reverse(f"{key}_new") if create else None, add_label=add_label)

    @role_required(*roles)
    def edit(request, pk=None):
        obj = get_object_or_404(model, pk=pk) if pk else None
        form = form_class(request.POST or None, instance=obj)
        if request.method == "POST" and form.is_valid():
            form.save()
            messages.success(request, "Saved.")
            return redirect(key)
        return show(request, "form", title=(f"Edit {title.lower()}" if obj else f"{add_label} · {title}"), form=form, back=reverse(key))

    @role_required(*roles)
    def remove(request, pk):
        obj = get_object_or_404(model, pk=pk)
        if request.method == "POST":
            obj.delete()
            messages.success(request, "Deleted.")
            return redirect(key)
        return show(request, "confirm", title=f"Delete {obj}?", back=reverse(key))

    return {"list": lst, "new": edit, "edit": edit, "delete": remove}


def date_s(d):
    return d.strftime("%d %b %Y") if d else "—"


def alloc_actions(a):
    return [("Vacate", reverse("allocation_vacate", args=[a.pk]), True, "Mark this student as moved out?")] if a.is_active else []


def fee_actions(fee):
    return [("Record payment", reverse("payment_new", args=[fee.pk]), False, "")] if fee.balance > 0 else []


V = {}
V["students"] = crud("students", m.Student, f.StudentEditForm, "Students", [
    ("Roll no.", lambda s: s.roll_no), ("Name", lambda s: s.user.display_name), ("Course", lambda s: s.course or "—"),
    ("Room", lambda s: s.room or "—"), ("Parent", lambda s: s.parent or "—"),
    ("Status", lambda s: "Staying" if s.is_active else "Left")],
    qs=lambda: m.Student.objects.select_related("user", "parent__user"), add_label="Add student")
V["rooms"] = crud("rooms", m.Room, f.RoomForm, "Rooms", [
    ("Room", str), ("Capacity", lambda r: r.capacity), ("Occupied", lambda r: r.occupied), ("Vacant", lambda r: r.vacancy)])
V["allocations"] = crud("allocations", m.RoomAllocation, f.AllocationForm, "Room allocations", [
    ("Student", lambda a: a.student), ("Room", lambda a: a.room), ("From", lambda a: date_s(a.start_date)),
    ("To", lambda a: date_s(a.end_date) if a.end_date else badge("Active"))],
    qs=lambda: m.RoomAllocation.objects.select_related("student__user", "room"), actions=alloc_actions, delete=False, add_label="Allocate")
V["complaints"] = crud("complaints", m.Complaint, f.ComplaintStaffForm, "Complaints", [
    ("Student", lambda c: c.student), ("Title", lambda c: c.title), ("Details", lambda c: c.description[:80]),
    ("Status", lambda c: badge(c.status)), ("Filed", lambda c: date_s(c.created_at))],
    qs=lambda: m.Complaint.objects.select_related("student__user"), create=False, delete=False)
V["menu_manage"] = crud("menu_manage", m.MealMenu, f.MenuForm, "Meal menu", [
    ("Day", lambda x: x.get_day_display()), ("Meal", lambda x: x.get_meal_display()), ("Items", lambda x: x.items)])
V["fees"] = crud("fees", m.Fee, f.FeeForm, "Fees", [
    ("Student", lambda x: x.student), ("Fee", lambda x: x.title), ("Amount", lambda x: x.amount), ("Paid", lambda x: x.paid),
    ("Balance", lambda x: x.balance), ("Due", lambda x: date_s(x.due_date)), ("Status", lambda x: badge(x.status))],
    qs=lambda: m.Fee.objects.select_related("student__user").prefetch_related("payments"), actions=fee_actions, add_label="Add fee")
V["announcements"] = crud("announcements", m.Announcement, f.AnnouncementForm, "Announcements", [
    ("Title", lambda a: a.title), ("For", lambda a: a.get_audience_display()), ("Posted", lambda a: date_s(a.created_at))],
    add_label="Post")
V["wardens"] = crud("wardens", m.User, f.WardenEditForm, "Wardens", [
    ("Name", lambda u: u.display_name), ("Username", lambda u: u.username), ("Phone", lambda u: u.phone or "—")],
    roles=("admin",), qs=lambda: m.User.objects.filter(role="warden"), add_label="Add warden")

# the generic edit for wardens/fees needs special create logic; override below


@role_required("admin")
def warden_new(request):
    form = f.WardenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        first, _, last = d["name"].partition(" ")
        from django.conf import settings
        m.User.objects.create_user(username=d["username"], password=settings.DEFAULT_PASSWORD, first_name=first, last_name=last,
                                   role="warden", phone=d["phone"], is_staff=True, must_change_password=True)
        messages.success(request, "Warden created with the default password; they must change it on first login.")
        return redirect("wardens")
    return show(request, "form", title="Add warden", form=form, back=reverse("wardens"))


@staff
def fee_create(request):
    form = f.FeeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        targets = [d["student"]] if d["student"] else list(m.Student.objects.filter(is_active=True))
        m.Fee.objects.bulk_create([m.Fee(student=s, title=d["title"], amount=d["amount"], due_date=d["due_date"]) for s in targets])
        messages.success(request, f"Fee added for {len(targets)} student(s).")
        return redirect("fees")
    return show(request, "form", title="Add fee", form=form, back=reverse("fees"))


@staff
def payment_new(request, fee_id):
    fee = get_object_or_404(m.Fee, pk=fee_id)
    form = f.PaymentForm(request.POST or None, initial={"amount": fee.balance})
    form.instance.fee = fee
    if request.method == "POST" and form.is_valid():
        p = form.save(commit=False)
        p.recorded_by = request.user
        p.save()
        messages.success(request, "Payment recorded.")
        return redirect("fees")
    return show(request, "form", title=f"Record payment · {fee.student} · {fee.title} (balance {fee.balance})", form=form, back=reverse("fees"))


@staff
@require_POST
def allocation_vacate(request, pk):
    a = get_object_or_404(m.RoomAllocation, pk=pk)
    a.end_date = date.today()
    a.save()
    messages.success(request, "Room vacated.")
    return redirect("allocations")


# ---------- auth / home ----------
@login_required
def home(request):
    u = request.user
    if u.role in STAFF:
        students = m.Student.objects.filter(is_active=True)
        beds = sum(r.capacity for r in m.Room.objects.all())
        occ = m.RoomAllocation.objects.filter(end_date__isnull=True).count()
        today = date.today()
        marked = m.Attendance.objects.filter(date=today).count()
        stats = [
            ("Students staying", students.count(), "students"), ("Beds free", beds - occ, "rooms"),
            ("Leave requests waiting", m.LeaveRequest.objects.filter(status="Pending").count(), "leaves"),
            ("Open complaints", m.Complaint.objects.exclude(status="Resolved").count(), "complaints"),
            ("Attendance marked today", f"{marked}/{students.count()}", "attendance"),
            ("Fees overdue", sum(1 for x in m.Fee.objects.filter(due_date__lt=today) if x.balance > 0), "fees"),
        ]
        return show(request, "dashboard", stats=stats)
    if u.role == "student":
        return show(request, "overview", **overview(u.student, u))
    children = u.parent.children.select_related("user") if hasattr(u, "parent") else []
    if len(children) == 1:
        return redirect("child", children[0].pk)
    return show(request, "children", children=children)


def overview(s, user):
    today = date.today()
    aud = ["all", "student" if user.role == "student" else "parent"]
    menu = m.MealMenu.objects.filter(day=today.weekday())
    return dict(s=s, room=s.room, pct=s.attendance_percent(), balance=s.fee_balance, menu=menu,
                complaints=s.complaints.exclude(status="Resolved").count(),
                recent=s.attendance.all()[:10], fees=s.fees.prefetch_related("payments"),
                notices=m.Announcement.objects.filter(audience__in=aud)[:5], badge=badge)


@role_required("parent")
def child(request, pk):
    s = get_object_or_404(m.Student, pk=pk, parent__user=request.user)
    return show(request, "overview", parent_view=True, siblings=request.user.parent.children.all(), **overview(s, request.user))


# ---------- staff workflows ----------
@staff
def student_new(request):
    form = f.StudentAddForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            s = form.save()
        except ValidationError as e:
            form.add_error(None, e)
        else:
            messages.success(request, f"{s} added. Login: roll number / default password (they must change it on first login).")
            return redirect("students")
    return show(request, "form", title="Add student", form=form, back=reverse("students"),
                note="A parent login is created automatically from the parent phone number (siblings share one parent login).")


@staff
def student_import(request):
    form = f.ImportForm(request.POST or None, request.FILES or None)
    result = None
    if request.method == "POST" and form.is_valid():
        try:
            text = decode_csv_file(request.FILES["file"])
            result = import_rows(csv.DictReader(text))
        except Exception as e:
            form.add_error("file", f"Could not read file: {e}")
    return show(request, "import", form=form, result=result)


@staff
def import_template(request):
    r = HttpResponse("name,roll_no,course,phone,parent_name,parent_phone,room,start_date\n"
                     "Asha Nair,CS2101,B.Tech CSE,9876500001,Mohan Nair,9876500002,A-101,2026-11-21\n"
                     "Rahul Menon,CS2102,B.Tech CSE,9876500003,Suresh Menon,9876500004,A-101,2026-11-21\n"
                     "Fatima Beevi,CS2103,B.Tech ECE,9876500005,Ali Beevi,9876500006,B-102,2026-11-21\n",
                     content_type="text/csv")
    r["Content-Disposition"] = 'attachment; filename="students_and_allocations_template.csv"'
    return r


@staff
def allocation_import(request):
    form = f.AllocationImportForm(request.POST or None, request.FILES or None)
    result = None
    if request.method == "POST" and form.is_valid():
        try:
            text = decode_csv_file(request.FILES["file"])
            result = import_allocations(csv.DictReader(text))
        except Exception as e:
            form.add_error("file", f"Could not read file: {e}")
    return show(request, "allocation_import", form=form, result=result)


@staff
def allocation_import_template(request):
    r = HttpResponse("name,roll_no,course,phone,parent_name,parent_phone,room,start_date\n"
                     "Asha Nair,CS2101,B.Tech CSE,9876500001,Mohan Nair,9876500002,A-101,2026-11-21\n"
                     "Rahul Menon,CS2102,B.Tech CSE,9876500003,Suresh Menon,9876500004,A-101,2026-11-21\n",
                     content_type="text/csv")
    r["Content-Disposition"] = 'attachment; filename="students_and_allocations_template.csv"'
    return r


@staff
def room_bulk_add(request):
    result = None
    form = f.RoomBulkAddForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        mode = form.cleaned_data.get("mode") or request.POST.get("mode", "count")
        d = form.cleaned_data
        try:
            if mode == "count":
                block = d.get("block") or "A"
                start_no = d.get("start_number") or "101"
                count = d.get("count")
                end_no = d.get("end_number") or None
                cap = d.get("capacity") or 2
                prefix = d.get("prefix") or ""
                result = bulk_create_rooms(
                    block=block,
                    start_number=start_no,
                    count=count if not end_no else None,
                    end_number=end_no,
                    capacity=cap,
                    prefix=prefix,
                )
            elif mode == "floor":
                block = d.get("block") or "A"
                start_fl = d.get("start_floor") or 1
                end_fl = d.get("end_floor") or 1
                r_per_fl = d.get("rooms_per_floor") or 10
                cap = d.get("capacity") or 2
                result = bulk_create_rooms_floors(
                    block=block,
                    start_floor=start_fl,
                    end_floor=end_fl,
                    rooms_per_floor=r_per_fl,
                    capacity=cap,
                )
            elif mode == "list":
                block = d.get("block") or "A"
                text = d.get("room_list") or ""
                cap = d.get("capacity") or 2
                result = bulk_create_rooms_from_list(
                    block=block,
                    text=text,
                    default_capacity=cap,
                )
            elif mode == "csv":
                if "file" not in request.FILES:
                    raise ValueError("Please select a CSV file to upload.")
                text = decode_csv_file(request.FILES["file"])
                result = import_rooms_csv(csv.DictReader(text))
        except Exception as e:
            form.add_error(None, f"{getattr(e, 'messages', [str(e)])[0]}")

    return show(request, "room_bulk_add", form=form, result=result)


@staff
def room_bulk_template(request):
    r = HttpResponse("block,number,capacity\nA,101,2\nA,102,2\nB,201,3\n",
                     content_type="text/csv")
    r["Content-Disposition"] = 'attachment; filename="rooms_template.csv"'
    return r


@staff
def room_list(request):
    if request.method == "POST":
        action = request.POST.get("bulk_action", "").strip()
        selected_ids = request.POST.getlist("selected_rooms")
        if not selected_ids:
            messages.warning(request, "No rooms were selected.")
            return redirect(request.get_full_path())

        rooms_qs = m.Room.objects.filter(pk__in=selected_ids)

        if action == "blacklist":
            cause = request.POST.get("blacklist_cause", "").strip() or "Maintenance"
            count = rooms_qs.update(is_blacklisted=True, blacklist_reason=cause)
            messages.success(request, f"{count} room(s) marked as blacklisted (cause: {cause}).")
        elif action == "activate":
            count = rooms_qs.update(is_blacklisted=False, blacklist_reason="")
            messages.success(request, f"{count} room(s) marked as active.")
        elif action == "delete":
            occupied = [r for r in rooms_qs if r.occupied > 0]
            if occupied:
                names = ", ".join(str(r) for r in occupied[:5])
                messages.error(request, f"Cannot delete: {len(occupied)} room(s) ({names}) currently have active students living in them. Vacate them first.")
            else:
                count = rooms_qs.count()
                rooms_qs.delete()
                messages.success(request, f"{count} room(s) deleted successfully.")
        elif action == "change_capacity":
            try:
                new_cap = int(request.POST.get("new_capacity", 2))
                if new_cap <= 0:
                    raise ValueError
                updated = 0
                for r in rooms_qs:
                    if r.occupied > new_cap:
                        messages.warning(request, f"Room {r} already has {r.occupied} students; capacity cannot be lower.")
                    else:
                        r.capacity = new_cap
                        r.save(update_fields=["capacity"])
                        updated += 1
                messages.success(request, f"Capacity updated for {updated} room(s).")
            except ValueError:
                messages.error(request, "Please enter a valid positive capacity number.")
        else:
            messages.warning(request, "Unknown action.")

        return redirect(request.get_full_path())

    q = request.GET.get("q", "").strip()
    block = request.GET.get("block", "").strip()
    status = request.GET.get("status", "").strip()
    cause = request.GET.get("cause", "").strip()
    capacity = request.GET.get("capacity", "").strip()

    rooms_qs = m.Room.objects.all().prefetch_related("allocations")

    if q:
        rooms_qs = rooms_qs.filter(Q(number__icontains=q) | Q(block__icontains=q) | Q(blacklist_reason__icontains=q))

    if block:
        rooms_qs = rooms_qs.filter(block__iexact=block)

    if capacity:
        try:
            rooms_qs = rooms_qs.filter(capacity=int(capacity))
        except ValueError:
            pass

    if cause:
        rooms_qs = rooms_qs.filter(is_blacklisted=True, blacklist_reason__icontains=cause)
    elif status == "blacklisted":
        rooms_qs = rooms_qs.filter(is_blacklisted=True)
    elif status == "active":
        rooms_qs = rooms_qs.filter(is_blacklisted=False)
    elif status == "maintenance":
        rooms_qs = rooms_qs.filter(is_blacklisted=True, blacklist_reason__icontains="maintenance")
    elif status == "renovation":
        rooms_qs = rooms_qs.filter(is_blacklisted=True, blacklist_reason__icontains="renovation")
    elif status == "repair":
        rooms_qs = rooms_qs.filter(is_blacklisted=True, blacklist_reason__icontains="repair")

    rooms = list(rooms_qs)

    if status == "vacant":
        rooms = [r for r in rooms if r.vacancy > 0 and not r.is_blacklisted]
    elif status == "full":
        rooms = [r for r in rooms if r.raw_vacancy == 0 and not r.is_blacklisted]
    elif status == "empty":
        rooms = [r for r in rooms if r.occupied == 0 and not r.is_blacklisted]

    all_blocks = sorted(list(m.Room.objects.values_list("block", flat=True).distinct()))
    all_causes = sorted(list(set(m.Room.objects.filter(is_blacklisted=True).exclude(blacklist_reason="").values_list("blacklist_reason", flat=True))))

    return show(
        request,
        "rooms_list",
        rooms=rooms,
        all_blocks=all_blocks,
        all_causes=all_causes,
        filter_q=q,
        filter_block=block,
        filter_status=status,
        filter_cause=cause,
        filter_capacity=capacity,
        total_rooms=len(rooms),
    )


@staff
@require_POST
def room_toggle_blacklist(request, pk):
    r = get_object_or_404(m.Room, pk=pk)
    if r.is_blacklisted:
        r.is_blacklisted = False
        r.blacklist_reason = ""
        r.save(update_fields=["is_blacklisted", "blacklist_reason"])
        messages.success(request, f"Room {r} marked as active.")
    else:
        cause = request.POST.get("cause", "").strip() or "Maintenance"
        r.is_blacklisted = True
        r.blacklist_reason = cause
        r.save(update_fields=["is_blacklisted", "blacklist_reason"])
        messages.success(request, f"Room {r} blacklisted (cause: {cause}).")
    return redirect(request.POST.get("next") or "rooms")


@staff
def attendance(request):
    try:
        day = date.fromisoformat(request.GET.get("date") or request.POST.get("date") or "")
    except ValueError:
        day = date.today()
    students = list(m.Student.objects.filter(is_active=True).select_related("user"))
    existing = {a.student_id: a.status for a in m.Attendance.objects.filter(date=day)}
    on_leave = set(m.LeaveRequest.objects.filter(status="Approved", from_date__lte=day, to_date__gte=day).values_list("student_id", flat=True))
    if request.method == "POST":
        for s in students:
            st = request.POST.get(f"s{s.pk}")
            if st in ("P", "A", "L"):
                m.Attendance.objects.update_or_create(student=s, date=day, defaults={"status": st})
        messages.success(request, f"Attendance saved for {date_s(day)}.")
        return redirect(f"{reverse('attendance')}?date={day.isoformat()}")
    rows = [(s, existing.get(s.pk) or ("L" if s.pk in on_leave else "P"), s.pk in on_leave) for s in students]
    return show(request, "attendance", rows=rows, day=day, saved=bool(existing))


@staff
def leaves(request):
    rows = m.LeaveRequest.objects.select_related("student__user").order_by("status", "-created_at")
    return show(request, "leaves", rows=rows, badge=badge)


@staff
@require_POST
def leave_decide(request, pk, decision):
    lv = get_object_or_404(m.LeaveRequest, pk=pk)
    lv.status = "Approved" if decision == "approve" else "Rejected"
    lv.save()
    messages.success(request, f"Leave {lv.status.lower()}.")
    return redirect("leaves")


@staff
def reports(request):
    exp = request.GET.get("export")
    students = m.Student.objects.filter(is_active=True).select_related("user")
    fee_rows = [(x.student.roll_no, x.student.user.display_name, x.title, x.amount, x.paid, x.balance, x.due_date, x.status)
                for x in m.Fee.objects.select_related("student__user") if x.balance > 0]
    att_rows = [(s.roll_no, s.user.display_name, s.attendance_percent() if s.attendance_percent() is not None else "") for s in students]
    if exp:
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="{exp}_report.csv"'
        w = csv.writer(resp)
        if exp == "fees":
            w.writerow(["Roll no", "Name", "Fee", "Amount", "Paid", "Balance", "Due", "Status"]); w.writerows(fee_rows)
        else:
            w.writerow(["Roll no", "Name", "Attendance %"]); w.writerows(att_rows)
        return resp
    blocks = {}
    for r in m.Room.objects.all():
        b = blocks.setdefault(r.block, [0, 0])
        b[0] += r.capacity; b[1] += r.occupied
    occupancy = [(k, v[0], v[1], v[0] - v[1]) for k, v in sorted(blocks.items())]
    low = sorted([r for r in att_rows if r[2] != ""], key=lambda r: r[2])[:10]
    return show(request, "reports", occupancy=occupancy, fee_rows=fee_rows[:50], low=low, badge=badge)


# ---------- student ----------
def _mine(request):
    return request.user.student


@role_required("student")
def my_attendance(request):
    s = _mine(request)
    rows = [(date_s(a.date), badge(a.get_status_display())) for a in s.attendance.all()[:60]]
    return show(request, "plain", title=f"Attendance · {s.attendance_percent() if s.attendance_percent() is not None else '—'}% present",
                headers=["Date", "Status"], rows=rows)


@role_required("student")
def my_complaints(request):
    s = _mine(request)
    form = f.ComplaintForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        c = form.save(commit=False); c.student = s; c.save()
        messages.success(request, "Complaint submitted.")
        return redirect("my_complaints")
    rows = [(date_s(c.created_at), c.title, badge(c.status), c.staff_note or "—") for c in s.complaints.all()]
    return show(request, "plain", title="Complaints", headers=["Filed", "Title", "Status", "Warden's note"], rows=rows,
                form=form, form_title="New complaint", button="Submit complaint")


@role_required("student")
def my_leaves(request):
    s = _mine(request)
    form = f.LeaveForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        lv = form.save(commit=False); lv.student = s; lv.save()
        messages.success(request, "Leave request sent to the warden.")
        return redirect("my_leaves")
    rows = [(date_s(x.from_date), date_s(x.to_date), x.reason, badge(x.status)) for x in s.leaves.all()]
    return show(request, "plain", title="Leave requests", headers=["From", "To", "Reason", "Status"], rows=rows,
                form=form, form_title="Request leave", button="Send request")


@role_required("student")
def my_fees(request):
    s = _mine(request)
    rows = [(x.title, x.amount, x.paid, x.balance, date_s(x.due_date), badge(x.status)) for x in s.fees.all()]
    return show(request, "plain", title="Fees", headers=["Fee", "Amount", "Paid", "Balance", "Due", "Status"], rows=rows,
                note="Pay at the warden's office; the warden records your payment here.")


@login_required
def menu(request):
    grid = {d: {mk: "—" for mk, _ in m.MealMenu.MEALS} for d, _ in m.MealMenu.DAYS}
    for x in m.MealMenu.objects.all():
        grid[x.day][x.meal] = x.items
    rows = [(name, *[grid[d][mk] for mk, _ in m.MealMenu.MEALS]) for d, name in m.MealMenu.DAYS]
    return show(request, "plain", title="Weekly meal menu", headers=["Day"] + [n for _, n in m.MealMenu.MEALS], rows=rows)
