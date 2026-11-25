import csv
from decimal import Decimal
import re
from datetime import date, timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.html import format_html, mark_safe
from django.views.decorators.http import require_POST

from . import forms as f
from . import models as m
from .pdf_services import generate_fee_invoice_pdf, generate_fees_report_pdf
from .services import (
    bulk_create_rooms,
    bulk_create_rooms_floors,
    bulk_create_rooms_from_list,
    calculate_student_monthly_fee,
    import_allocations,
    import_rooms_csv,
    import_rows,
    notify_user,
    notify_users,
    parse_date,
)


def decode_csv_file(file_obj):
    raw = file_obj.read()
    try:
        return raw.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        return raw.decode("latin-1").splitlines()


def floor_order_key(b):
    code = str(b or "").strip().upper()
    order = {"B": -1, "G": 0, "0": 0, "F": 1, "1": 1, "S": 2, "2": 2, "T": 3, "3": 3, "4": 4, "5": 5}
    return order.get(code, 50)


STAFF = ("admin", "warden")
BADGE = {"Paid": "success", "Resolved": "success", "Approved": "success", "Present": "success", "Unpaid": "secondary",
         "Submitted": "secondary", "Pending": "warning", "Under Review": "info", "In Progress": "primary",
         "Partly paid": "info", "Overdue": "danger", "Rejected": "danger", "Absent": "danger", "On leave": "info",
         "Suspended": "danger", "Staying": "success", "Left": "secondary", "Active": "success"}


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


def _get_building_scope(request):
    """
    Resolve active building scope for staff:
    - Building-locked Warden (assigned_building set): strictly locked to that building (403 on other buildings).
    - Admin / Head Warden (assigned_building is None): free to view all or filter by ?building=<id>.
    """
    u = request.user
    all_buildings = list(m.Building.objects.filter(is_active=True).order_by("code", "name"))
    locked = getattr(u, "scoped_building", None) if getattr(u, "role", None) == "warden" else None
    if locked is not None:
        raw_b = (request.GET.get("building") or request.POST.get("building") or "").strip()
        if raw_b and raw_b.lower() not in ("all", "") and str(locked.pk) != raw_b and locked.code.lower() != raw_b.lower():
            raise PermissionDenied
        return [locked], locked, True

    raw_b = (request.GET.get("building") or "").strip()
    active = None
    if raw_b and raw_b.lower() != "all":
        if raw_b.isdigit():
            active = next((b for b in all_buildings if b.pk == int(raw_b)), None)
        else:
            active = next((b for b in all_buildings if b.code.lower() == raw_b.lower()), None)
    return all_buildings, active, False


def _student_matches_building(s, building):
    """Return True if student belongs to building (by active room or compatible unallocated year)."""
    if building is None:
        return True
    rm = s.room
    if rm is not None:
        return rm.building_id == building.pk
    return building.allows_year(s.year_of_study)


def _assert_building_access(request, building=None, room=None, student=None):
    """Raise PermissionDenied if a building-locked warden accesses records outside their building."""
    u = request.user
    locked = getattr(u, "scoped_building", None) if getattr(u, "role", None) == "warden" else None
    if locked is None:
        return
    if building is not None and building.pk != locked.pk:
        raise PermissionDenied
    if room is not None and room.building_id != locked.pk:
        raise PermissionDenied
    if student is not None and not _student_matches_building(student, locked):
        raise PermissionDenied


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
        locked = getattr(request.user, "scoped_building", None)
        rows = []
        for o in objs():
            if locked is not None:
                if isinstance(o, m.RoomAllocation) and o.room.building_id != locked.pk:
                    continue
                if isinstance(o, (m.Complaint, m.Fee)) and not _student_matches_building(o.student, locked):
                    continue
                if isinstance(o, m.Announcement):
                    if o.building_id and o.building_id != locked.pk:
                        continue
                    if o.room_id and o.room.building_id != locked.pk:
                        continue
                    if o.student_id and not _student_matches_building(o.student, locked):
                        continue
            rows.append({"cells": [c(o) for _, c in columns], "edit": reverse(f"{key}_edit", args=[o.pk]),
                         "delete": reverse(f"{key}_delete", args=[o.pk]) if delete else None,
                         "actions": actions(o) if actions else []})
        return show(request, "list", title=title, headers=[h for h, _ in columns], rows=rows,
                    add=reverse(f"{key}_new") if create else None, add_label=add_label)

    @role_required(*roles)
    def edit(request, pk=None):
        obj = get_object_or_404(model, pk=pk) if pk else None
        if obj is not None:
            if isinstance(obj, m.Room):
                _assert_building_access(request, room=obj)
            elif isinstance(obj, m.Student):
                _assert_building_access(request, student=obj)
            elif isinstance(obj, m.RoomAllocation):
                _assert_building_access(request, room=obj.room, student=obj.student)
            elif isinstance(obj, (m.Complaint, m.Fee)):
                _assert_building_access(request, student=obj.student)
            elif isinstance(obj, m.Announcement) and obj.building_id:
                _assert_building_access(request, building=obj.building)
        initial = request.GET.dict() if (request.method == "GET" and not obj) else None
        fk = {}
        if form_class in (f.RoomForm, f.AllocationForm, f.AnnouncementForm):
            fk["building"] = getattr(request.user, "scoped_building", None)
        form = form_class(request.POST or None, initial=initial, instance=obj, **fk)
        if request.method == "POST" and form.is_valid():
            form.save()
            messages.success(request, "Saved.")
            return redirect(key)
        return show(request, "form", title=(f"Edit {title.lower()}" if obj else f"{add_label} · {title}"), form=form, back=reverse(key))

    @role_required(*roles)
    def remove(request, pk):
        obj = get_object_or_404(model, pk=pk)
        if isinstance(obj, m.Room):
            _assert_building_access(request, room=obj)
        elif isinstance(obj, m.Student):
            _assert_building_access(request, student=obj)
        elif isinstance(obj, (m.Complaint, m.Fee)):
            _assert_building_access(request, student=obj.student)
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
    acts = []
    if fee.balance > 0:
        acts.append(("Record payment", reverse("payment_new", args=[fee.pk]), False, ""))
    if fee.fine_amount > 0 and not fee.fine_waived:
        acts.append(("Waive fine", reverse("fee_waive_fine", args=[fee.pk]), True, f"Waive late fine of ₹{fee.fine_amount} for {fee.student}?"))
    acts.append(("Receipt PDF", reverse("fee_pdf", args=[fee.pk]), False, ""))
    return acts


def fee_list_qs():
    for f_obj in m.Fee.objects.filter(due_date__lt=date.today()):
        f_obj.sync_late_fine()
    return m.Fee.objects.select_related("student__user").prefetch_related("payments")


V = {}
V["students"] = crud("students", m.Student, f.StudentEditForm, "Students", [
    ("Roll no.", lambda s: s.roll_no), ("Name", lambda s: s.user.display_name), ("Course", lambda s: s.course or "—"),
    ("Room", lambda s: s.room or "—"), ("Parent", lambda s: s.parent or "—"),
    ("Status", lambda s: badge("Suspended") if s.is_currently_suspended else badge("Staying" if s.is_active else "Left"))],
    qs=lambda: m.Student.objects.select_related("user", "parent__user"),
    actions=lambda s: [("View Profile", reverse("student_detail", args=[s.pk]), False, "")],
    add_label="Add student")
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
    ("Student", lambda x: x.student),
    ("Fee", lambda x: x.title),
    ("Rent", lambda x: f"₹{x.rent_amount}" if x.rent_amount else "—"),
    ("Food", lambda x: f"₹{x.food_amount}" if x.food_amount else "—"),
    ("Mess Cut", lambda x: f"-₹{x.mess_cut_amount} ({x.mess_cut_days}d)" if x.mess_cut_amount else "—"),
    ("Late Fine", lambda x: f"+₹{x.fine_amount} ({x.overdue_days}d late)" if x.fine_amount > 0 else ("Waived" if x.fine_waived else "—")),
    ("Total", lambda x: f"₹{x.amount}"),
    ("Paid", lambda x: f"₹{x.paid}"),
    ("Balance", lambda x: f"₹{x.balance}"),
    ("Due", lambda x: date_s(x.due_date)),
    ("Status", lambda x: badge(x.status))],
    qs=fee_list_qs, actions=fee_actions, add_label="Add custom fee")
V["announcements"] = crud("announcements", m.Announcement, f.AnnouncementForm, "Announcements", [
    ("Title", lambda a: a.title), ("For", lambda a: a.get_audience_display()),
    ("Building / Target", lambda a: a.room or a.building or "All Buildings"),
    ("Posted", lambda a: date_s(a.created_at))],
    add_label="Post")
V["wardens"] = crud("wardens", m.User, f.WardenEditForm, "Wardens", [
    ("Name", lambda u: u.display_name), ("Username", lambda u: u.username), ("Phone", lambda u: u.phone or "—"),
    ("Scope / Tier", lambda u: format_html('<span class="badge text-bg-{}">{}</span>', "primary" if u.is_head_warden else "warning text-dark", "Head Warden (All Buildings)" if u.is_head_warden else u.warden_role_label))],
    roles=("admin",), qs=lambda: m.User.objects.filter(role="warden").select_related("assigned_building"), add_label="Add warden")
V["buildings"] = crud("buildings", m.Building, f.BuildingForm, "Buildings", [
    ("Code", lambda b: b.code), ("Name", lambda b: b.name), ("Allowed Years", lambda b: b.allowed_years_display)],
    roles=("admin",), add_label="Add building")


@role_required("admin")
def buildings_list(request):
    buildings = list(m.Building.objects.prefetch_related("rooms", "wardens").order_by("code", "name"))
    building_cards = []
    for b in buildings:
        rooms = list(b.rooms.all())
        cap = sum(r.capacity for r in rooms)
        occ = m.RoomAllocation.objects.filter(room__building=b, end_date__isnull=True).count()
        vac = sum(r.vacancy for r in rooms)
        pct = round(100 * occ / cap) if cap > 0 else 0
        building_cards.append({
            "building": b,
            "rooms_count": len(rooms),
            "capacity": cap,
            "occupied": occ,
            "vacant": vac,
            "occupancy_pct": pct,
            "wardens": [w for w in b.wardens.filter(role="warden", is_active=True)],
        })
    return show(request, "buildings_list", buildings=buildings, building_cards=building_cards)


@role_required("admin")
def warden_new(request):
    form = f.WardenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        first, _, last = d["name"].partition(" ")
        from django.conf import settings
        m.User.objects.create_user(
            username=d["username"],
            password=settings.DEFAULT_PASSWORD,
            first_name=first,
            last_name=last,
            role="warden",
            phone=d["phone"],
            assigned_building=d.get("assigned_building"),
            is_staff=True,
            must_change_password=True,
        )
        messages.success(request, "Warden created with the default password; they must change it on first login.")
        return redirect("wardens")
    return show(request, "form", title="Add warden", form=form, back=reverse("wardens"))


@staff
def announcement_new(request):
    form = f.AnnouncementForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        anc = form.save(commit=False)
        anc.created_by = request.user
        anc.save()
        aud = anc.audience
        recipients = []
        if aud in ("all", "student"):
            recipients.extend([s.user for s in m.Student.objects.filter(is_active=True).select_related("user")])
        if aud in ("all", "parent"):
            recipients.extend([p.user for p in m.Parent.objects.all().select_related("user")])
        notify_users(
            recipients=recipients,
            title=f"Hostel Announcement: {anc.title}",
            message=anc.body[:200],
            category="announcement",
            link=reverse("home"),
            actor=request.user,
        )
        messages.success(request, "Announcement posted.")
        return redirect("announcements")
    return show(request, "form", title="Post · Announcements", form=form, back=reverse("announcements"))


@staff
def fee_create(request):
    form = f.FeeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        targets = [d["student"]] if d["student"] else list(m.Student.objects.filter(is_active=True))
        m.Fee.objects.bulk_create([
            m.Fee(
                student=s,
                title=d["title"],
                amount=d["amount"],
                due_date=d["due_date"],
                rent_amount=d.get("rent_amount") or Decimal("0.00"),
                food_amount=d.get("food_amount") or Decimal("0.00"),
                mess_cut_days=d.get("mess_cut_days") or 0,
                mess_cut_amount=d.get("mess_cut_amount") or Decimal("0.00"),
                notes=d.get("notes") or "",
            )
            for s in targets
        ])
        student_users = [s.user for s in targets]
        parent_users = [s.parent.user for s in targets if s.parent]
        notify_users(
            recipients=student_users + parent_users,
            title=f"New Fee Issued: {d['title']}",
            message=f"Amount: ₹{d['amount']} | Due: {d['due_date'].strftime('%d %b %Y')}. Review in Fees section.",
            category="fee",
            link=reverse("my_fees"),
            actor=request.user,
        )
        messages.success(request, f"Fee added for {len(targets)} student(s).")
        return redirect("fees")
    return show(request, "form", title="Add fee", form=form, back=reverse("fees"))


@staff
def fee_structure(request):
    """Allows wardens and admins to configure fixed rent, food daily rates, and mess cut rules."""
    cfg = m.FeeConfig.get_config()
    form = f.FeeConfigForm(request.POST or None, instance=cfg)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Fee structure and mess cut settings updated successfully.")
        return redirect("fee_structure")
    return render(request, "core/fee_structure.html", {
        "title": "Fee Structure & Mess Cut Rules",
        "form": form,
        "cfg": cfg,
    })


@staff
def fees_calculate_monthly(request):
    """
    Scans attendance records for consecutive absent days (>= 3 days),
    calculates monthly hostel rent, food charges, and mess cut rebates.
    Allows the warden to adjust any student's figures directly before saving bills.
    """
    cfg = m.FeeConfig.get_config()
    today = date.today()

    # Determine selected month
    month_param = request.POST.get("month") or request.GET.get("month")
    if month_param:
        try:
            parts = month_param.split("-")
            year = int(parts[0])
            month = int(parts[1])
            selected_date = date(year, month, 1)
        except (ValueError, IndexError):
            selected_date = date(today.year, today.month, 1)
            year, month = selected_date.year, selected_date.month
    else:
        selected_date = date(today.year, today.month, 1)
        year, month = selected_date.year, selected_date.month

    month_str = f"{year:04d}-{month:02d}"
    month_label = selected_date.strftime("%B %Y")

    # Generate available months list (current + past 11 months)
    available_months = []
    y, m_num = today.year, today.month
    for _ in range(12):
        d_val = date(y, m_num, 1)
        available_months.append((d_val.strftime("%Y-%m"), d_val.strftime("%B %Y")))
        if m_num == 1:
            m_num = 12
            y -= 1
        else:
            m_num -= 1

    active_students = list(m.Student.objects.filter(is_active=True).select_related("user").prefetch_related("allocations__room"))

    # Handle generation/saving of bills
    if request.method == "POST" and "save_fees" in request.POST:
        selected_student_ids = request.POST.getlist("selected_students")
        due_date_str = request.POST.get("due_date")
        title = (request.POST.get("title") or f"Hostel & Mess Fee - {month_label}").strip()
        due_date = parse_date(due_date_str) if due_date_str else (selected_date + timedelta(days=39)).replace(day=10)

        created_count = 0
        updated_count = 0

        for sid_str in selected_student_ids:
            try:
                sid = int(sid_str)
                student = next((s for s in active_students if s.pk == sid), None)
                if not student:
                    continue

                raw_rent = (request.POST.get(f"rent_{sid}") or "").strip()
                rent_val = Decimal(raw_rent) if raw_rent else cfg.rent_amount

                raw_food = (request.POST.get(f"food_{sid}") or "").strip()
                food_val = Decimal(raw_food) if raw_food else Decimal("0.00")

                raw_days = (request.POST.get(f"mess_days_{sid}") or "").strip()
                mess_days_val = int(raw_days) if raw_days.isdigit() else 0

                raw_cut = (request.POST.get(f"mess_cut_{sid}") or "").strip()
                mess_cut_val = Decimal(raw_cut) if raw_cut else Decimal("0.00")

                raw_total = (request.POST.get(f"total_{sid}") or "").strip()
                total_val = Decimal(raw_total) if raw_total else max(Decimal("0.00"), rent_val + food_val - mess_cut_val)

                notes = f"Rent: ₹{rent_val} | Food: ₹{food_val} | Mess Cut: -₹{mess_cut_val} ({mess_days_val}d)"

                existing = m.Fee.objects.filter(student=student, month=month_str).first()
                if existing:
                    existing.title = title
                    existing.rent_amount = rent_val
                    existing.food_amount = food_val
                    existing.mess_cut_days = mess_days_val
                    existing.mess_cut_amount = mess_cut_val
                    existing.amount = total_val
                    existing.due_date = due_date
                    existing.notes = notes
                    existing.save()
                    updated_count += 1
                else:
                    m.Fee.objects.create(
                        student=student,
                        title=title,
                        month=month_str,
                        rent_amount=rent_val,
                        food_amount=food_val,
                        mess_cut_days=mess_days_val,
                        mess_cut_amount=mess_cut_val,
                        amount=total_val,
                        due_date=due_date,
                        notes=notes,
                    )
                    created_count += 1
            except Exception:
                continue

        msg = f"Hostel & Mess fee bills saved for {created_count + updated_count} student(s) for {month_label}."
        if updated_count:
            msg += f" ({updated_count} existing bills updated)."

        # Notify affected students and parents
        notif_students = [s for s in active_students if str(s.pk) in selected_student_ids]
        student_users = [s.user for s in notif_students]
        parent_users = [s.parent.user for s in notif_students if s.parent]
        notify_users(
            recipients=student_users + parent_users,
            title=f"Hostel & Mess Fee Released - {month_label}",
            message=f"{title} has been generated. Due date: {due_date.strftime('%d %b %Y')}. Review breakdown in Fees section.",
            category="fee",
            link=reverse("my_fees"),
            actor=request.user,
        )

        messages.success(request, msg)
        return redirect("fees")

    existing_fees = {f.student_id: f for f in m.Fee.objects.filter(month=month_str)}

    calc_rows = []
    total_projected = Decimal("0.00")
    total_mess_cut_all = Decimal("0.00")

    for s in active_students:
        calc = calculate_student_monthly_fee(s, year, month, config=cfg)
        ex = existing_fees.get(s.pk)
        row = {
            "student": s,
            "room": s.room,
            "calc": calc,
            "existing_fee": ex,
            "rent": ex.rent_amount if ex and ex.rent_amount else calc["rent_amount"],
            "food": ex.food_amount if ex and ex.food_amount else calc["food_amount"],
            "mess_days": ex.mess_cut_days if ex else calc["mess_cut_days"],
            "mess_cut": ex.mess_cut_amount if ex and ex.mess_cut_amount else calc["mess_cut_amount"],
            "total": ex.amount if ex else calc["total_amount"],
        }
        calc_rows.append(row)
        total_projected += row["total"]
        total_mess_cut_all += row["mess_cut"]

    default_due = (selected_date + timedelta(days=39)).replace(day=10)

    context = {
        "title": f"Monthly Fee & Mess Cut Calculation · {month_label}",
        "cfg": cfg,
        "month_str": month_str,
        "month_label": month_label,
        "available_months": available_months,
        "calc_rows": calc_rows,
        "default_title": f"Hostel & Mess Fee - {month_label}",
        "default_due": default_due.strftime("%Y-%m-%d"),
        "total_students": len(active_students),
        "total_projected": total_projected,
        "total_mess_cut_all": total_mess_cut_all,
    }
    return render(request, "core/fees_calculate_monthly.html", context)


@staff
def payment_new(request, fee_id):
    fee = get_object_or_404(m.Fee, pk=fee_id)
    fee.sync_late_fine()
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
def fee_waive_fine(request, pk):
    """Allows wardens or admins to waive the late fine for a student."""
    fee = get_object_or_404(m.Fee, pk=pk)
    fee.fine_waived = True
    fee.sync_late_fine(save=True)
    messages.success(request, f"Late fine waived for {fee.student}. Total fee adjusted to ₹{fee.amount}.")
    return redirect("fees")


@staff
def fees_export_pdf(request):
    """Exports all or filtered hostel fees as a master PDF report."""
    month = request.GET.get("month")
    status_filter = request.GET.get("status")
    qs = m.Fee.objects.select_related("student__user").prefetch_related("payments")

    title = "Hostel Fees & Resident Billing Statement"
    subtitle = ""
    if month:
        qs = qs.filter(month=month)
        try:
            parts = month.split("-")
            month_lbl = date(int(parts[0]), int(parts[1]), 1).strftime("%B %Y")
            title = f"Hostel Fee Report · {month_lbl}"
            subtitle = f"All billed resident fees and mess charges for {month_lbl}"
        except Exception:
            pass

    fee_list = list(qs)
    for f in fee_list:
        if f.due_date and f.due_date < date.today():
            f.sync_late_fine()

    if status_filter == "pending":
        fee_list = [f for f in fee_list if f.balance > 0]
        title = "Hostel Pending Fees & Dues Statement"
        subtitle = "All residents with outstanding fee balances"
    elif status_filter == "overdue":
        fee_list = [f for f in fee_list if f.balance > 0 and f.due_date and f.due_date < date.today()]
        title = "Hostel Overdue Fees & Penalties Report"
        subtitle = "Residents with past-due balances incurring late fines"

    pdf_bytes = generate_fees_report_pdf(fee_list, title=title, subtitle=subtitle, staff_user=request.user)
    filename = f"hostel_fees_{month or 'report'}_{date.today().strftime('%Y%m%d')}.pdf"
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required
def fee_invoice_pdf(request, pk):
    """Exports an individual fee invoice / receipt as a downloadable PDF."""
    fee = get_object_or_404(m.Fee, pk=pk)
    u = request.user
    if u.role == "student" and fee.student.user != u:
        raise PermissionDenied
    if u.role == "parent":
        p = getattr(u, "parent", None)
        if not p or fee.student not in p.children.all():
            raise PermissionDenied

    pdf_bytes = generate_fee_invoice_pdf(fee, current_user=u)
    filename = f"fee_receipt_{fee.student.roll_no}_{fee.pk}.pdf"
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


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
        today = date.today()
        buildings, active_building, is_building_locked = _get_building_scope(request)

        # Student metrics (respecting active/locked building)
        all_students = list(
            m.Student.objects.select_related("user").prefetch_related(
                Prefetch("allocations", queryset=m.RoomAllocation.objects.filter(end_date__isnull=True).select_related("room__building"))
            )
        )
        scoped_students = [s for s in all_students if _student_matches_building(s, active_building)]
        scoped_student_ids = [s.pk for s in scoped_students]

        active_scoped = [s for s in scoped_students if s.is_active]
        active_student_count = len(active_scoped)
        inactive_student_count = len(scoped_students) - active_student_count
        allocated_count = sum(1 for s in active_scoped if s.room and (not active_building or s.room.building_id == active_building.pk))
        unallocated_count = max(0, active_student_count - allocated_count)

        # Rooms & Beds (respecting active/locked building)
        rooms_qs = m.Room.objects.select_related("building").prefetch_related(
            Prefetch("allocations", queryset=m.RoomAllocation.objects.filter(end_date__isnull=True))
        )
        if active_building is not None:
            rooms_qs = rooms_qs.filter(building=active_building)
        all_rooms = list(rooms_qs)
        total_rooms = len(all_rooms)
        total_beds = sum(r.capacity for r in all_rooms)
        occupied_beds = sum(r.occupied for r in all_rooms)
        blacklisted_rooms = [r for r in all_rooms if r.is_blacklisted]
        blacklisted_beds = sum(r.capacity for r in blacklisted_rooms)
        free_beds = max(0, total_beds - occupied_beds - blacklisted_beds)
        effective_capacity = total_beds - blacklisted_beds
        occupancy_pct = round(100 * occupied_beds / effective_capacity) if effective_capacity > 0 else 0

        # Floor-wise stats for bar chart
        floor_dict = {}
        for r in all_rooms:
            fl_label = f"{r.building.code} · {r.floor_display}" if (active_building is None and r.building_id) else r.floor_display
            b = floor_dict.setdefault(fl_label, {"rooms": 0, "capacity": 0, "occupied": 0, "free": 0, "b_code": r.building.code if r.building_id else "", "fl_code": r.block})
            b["rooms"] += 1
            b["capacity"] += r.capacity
            b["occupied"] += r.occupied
            b["free"] = max(0, b["capacity"] - b["occupied"])

        block_labels = sorted(
            floor_dict.keys(),
            key=lambda k: (floor_dict[k]["b_code"], floor_order_key(floor_dict[k]["fl_code"])),
        )
        block_capacities = [floor_dict[k]["capacity"] for k in block_labels]
        block_occupied = [floor_dict[k]["occupied"] for k in block_labels]
        block_free = [floor_dict[k]["free"] for k in block_labels]

        # Attendance metrics for today
        today_att = m.Attendance.objects.filter(date=today, student_id__in=scoped_student_ids)
        att_marked_count = today_att.count()
        att_present = today_att.filter(status="P").count()
        att_absent = today_att.filter(status="A").count()
        att_leave = today_att.filter(status="L").count()
        att_unmarked = max(0, active_student_count - att_marked_count)
        att_total_eval = att_present + att_absent
        today_att_pct = round(100 * att_present / att_total_eval) if att_total_eval > 0 else (100 if att_present > 0 else 0)

        # Pending items & alerts
        pending_leaves = list(
            m.LeaveRequest.objects.filter(status="Pending", student_id__in=scoped_student_ids)
            .select_related("student__user")
            .order_by("from_date")[:4]
        )
        pending_leaves_count = m.LeaveRequest.objects.filter(status="Pending", student_id__in=scoped_student_ids).count()
        open_complaints = list(
            m.Complaint.objects.filter(student_id__in=scoped_student_ids)
            .exclude(status="Resolved")
            .select_related("student__user")
            .order_by("-created_at")[:4]
        )
        open_complaints_count = m.Complaint.objects.filter(student_id__in=scoped_student_ids).exclude(status="Resolved").count()

        # Fees
        all_fees = list(m.Fee.objects.filter(student_id__in=scoped_student_ids).prefetch_related("payments"))
        total_fee_amount = sum(f.amount for f in all_fees)
        total_balance_amount = sum(f.balance for f in all_fees)
        total_paid_amount = max(0, total_fee_amount - total_balance_amount)
        fee_paid_pct = round(100 * total_paid_amount / total_fee_amount) if total_fee_amount > 0 else 100
        overdue_fees_count = sum(1 for x in all_fees if x.due_date and x.due_date < today and x.balance > 0)

        # Today's menu
        today_menu = list(m.MealMenu.objects.filter(day=today.weekday()))

        # Per-building overview strip when viewing all buildings
        building_summaries = []
        if active_building is None and len(buildings) > 1:
            all_db_rooms = list(m.Room.objects.select_related("building").prefetch_related(
                Prefetch("allocations", queryset=m.RoomAllocation.objects.filter(end_date__isnull=True))
            ))
            for b in buildings:
                b_rooms = [r for r in all_db_rooms if r.building_id == b.pk]
                b_cap = sum(r.capacity for r in b_rooms)
                b_occ = sum(r.occupied for r in b_rooms)
                b_vac = sum(r.vacancy for r in b_rooms)
                b_students = sum(1 for s in all_students if s.is_active and _student_matches_building(s, b))
                building_summaries.append({
                    "building": b,
                    "rooms": len(b_rooms),
                    "capacity": b_cap,
                    "occupied": b_occ,
                    "vacant": b_vac,
                    "students": b_students,
                    "pct": round(100 * b_occ / b_cap) if b_cap > 0 else 0,
                })

        # Backward compatibility for any template referencing stats
        stats = [
            ("Students staying", active_student_count, "students"),
            ("Beds free", free_beds, "rooms"),
            ("Leave requests waiting", pending_leaves_count, "leaves"),
            ("Open complaints", open_complaints_count, "complaints"),
            ("Attendance marked today", f"{att_marked_count}/{active_student_count}", "attendance"),
            ("Fees overdue", overdue_fees_count, "fees"),
        ]

        return show(
            request,
            "dashboard",
            stats=stats,
            today=today,
            buildings=buildings,
            active_building=active_building,
            is_building_locked=is_building_locked,
            building_summaries=building_summaries,
            active_student_count=active_student_count,
            inactive_student_count=inactive_student_count,
            allocated_count=allocated_count,
            unallocated_count=unallocated_count,
            total_rooms=total_rooms,
            total_beds=total_beds,
            occupied_beds=occupied_beds,
            free_beds=free_beds,
            blacklisted_beds=blacklisted_beds,
            blacklisted_rooms_count=len(blacklisted_rooms),
            occupancy_pct=occupancy_pct,
            block_labels=block_labels,
            block_capacities=block_capacities,
            block_occupied=block_occupied,
            block_free=block_free,
            att_marked_count=att_marked_count,
            att_present=att_present,
            att_absent=att_absent,
            att_leave=att_leave,
            att_unmarked=att_unmarked,
            today_att_pct=today_att_pct,
            pending_leaves=pending_leaves,
            pending_leaves_count=pending_leaves_count,
            open_complaints=open_complaints,
            open_complaints_count=open_complaints_count,
            total_fee_amount=total_fee_amount,
            total_paid_amount=total_paid_amount,
            total_balance_amount=total_balance_amount,
            fee_paid_pct=fee_paid_pct,
            overdue_fees_count=overdue_fees_count,
            today_menu=today_menu,
            badge=badge,
        )

    if u.role == "student":
        return show(request, "overview", **overview(u.student, u))
    today = date.today()
    children = list(u.parent.children.select_related("user") if hasattr(u, "parent") else [])
    if len(children) == 1:
        return redirect("child", children[0].pk)
    child_cards = []
    for c in children:
        today_att = c.attendance.filter(date=today).first()
        child_cards.append({
            "student": c,
            "room": c.room,
            "pct": c.attendance_percent(),
            "balance": c.fee_balance,
            "today_status": today_att.status if today_att else None,
        })
    return show(request, "children", children=child_cards, today=today)


def _vacant_rooms():
    """Return active, non-blacklisted rooms with open beds, ordered by floor and number."""
    rooms = []
    for r in m.Room.objects.filter(is_blacklisted=False).prefetch_related(
        Prefetch("allocations", queryset=m.RoomAllocation.objects.filter(end_date__isnull=True))
    ):
        if r.vacancy > 0:
            rooms.append(r)
    rooms.sort(key=lambda r: (floor_order_key(r.block), r.number))
    return rooms


def overview(s, user):
    today = date.today()
    aud = ["all"]
    if user.role == "student":
        aud.append("student")
    elif user.role == "parent":
        aud.append("parent")
    else:
        aud.extend(["student", "parent"])
    menu = list(m.MealMenu.objects.filter(day=today.weekday()))

    # Attendance statistics for student
    att_qs = s.attendance.all()
    p_count = att_qs.filter(status="P").count()
    a_count = att_qs.filter(status="A").count()
    l_count = att_qs.filter(status="L").count()
    total_att = p_count + a_count + l_count

    # Today's attendance record
    today_att = s.attendance.filter(date=today).first()
    today_status = today_att.status if today_att else None

    # Room and roommates
    room = s.room
    roommates = []
    available_rooms = []
    if room:
        allocs = m.RoomAllocation.objects.filter(
            room=room, end_date__isnull=True
        ).exclude(student=s).select_related("student__user", "student__parent__user")
        roommates = [a.student for a in allocs]
    elif user.role in STAFF:
        available_rooms = _vacant_rooms()

    # Fee metrics
    fees = list(s.fees.prefetch_related("payments"))
    total_fee = sum(f.amount for f in fees)
    total_paid = sum(f.paid for f in fees)
    balance = s.fee_balance
    fee_paid_pct = round(100 * total_paid / total_fee) if total_fee > 0 else (100 if total_fee == 0 else 0)

    # Leaves and complaints
    recent_leaves = list(s.leaves.all()[:5])
    open_complaints_count = s.complaints.exclude(status="Resolved").count()
    my_complaints_list = list(s.complaints.all()[:3])

    # Wardens for emergency / inquiries
    wardens = list(m.User.objects.filter(role="warden", is_active=True))

    # Room-targeted and general announcements
    if room:
        notices_qs = m.Announcement.objects.filter(
            Q(room=room, audience__in=aud) | Q(room__isnull=True, audience__in=aud) | Q(student=s)
        )
    else:
        notices_qs = m.Announcement.objects.filter(
            Q(room__isnull=True, audience__in=aud) | Q(student=s)
        )
    notices = list(notices_qs.select_related("room", "created_by").order_by("-created_at")[:10])

    return dict(
        s=s,
        room=room,
        roommates=roommates,
        pct=s.attendance_percent(),
        p_count=p_count,
        a_count=a_count,
        l_count=l_count,
        total_att=total_att,
        today_att=today_att,
        today_status=today_status,
        today=today,
        total_fee=total_fee,
        total_paid=total_paid,
        balance=balance,
        fee_paid_pct=fee_paid_pct,
        menu=menu,
        complaints=open_complaints_count,
        my_complaints=my_complaints_list,
        recent=att_qs[:10],
        recent_leaves=recent_leaves,
        fees=fees,
        wardens=wardens,
        notices=notices,
        badge=badge,
        available_rooms=available_rooms,
    )


@role_required("parent")
def child(request, pk):
    s = get_object_or_404(m.Student, pk=pk, parent__user=request.user)
    siblings = list(request.user.parent.children.exclude(pk=s.pk).select_related("user"))
    all_children = list(request.user.parent.children.select_related("user"))
    return show(
        request,
        "overview",
        parent_view=True,
        siblings=siblings,
        all_children=all_children,
        **overview(s, request.user),
    )


# ---------- staff workflows ----------
def _handle_student_bulk_action(request):
    """Execute bulk actions submitted from the student directory selection toolbar."""
    action = request.POST.get("bulk_action", "").strip()
    selected_ids = request.POST.getlist("selected_students")
    if not selected_ids:
        messages.warning(request, "No students were selected.")
        return redirect(request.get_full_path())

    students = list(
        m.Student.objects.filter(pk__in=selected_ids)
        .select_related("user", "parent__user")
        .prefetch_related(
            Prefetch("allocations", queryset=m.RoomAllocation.objects.filter(end_date__isnull=True).select_related("room"))
        )
    )
    if not students:
        messages.warning(request, "No matching students found.")
        return redirect(request.get_full_path())

    if action == "notify":
        title = request.POST.get("notify_title", "").strip() or "Administrative Notice"
        msg = request.POST.get("notify_message", "").strip()
        if not msg:
            messages.error(request, "Please enter a notification message.")
            return redirect(request.get_full_path())
        recipients = [s.user for s in students]
        if request.POST.get("notify_parents") == "1":
            recipients.extend(s.parent.user for s in students if s.parent and s.parent.user)
        notify_users(
            recipients=recipients,
            title=title,
            message=msg,
            category="announcement",
            link=reverse("home"),
            actor=request.user,
        )
        messages.success(request, f"Notification sent to {len(students)} student(s).")

    elif action == "allocate_room":
        room_id = request.POST.get("target_room_id", "").strip()
        if not room_id:
            messages.error(request, "Please select a target room to allocate.")
            return redirect(request.get_full_path())
        room = get_object_or_404(m.Room, pk=room_id)
        if room.is_blacklisted:
            messages.error(request, f"Room {room} is blacklisted and cannot accept allocations.")
            return redirect(request.get_full_path())

        to_allocate = [s for s in students if not s.room]
        already_allocated = len(students) - len(to_allocate)
        if not to_allocate:
            messages.warning(request, "All selected students already have active room allocations.")
            return redirect(request.get_full_path())

        if len(to_allocate) > room.vacancy:
            messages.error(
                request,
                f"Room {room} only has {room.vacancy} vacant bed(s); cannot allocate {len(to_allocate)} student(s).",
            )
            return redirect(request.get_full_path())

        today = date.today()
        for s in to_allocate:
            m.RoomAllocation.objects.create(student=s, room=room, start_date=today)
            notify_user(
                recipient=s.user,
                actor=request.user,
                category="announcement",
                title="Hostel Room Allocated",
                message=f"You have been allocated to Room {room} ({room.floor_display}) starting from {today}.",
                link=reverse("home"),
            )
            if s.parent and s.parent.user:
                notify_user(
                    recipient=s.parent.user,
                    actor=request.user,
                    category="announcement",
                    title="Ward Room Allocated",
                    message=f"Your ward {s.user.display_name} has been allocated to Room {room} ({room.floor_display}) starting from {today}.",
                    link=reverse("home"),
                )
        msg = f"Allocated {len(to_allocate)} student(s) to Room {room}."
        if already_allocated:
            msg += f" ({already_allocated} already had active rooms and were skipped.)"
        messages.success(request, msg)

    elif action == "vacate":
        vacated = m.RoomAllocation.objects.filter(
            student__in=students, end_date__isnull=True
        ).update(end_date=date.today())
        if vacated:
            messages.success(request, f"Vacated active room allocations for {vacated} student(s).")
        else:
            messages.info(request, "None of the selected students had active room allocations.")

    elif action == "mark_staying":
        updated = m.Student.objects.filter(pk__in=[s.pk for s in students]).update(is_active=True)
        messages.success(request, f"{updated} student(s) marked as Staying.")

    elif action == "mark_left":
        pks = [s.pk for s in students]
        updated = m.Student.objects.filter(pk__in=pks).update(is_active=False)
        m.RoomAllocation.objects.filter(student_id__in=pks, end_date__isnull=True).update(end_date=date.today())
        messages.success(request, f"{updated} student(s) marked as Left and active room allocations closed.")

    elif action == "suspend":
        if request.user.role != "admin":
            raise PermissionDenied
        reason = request.POST.get("suspend_reason", "").strip()
        if not reason:
            messages.error(request, "A reason is required to suspend selected students.")
            return redirect(request.get_full_path())
        end_str = request.POST.get("suspend_end_date", "").strip()
        start_d = date.today()
        end_d = parse_date(end_str) if end_str else None
        if end_d and end_d < start_d:
            messages.error(request, "Suspension end date cannot be earlier than today.")
            return redirect(request.get_full_path())
        for s in students:
            _enact_student_suspension(s, request.user, reason, start_d, end_d)
        messages.success(request, f"Suspended {len(students)} student(s). Entry to the hostel is barred.")

    elif action == "reinstate":
        if request.user.role != "admin":
            raise PermissionDenied
        suspended = [s for s in students if s.is_suspended]
        for s in suspended:
            _lift_student_suspension(s, request.user)
        messages.success(request, f"Lifted suspension for {len(suspended)} student(s).")

    elif action == "delete":
        count = len(students)
        for s in students:
            s.delete()
        messages.success(request, f"Deleted {count} student(s).")

    else:
        messages.warning(request, "Please choose a valid action.")

    return redirect(request.get_full_path())


@staff
def student_list(request):
    if request.method == "POST":
        return _handle_student_bulk_action(request)

    q = request.GET.get("q", "").strip()
    floor_filter = request.GET.get("floor", "all").strip()
    allocation_filter = request.GET.get("allocation", "all").strip().lower()
    status_filter = request.GET.get("status", "all").strip().lower()
    course_filter = request.GET.get("course", "all").strip()
    view_mode = request.GET.get("view", "grid").strip().lower()
    grouped = request.GET.get("grouped", "1").strip() != "0"

    # Distinct blocks from rooms for floor sorting
    distinct_blocks = sorted(list(set(m.Room.objects.values_list("block", flat=True))), key=floor_order_key)
    all_floors = []
    seen_floor_displays = set()
    for b in distinct_blocks:
        rm_dummy = m.Room(block=b)
        disp = rm_dummy.floor_display
        if disp not in seen_floor_displays:
            seen_floor_displays.add(disp)
            all_floors.append({"code": b, "display": disp, "name": rm_dummy.floor_name})

    students_qs = m.Student.objects.select_related("user", "parent__user").prefetch_related(
        Prefetch("allocations", queryset=m.RoomAllocation.objects.filter(end_date__isnull=True).select_related("room"))
    )

    all_students = list(students_qs)
    total_count = len(all_students)
    staying_count = sum(1 for s in all_students if s.is_active and not s.is_currently_suspended)
    suspended_count = sum(1 for s in all_students if s.is_currently_suspended)
    left_count = sum(1 for s in all_students if not s.is_active)
    allocated_count = sum(1 for s in all_students if s.room)
    unallocated_count = sum(1 for s in all_students if not s.room)

    # Compute floor student counts across all students
    for fl in all_floors:
        fl["count"] = sum(1 for s in all_students if s.room and str(s.room.block).strip().upper() == fl["code"].upper())

    # Distinct courses
    all_courses = sorted(list(set(s.course.strip() for s in all_students if s.course and s.course.strip())))

    # Filter pipeline
    filtered = all_students

    # 1. Allocation filter
    if allocation_filter == "allocated":
        filtered = [s for s in filtered if s.room]
    elif allocation_filter == "unallocated":
        filtered = [s for s in filtered if not s.room]

    # 2. Floor filter
    if floor_filter and floor_filter != "all":
        if floor_filter in ("unallocated", "none"):
            filtered = [s for s in filtered if not s.room]
        else:
            filtered = [s for s in filtered if s.room and str(s.room.block).strip().upper() == floor_filter.upper()]

    # 3. Status filter
    if status_filter == "staying":
        filtered = [s for s in filtered if s.is_active and not s.is_currently_suspended]
    elif status_filter == "suspended":
        filtered = [s for s in filtered if s.is_currently_suspended]
    elif status_filter == "left":
        filtered = [s for s in filtered if not s.is_active]

    # 4. Course filter
    if course_filter and course_filter != "all":
        filtered = [s for s in filtered if (s.course or "").strip().lower() == course_filter.lower()]

    # 5. Search query
    if q:
        ql = q.lower()
        filtered = [
            s for s in filtered
            if (ql in s.user.display_name.lower() or
                ql in s.roll_no.lower() or
                ql in (s.course or "").lower() or
                (s.room and (ql in str(s.room).lower() or ql in s.room.floor_name.lower())))
        ]

    # Build categorized groupings (floors and unallocated)
    categories = []
    if allocation_filter != "unallocated":
        for fl in all_floors:
            if floor_filter != "all" and floor_filter.upper() != fl["code"].upper():
                continue
            fl_students = [s for s in filtered if s.room and str(s.room.block).strip().upper() == fl["code"].upper()]
            if fl_students or (floor_filter != "all" and floor_filter.upper() == fl["code"].upper()):
                categories.append({
                    "type": "floor",
                    "code": fl["code"],
                    "title": fl["display"],
                    "subtitle": f"Residents residing on {fl['name']}",
                    "badge_class": "text-bg-primary",
                    "count": len(fl_students),
                    "students": fl_students,
                })

    if allocation_filter != "allocated" and (floor_filter == "all" or floor_filter in ("unallocated", "none")):
        unallocated_students = [s for s in filtered if not s.room]
        if unallocated_students or allocation_filter == "unallocated" or floor_filter in ("unallocated", "none"):
            categories.append({
                "type": "unallocated",
                "code": "unallocated",
                "title": "Unallocated Students",
                "subtitle": "Students awaiting room and bed allocation",
                "badge_class": "text-bg-warning text-dark",
                "count": len(unallocated_students),
                "students": unallocated_students,
            })

    table_rows = []
    if view_mode == "table":
        for s in filtered:
            table_rows.append({
                "student": s,
                "cells": [
                    s.roll_no,
                    s.user.display_name,
                    s.course or "—",
                    s.room.floor_display if s.room else "—",
                    s.room or "Unallocated",
                    s.parent or "—",
                    badge("Suspended") if s.is_currently_suspended else badge("Staying" if s.is_active else "Left"),
                ],
                "detail": reverse("student_detail", args=[s.pk]),
                "edit": reverse("students_edit", args=[s.pk]),
                "delete": reverse("students_delete", args=[s.pk]),
            })

    has_active_filters = bool(
        q or
        (floor_filter and floor_filter != "all") or
        (allocation_filter and allocation_filter != "all") or
        (status_filter and status_filter != "all") or
        (course_filter and course_filter != "all")
    )

    context = {
        "title": "Students",
        "students": filtered,
        "categories": categories,
        "total_count": total_count,
        "filtered_count": len(filtered),
        "staying_count": staying_count,
        "suspended_count": suspended_count,
        "left_count": left_count,
        "allocated_count": allocated_count,
        "unallocated_count": unallocated_count,
        "all_floors": all_floors,
        "all_courses": all_courses,
        "available_rooms": _vacant_rooms(),
        "q": q,
        "floor_filter": floor_filter,
        "allocation_filter": allocation_filter,
        "status_filter": status_filter,
        "course_filter": course_filter,
        "view_mode": view_mode,
        "grouped": grouped,
        "has_active_filters": has_active_filters,
        "table_rows": table_rows,
        "table_headers": ["Roll no.", "Name", "Course", "Floor", "Room", "Parent", "Status"],
        "add": reverse("students_new"),
        "add_label": "Add student",
    }
    return render(request, "core/students_list.html", context)


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
                blocks = request.POST.getlist("block")
                block = d.get("count_block") or (blocks[0] if blocks else None) or d.get("block") or "A"
                start_no = d.get("start_number") or "101"
                count = d.get("count")
                end_no = d.get("end_number") or None
                caps = request.POST.getlist("capacity")
                cap = d.get("count_capacity") or (caps[0] if caps else None) or d.get("capacity") or 2
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
                block = d.get("floor_block") or d.get("block") or "A"
                start_fl = d.get("start_floor") or 1
                end_fl = d.get("end_floor") or 1
                r_per_fl = d.get("rooms_per_floor") or 10
                cap = d.get("floor_capacity") or d.get("capacity") or 2
                result = bulk_create_rooms_floors(
                    block=block,
                    start_floor=start_fl,
                    end_floor=end_fl,
                    rooms_per_floor=r_per_fl,
                    capacity=cap,
                )
            elif mode == "list":
                block = d.get("list_block") or d.get("block") or "A"
                text = d.get("room_list") or ""
                cap = d.get("list_capacity") or d.get("capacity") or 2
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
    view_mode = request.GET.get("view", "grid").strip()

    rooms_qs = m.Room.objects.all().prefetch_related(
        Prefetch(
            "allocations",
            queryset=m.RoomAllocation.objects.filter(end_date__isnull=True).select_related("student__user")
        )
    )

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

    if q:
        q_lower = q.lower()
        rooms = [
            r for r in rooms
            if (q_lower in str(r).lower() or
                q_lower in r.block.lower() or
                q_lower in r.floor_name.lower() or
                q_lower in r.number.lower() or
                q_lower in (r.blacklist_reason or "").lower() or
                any(q_lower in a.student.user.display_name.lower() or q_lower in a.student.roll_no.lower() for a in r.allocations.all()))
        ]

    if status == "vacant":
        rooms = [r for r in rooms if r.vacancy > 0 and not r.is_blacklisted]
    elif status == "full":
        rooms = [r for r in rooms if r.raw_vacancy == 0 and not r.is_blacklisted]
    elif status == "empty":
        rooms = [r for r in rooms if r.occupied == 0 and not r.is_blacklisted]

    distinct_blocks = sorted(list(set(m.Room.objects.values_list("block", flat=True))), key=floor_order_key)
    all_blocks = []
    seen_displays = set()
    for b in distinct_blocks:
        rm_dummy = m.Room(block=b)
        disp = rm_dummy.floor_display
        if disp not in seen_displays:
            seen_displays.add(disp)
            all_blocks.append({"code": b, "display": disp, "name": rm_dummy.floor_name})
    all_causes = sorted(list(set(m.Room.objects.filter(is_blacklisted=True).exclude(blacklist_reason="").values_list("blacklist_reason", flat=True))))

    rooms.sort(key=lambda r: (floor_order_key(r.block), r.number))

    # Active complaints per student
    active_complaints_students = set(
        m.Complaint.objects.exclude(status="Resolved").values_list("student_id", flat=True)
    )

    # Enrich each room with inmates and active complaints count
    for r in rooms:
        r.inmates_list = [a.student for a in r.allocations.all()]
        r.inmate_names = [s.user.display_name for s in r.inmates_list]
        r.active_complaints_count = sum(1 for s in r.inmates_list if s.pk in active_complaints_students)

    # Group rooms by floor for Grid View
    grouped_floors = {}
    for r in rooms:
        b = r.block
        if b not in grouped_floors:
            grouped_floors[b] = {
                "floor_code": b,
                "floor_name": r.floor_name,
                "floor_display": r.floor_display,
                "rooms": [],
                "total_capacity": 0,
                "total_occupied": 0,
                "total_vacant": 0,
                "active_complaints": 0,
            }
        grouped_floors[b]["rooms"].append(r)
        grouped_floors[b]["total_capacity"] += r.capacity
        grouped_floors[b]["total_occupied"] += len(r.inmates_list)
        grouped_floors[b]["total_vacant"] += r.vacancy
        grouped_floors[b]["active_complaints"] += r.active_complaints_count

    sorted_floor_keys = sorted(grouped_floors.keys(), key=floor_order_key)
    floor_groups = [grouped_floors[k] for k in sorted_floor_keys]

    total_beds = sum(r.capacity for r in rooms)
    total_occupied = sum(len(r.inmates_list) for r in rooms)
    total_vacant = sum(r.vacancy for r in rooms)
    total_maintenance = sum(1 for r in rooms if r.is_blacklisted)
    total_active_complaints = sum(r.active_complaints_count for r in rooms)

    return show(
        request,
        "rooms_list",
        rooms=rooms,
        floor_groups=floor_groups,
        all_blocks=all_blocks,
        all_causes=all_causes,
        filter_q=q,
        filter_block=block,
        filter_status=status,
        filter_cause=cause,
        filter_capacity=capacity,
        total_rooms=len(rooms),
        total_beds=total_beds,
        total_occupied=total_occupied,
        total_vacant=total_vacant,
        total_maintenance=total_maintenance,
        total_active_complaints=total_active_complaints,
        view_mode=view_mode,
    )


@staff
def room_detail(request, pk):
    room = get_object_or_404(m.Room, pk=pk)
    today = date.today()

    active_allocs = list(
        m.RoomAllocation.objects.filter(room=room, end_date__isnull=True)
        .select_related("student__user", "student__parent__user")
        .order_by("start_date")
    )

    inmates = []
    for a in active_allocs:
        s = a.student
        today_att = s.attendance.filter(date=today).first()
        today_status = today_att.status if today_att else None

        balance = s.fee_balance
        has_fines = s.fees.filter(fine_amount__gt=0).exists()

        active_leave = s.leaves.filter(
            status="Approved",
            from_date__lte=today,
            to_date__gte=today,
        ).first()

        parent_user = getattr(getattr(s, "parent", None), "user", None)

        inmates.append({
            "allocation": a,
            "student": s,
            "today_status": today_status,
            "fee_balance": balance,
            "has_fines": has_fines,
            "active_leave": active_leave,
            "parent_name": parent_user.display_name if parent_user else "Not Registered",
            "parent_phone": parent_user.phone if (parent_user and parent_user.phone) else "",
            "student_phone": s.user.phone or "",
        })

    empty_beds = list(range(room.vacancy))

    all_room_students = list(m.Student.objects.filter(allocations__room=room).distinct())
    complaints_qs = m.Complaint.objects.filter(
        Q(student__in=all_room_students) |
        Q(description__icontains=str(room)) |
        Q(title__icontains=str(room))
    ).select_related("student__user").distinct().order_by("-created_at")

    active_complaints = [c for c in complaints_qs if c.status != "Resolved"]
    resolved_complaints = [c for c in complaints_qs if c.status == "Resolved"]

    past_allocs = list(
        m.RoomAllocation.objects.filter(room=room, end_date__isnull=False)
        .select_related("student__user")
        .order_by("-end_date")[:10]
    )

    room_announcements = list(
        room.announcements.select_related("created_by").order_by("-created_at")
    )

    return show(
        request,
        "room_detail",
        room=room,
        inmates=inmates,
        empty_beds=empty_beds,
        active_complaints=active_complaints,
        resolved_complaints=resolved_complaints,
        past_allocs=past_allocs,
        room_announcements=room_announcements,
        total_inmates=len(inmates),
        today=today,
    )


@staff
@require_POST
def room_announcement_create(request, pk):
    room = get_object_or_404(m.Room, pk=pk)
    title = request.POST.get("title", "").strip()
    body = request.POST.get("body", "").strip()
    audience = request.POST.get("audience", "student").strip()
    if audience not in ("student", "parent", "all"):
        audience = "student"

    if not title or not body:
        messages.error(request, "Please enter both a title and details for the announcement.")
        return redirect("room_detail", pk=room.pk)

    anc = m.Announcement.objects.create(
        room=room,
        title=title,
        body=body,
        audience=audience,
        created_by=request.user,
    )
    inmate_users = [a.student.user for a in room.allocations.filter(end_date__isnull=True)]
    parent_users = [a.student.parent.user for a in room.allocations.filter(end_date__isnull=True) if a.student.parent] if audience in ("all", "parent") else []
    target_users = inmate_users if audience == "student" else (inmate_users + parent_users if audience == "all" else parent_users)
    notify_users(
        recipients=target_users,
        title=f"Room {room} Notice: {title}",
        message=body[:200],
        category="announcement",
        link=reverse("home"),
        actor=request.user,
    )
    messages.success(request, f"Announcement successfully posted to Room {room} inmates!")
    return redirect("room_detail", pk=room.pk)


@staff
@require_POST
def room_announcement_delete(request, pk):
    anc = get_object_or_404(m.Announcement, pk=pk)
    room_pk = anc.room_id
    anc.delete()
    messages.success(request, "Room announcement removed.")
    if room_pk:
        return redirect("room_detail", pk=room_pk)
    return redirect("announcements")


@staff
def complaint_edit(request, pk):
    obj = get_object_or_404(m.Complaint.objects.select_related("student__user"), pk=pk)
    old_status = obj.status
    old_note = obj.staff_note or ""
    form = f.ComplaintStaffForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        updated_c = form.save()
        new_status = updated_c.status
        new_note = updated_c.staff_note or ""
        # If status changed or staff reply/note changed, notify student
        if new_status != old_status or new_note != old_note:
            note_text = f" Staff reply: '{new_note[:100]}'" if new_note else ""
            notify_user(
                recipient=obj.student.user,
                title=f"Reply to Complaint: {obj.title}",
                message=f"Status set to '{new_status}'.{note_text}",
                category="complaint",
                link=reverse("my_complaints"),
                actor=request.user,
            )
        messages.success(request, "Complaint updated.")
        return redirect("complaints")
    return show(request, "form", title="Manage Complaint", form=form, back=reverse("complaints"))


@login_required
def notifications_list(request):
    filter_mode = request.GET.get("filter", "all").strip()
    qs = request.user.notifications.all().select_related("actor")
    if filter_mode == "unread":
        qs = qs.filter(is_read=False)

    total_count = request.user.notifications.count()
    unread_count = request.user.notifications.filter(is_read=False).count()

    return show(
        request,
        "notifications",
        title="Notifications",
        notifications=list(qs[:60]),
        total_count=total_count,
        unread_count=unread_count,
        filter_mode=filter_mode,
    )


@login_required
@require_POST
def notifications_mark_all_read(request):
    request.user.notifications.filter(is_read=False).update(is_read=True)
    messages.success(request, "All notifications marked as read.")
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("notifications")
    return redirect(next_url)


@login_required
def notification_read(request, pk):
    notif = get_object_or_404(m.Notification, pk=pk, recipient=request.user)
    if not notif.is_read:
        notif.is_read = True
        notif.save(update_fields=["is_read"])
    if notif.link:
        return redirect(notif.link)
    next_url = request.GET.get("next") or request.META.get("HTTP_REFERER") or reverse("notifications")
    return redirect(next_url)


@login_required
@require_POST
def notification_delete(request, pk):
    notif = get_object_or_404(m.Notification, pk=pk, recipient=request.user)
    notif.delete()
    messages.success(request, "Notification removed.")
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("notifications")
    return redirect(next_url)


@staff
def student_detail(request, pk):
    s = get_object_or_404(m.Student.objects.select_related("user", "parent__user"), pk=pk)
    return show(
        request,
        "overview",
        staff_view=True,
        **overview(s, request.user),
    )


@staff
@require_POST
def student_allocate(request, pk):
    s = get_object_or_404(m.Student.objects.select_related("user", "parent__user"), pk=pk)
    room_id = request.POST.get("room_id", "").strip()
    start_date_str = request.POST.get("start_date", "").strip()

    if not room_id:
        messages.error(request, "Please select a room to allocate.")
        return redirect("student_detail", pk=s.pk)

    room = get_object_or_404(m.Room, pk=room_id)

    if room.is_blacklisted:
        messages.error(request, f"Room {room} is blacklisted ({room.blacklist_reason or 'Maintenance'}) and cannot accept allocations.")
        return redirect("student_detail", pk=s.pk)

    if room.vacancy <= 0:
        messages.error(request, f"Room {room} is already full (capacity: {room.capacity}).")
        return redirect("student_detail", pk=s.pk)

    active_alloc = m.RoomAllocation.objects.filter(student=s, end_date__isnull=True).first()
    if active_alloc:
        messages.warning(request, f"{s.user.display_name} is already allocated to Room {active_alloc.room}.")
        return redirect("student_detail", pk=s.pk)

    alloc_date = date.today()
    if start_date_str:
        try:
            alloc_date = parse_date(start_date_str) or date.today()
        except Exception:
            alloc_date = date.today()

    allocation = m.RoomAllocation(student=s, room=room, start_date=alloc_date)
    try:
        allocation.full_clean()
        allocation.save()
    except ValidationError as e:
        messages.error(request, f"Allocation failed: {e}")
        return redirect("student_detail", pk=s.pk)

    # Notify student
    notify_user(
        recipient=s.user,
        actor=request.user,
        category="announcement",
        title="Hostel Room Allocated",
        message=f"You have been allocated to Room {room} ({room.floor_display}) starting from {alloc_date}.",
        link=reverse("home"),
    )

    # Notify parent if registered
    if s.parent and s.parent.user:
        notify_user(
            recipient=s.parent.user,
            actor=request.user,
            category="announcement",
            title="Ward Room Allocated",
            message=f"Your ward {s.user.display_name} has been allocated to Room {room} ({room.floor_display}) starting from {alloc_date}.",
            link=reverse("home"),
        )

    messages.success(request, f"{s.user.display_name} allocated to Room {room} successfully.")
    return redirect("student_detail", pk=s.pk)


def _enact_student_suspension(s, actor, reason, start_date, end_date=None):
    """Apply hostel suspension to a student, notify stakeholders, and post student-board announcement."""
    s.is_suspended = True
    s.suspension_reason = reason
    s.suspension_start = start_date
    s.suspension_end = end_date
    s.suspended_by = actor
    s.suspended_at = timezone.now()
    s.save(update_fields=["is_suspended", "suspension_reason", "suspension_start", "suspension_end", "suspended_by", "suspended_at"])

    notify_user(
        recipient=s.user,
        title="Hostel Resident Access Suspended",
        message=f"You have been suspended from entering or residing in the hostel by Administration. Reason: {reason}.",
        category="general",
        link=reverse("home"),
        actor=actor,
    )
    if s.parent and s.parent.user:
        notify_user(
            recipient=s.parent.user,
            title=f"Ward Suspended: {s.user.display_name}",
            message=f"Your ward {s.user.display_name} ({s.roll_no}) has been suspended from entering the hostel by Administration. Reason: {reason}.",
            category="general",
            link=reverse("child", args=[s.pk]),
            actor=actor,
        )

    wardens = list(m.User.objects.filter(role="warden", is_active=True))
    if wardens:
        notify_users(
            recipients=wardens,
            title=f"Student Suspended: {s.user.display_name} ({s.roll_no})",
            message=f"Suspension enacted by Admin {actor.display_name}. Student is barred from hostel entry. Reason: {reason}.",
            category="general",
            link=reverse("student_detail", args=[s.pk]),
            actor=actor,
        )

    date_desc = f"From {date_s(start_date)} to {date_s(end_date)}" if end_date else f"From {date_s(start_date)} (Until further notice)"
    announcement_body = (
        f"Resident: {s.user.display_name} ({s.roll_no})\n"
        f"Details: {reason}\n"
        f"Period: {date_desc}\n\n"
        f"Notice: The student is barred from entering the hostel premises during this period."
    )
    m.Announcement.objects.create(
        title=f"Suspension Notice: {s.user.display_name} ({s.roll_no})",
        body=announcement_body,
        audience="student",
        student=s,
        room=None,
        created_by=actor,
    )


def _lift_student_suspension(s, actor):
    """Revoke hostel suspension, clear suspension announcements, and notify student and parent."""
    s.is_suspended = False
    s.suspension_reason = ""
    s.suspension_start = None
    s.suspension_end = None
    s.save(update_fields=["is_suspended", "suspension_reason", "suspension_start", "suspension_end"])

    m.Announcement.objects.filter(student=s, title__startswith="Suspension Notice:").delete()

    notify_user(
        recipient=s.user,
        title="Hostel Suspension Lifted",
        message="Your hostel suspension has been revoked by Administration. Entry and resident access are restored.",
        category="general",
        link=reverse("home"),
        actor=actor,
    )
    if s.parent and s.parent.user:
        notify_user(
            recipient=s.parent.user,
            title=f"Suspension Lifted: {s.user.display_name}",
            message=f"The hostel suspension for your ward {s.user.display_name} has been lifted. Hostel entry access is reinstated.",
            category="general",
            link=reverse("child", args=[s.pk]),
            actor=actor,
        )


@role_required("admin")
@require_POST
def student_suspend(request, pk):
    s = get_object_or_404(m.Student.objects.select_related("user", "parent__user"), pk=pk)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "A reason is required to suspend a student.")
        return redirect("student_detail", pk=pk)

    start_date_str = request.POST.get("start_date", "").strip()
    end_date_str = request.POST.get("end_date", "").strip()
    start_date = parse_date(start_date_str) or date.today()
    end_date = parse_date(end_date_str) if end_date_str else None

    if end_date and end_date < start_date:
        messages.error(request, "Suspension end date cannot be earlier than start date.")
        return redirect("student_detail", pk=pk)

    _enact_student_suspension(s, request.user, reason, start_date, end_date)
    messages.success(request, f"Student {s.user.display_name} ({s.roll_no}) has been suspended. Entry to the hostel is barred.")
    return redirect("student_detail", pk=pk)


@role_required("admin")
@require_POST
def student_reinstate(request, pk):
    s = get_object_or_404(m.Student.objects.select_related("user", "parent__user"), pk=pk)
    _lift_student_suspension(s, request.user)
    messages.success(request, f"Suspension lifted for {s.user.display_name}. Hostel entry access has been reinstated.")
    return redirect("student_detail", pk=pk)


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
            if s.is_currently_suspended and st == "P":
                messages.warning(request, f"{s.user.display_name} is suspended by administration and barred from hostel entry. Attendance recorded as Absent.")
                st = "A"
            if st in ("P", "A", "L"):
                m.Attendance.objects.update_or_create(student=s, date=day, defaults={"status": st})
        messages.success(request, f"Attendance saved for {date_s(day)}.")
        next_url = request.POST.get("next")
        if next_url:
            return redirect(next_url)
        return redirect(f"{reverse('attendance')}?date={day.isoformat()}")

    # Room mappings for active students
    active_allocs = m.RoomAllocation.objects.filter(end_date__isnull=True).select_related("room")
    student_rooms = {a.student_id: a.room for a in active_allocs}
    all_rooms = list(m.Room.objects.all().order_by("block", "number"))

    # Day stats across ALL staying students (unfiltered)
    total_staying = len(students)
    day_suspended = sum(1 for s in students if s.is_currently_suspended)
    day_present = sum(1 for s in students if (existing.get(s.pk) or ("L" if s.pk in on_leave else ("A" if s.is_currently_suspended else "P"))) == "P")
    day_absent = sum(1 for s in students if (existing.get(s.pk) or ("L" if s.pk in on_leave else ("A" if s.is_currently_suspended else "P"))) == "A")
    day_leave = sum(1 for s in students if (existing.get(s.pk) or ("L" if s.pk in on_leave else ("A" if s.is_currently_suspended else "P"))) == "L")
    day_pct = round(100 * day_present / total_staying) if total_staying > 0 else 0

    # Filters
    filter_q = request.GET.get("q", "").strip()
    filter_room = request.GET.get("room", "").strip()
    filter_status = request.GET.get("status", "").strip()

    rows = []
    for s in students:
        st = existing.get(s.pk) or ("L" if s.pk in on_leave else ("A" if s.is_currently_suspended else "P"))
        s_room = student_rooms.get(s.pk)
        is_lv = s.pk in on_leave

        if filter_q:
            q_low = filter_q.lower()
            if q_low not in s.roll_no.lower() and q_low not in s.user.display_name.lower():
                continue

        if filter_room:
            if filter_room.isdigit():
                if not s_room or s_room.pk != int(filter_room):
                    continue
            else:
                if not s_room:
                    continue
                if (s_room.block.lower() != filter_room.lower() and
                    s_room.number.lower() != filter_room.lower() and
                    str(s_room).lower() != filter_room.lower()):
                    continue

        if filter_status == "A" and st != "A":
            continue
        elif filter_status == "L" and st != "L":
            continue
        elif filter_status == "P" and st != "P":
            continue
        elif filter_status == "absent_or_leave" and st not in ("A", "L"):
            continue
        elif filter_status == "suspended" and not s.is_currently_suspended:
            continue

        rows.append((s, st, is_lv, s_room))

    prev_day = day - timedelta(days=1)
    next_day = day + timedelta(days=1)
    is_today = (day == date.today())

    return show(
        request,
        "attendance",
        rows=rows,
        day=day,
        prev_day=prev_day,
        next_day=next_day,
        is_today=is_today,
        saved=bool(existing),
        total_staying=total_staying,
        day_suspended=day_suspended,
        day_present=day_present,
        day_absent=day_absent,
        day_leave=day_leave,
        day_pct=day_pct,
        all_rooms=all_rooms,
        filter_q=filter_q,
        filter_room=filter_room,
        filter_status=filter_status,
        filtered_count=len(rows),
    )


@staff
def attendance_history(request):
    # 1. Determine Selected Month
    month_str = request.GET.get("month", "").strip()
    if not month_str or not re.match(r"^\d{4}-\d{2}$", month_str):
        month_str = date.today().strftime("%Y-%m")

    try:
        year, month_num = map(int, month_str.split("-"))
        selected_month_date = date(year, month_num, 1)
    except ValueError:
        selected_month_date = date.today().replace(day=1)
        year, month_num = selected_month_date.year, selected_month_date.month
        month_str = selected_month_date.strftime("%Y-%m")

    if month_num == 1:
        prev_month_str = f"{year - 1:04d}-12"
    else:
        prev_month_str = f"{year:04d}-{month_num - 1:02d}"

    if month_num == 12:
        next_month_str = f"{year + 1:04d}-01"
    else:
        next_month_str = f"{year:04d}-{month_num + 1:02d}"

    month_label = selected_month_date.strftime("%B %Y")

    # 2. Discover available months
    existing_months = set(
        d.strftime("%Y-%m") for d in m.Attendance.objects.dates("date", "month", order="DESC")
    )
    today = date.today()
    for i in range(6):
        m_calc = today.month - i
        y_calc = today.year
        while m_calc <= 0:
            m_calc += 12
            y_calc -= 1
        existing_months.add(f"{y_calc:04d}-{m_calc:02d}")
    existing_months.add(month_str)

    available_months = []
    for ym in sorted(existing_months, reverse=True):
        y_int, m_int = map(int, ym.split("-"))
        available_months.append({"key": ym, "label": date(y_int, m_int, 1).strftime("%B %Y")})

    # 3. Room mapping for students
    active_allocs = m.RoomAllocation.objects.filter(end_date__isnull=True).select_related("room")
    student_room_map = {a.student_id: a.room for a in active_allocs}
    all_rooms = list(m.Room.objects.all().order_by("block", "number"))

    # 4. Base QuerySet for this month
    month_qs = m.Attendance.objects.filter(
        date__year=year,
        date__month=month_num
    ).select_related("student__user")

    total_records = month_qs.count()
    month_present = month_qs.filter(status="P").count()
    month_absent = month_qs.filter(status="A").count()
    month_leave = month_qs.filter(status="L").count()
    distinct_dates = list(month_qs.values_list("date", flat=True).distinct().order_by("-date"))
    days_recorded = len(distinct_dates)
    tot_eval = month_present + month_absent
    avg_present_pct = round(100 * month_present / tot_eval) if tot_eval > 0 else (100 if month_present > 0 else 0)

    # 5. Filters
    filter_q = request.GET.get("q", "").strip()
    filter_room = request.GET.get("room", "").strip()
    filter_status = request.GET.get("status", "").strip()
    filter_date = request.GET.get("date", "").strip()
    view_mode = request.GET.get("view_mode", "logs").strip()
    if view_mode not in ("logs", "summary"):
        view_mode = "logs"

    filtered_qs = month_qs

    if filter_date:
        try:
            d_val = date.fromisoformat(filter_date)
            filtered_qs = filtered_qs.filter(date=d_val)
        except ValueError:
            pass

    if filter_q:
        filtered_qs = filtered_qs.filter(
            Q(student__user__first_name__icontains=filter_q) |
            Q(student__user__last_name__icontains=filter_q) |
            Q(student__roll_no__icontains=filter_q) |
            Q(student__user__username__icontains=filter_q)
        )

    if filter_status == "A":
        filtered_qs = filtered_qs.filter(status="A")
    elif filter_status == "L":
        filtered_qs = filtered_qs.filter(status="L")
    elif filter_status == "P":
        filtered_qs = filtered_qs.filter(status="P")
    elif filter_status == "absent_or_leave":
        filtered_qs = filtered_qs.filter(status__in=["A", "L"])

    if filter_room:
        if filter_room.isdigit():
            r_st_ids = m.RoomAllocation.objects.filter(room_id=int(filter_room)).values_list("student_id", flat=True)
            filtered_qs = filtered_qs.filter(student_id__in=r_st_ids)
        else:
            r_st_ids = m.RoomAllocation.objects.filter(
                Q(room__number__iexact=filter_room) | Q(room__block__iexact=filter_room)
            ).values_list("student_id", flat=True)
            filtered_qs = filtered_qs.filter(student_id__in=r_st_ids)

    # 6. CSV Export
    if request.GET.get("export") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="attendance_{month_str}.csv"'
        w = csv.writer(resp)
        w.writerow(["Date", "Roll no", "Name", "Room", "Status"])
        for a in filtered_qs.order_by("-date", "student__roll_no"):
            r = student_room_map.get(a.student_id)
            w.writerow([a.date.isoformat(), a.student.roll_no, a.student.user.display_name, str(r) if r else "—", a.get_status_display()])
        return resp

    # 7. Formatted Logs
    logs = []
    for a in filtered_qs.order_by("-date", "student__roll_no")[:500]:
        r = student_room_map.get(a.student_id)
        logs.append({
            "record": a,
            "student": a.student,
            "date": a.date,
            "status": a.status,
            "status_display": a.get_status_display(),
            "room": r,
        })

    # 8. Student Monthly Summary
    student_stats = {}
    for a in month_qs:
        s_id = a.student_id
        if s_id not in student_stats:
            student_stats[s_id] = {"present": 0, "absent": 0, "leave": 0, "total": 0, "student": a.student}
        st_data = student_stats[s_id]
        st_data["total"] += 1
        if a.status == "P":
            st_data["present"] += 1
        elif a.status == "A":
            st_data["absent"] += 1
        elif a.status == "L":
            st_data["leave"] += 1

    summary_rows = []
    for s_id, s_data in student_stats.items():
        s = s_data["student"]
        r = student_room_map.get(s_id)
        p = s_data["present"]
        ab = s_data["absent"]
        lv = s_data["leave"]
        tot = p + ab
        pct = round(100 * p / tot) if tot > 0 else (100 if p > 0 else 0)
        s_data["percent"] = pct
        s_data["room"] = r

        if filter_q:
            q_low = filter_q.lower()
            if q_low not in s.roll_no.lower() and q_low not in s.user.display_name.lower():
                continue

        if filter_room:
            if filter_room.isdigit() and (not r or r.pk != int(filter_room)):
                continue
            elif not filter_room.isdigit() and (not r or (r.number.lower() != filter_room.lower() and r.block.lower() != filter_room.lower() and str(r).lower() != filter_room.lower())):
                continue

        if filter_status == "A" and ab == 0:
            continue
        elif filter_status == "L" and lv == 0:
            continue
        elif filter_status == "P" and p == 0:
            continue
        elif filter_status == "absent_or_leave" and (ab == 0 and lv == 0):
            continue

        summary_rows.append(s_data)

    summary_rows.sort(key=lambda x: (x["room"] is None, str(x["room"]) if x["room"] else "", x["student"].roll_no))

    return show(
        request,
        "attendance_history",
        month=month_str,
        month_label=month_label,
        prev_month=prev_month_str,
        next_month=next_month_str,
        available_months=available_months,
        distinct_dates=distinct_dates,
        all_rooms=all_rooms,
        total_records=total_records,
        month_present=month_present,
        month_absent=month_absent,
        month_leave=month_leave,
        days_recorded=days_recorded,
        avg_present_pct=avg_present_pct,
        filter_q=filter_q,
        filter_room=filter_room,
        filter_status=filter_status,
        filter_date=filter_date,
        view_mode=view_mode,
        logs=logs,
        summary_rows=summary_rows,
        total_filtered_logs=filtered_qs.count(),
        total_filtered_summary=len(summary_rows),
    )


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
    targets = [lv.student.user]
    if lv.student.parent:
        targets.append(lv.student.parent.user)
    notify_users(
        recipients=targets,
        title=f"Leave Request {lv.status}",
        message=f"Leave request from {lv.from_date.strftime('%d %b')} to {lv.to_date.strftime('%d %b')} was {lv.status.lower()} by the warden.",
        category="leave",
        link=reverse("my_leaves"),
        actor=request.user,
    )
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
        if exp == "fees_pdf":
            pending_fees = [x for x in m.Fee.objects.select_related("student__user").prefetch_related("payments") if x.balance > 0]
            pdf_bytes = generate_fees_report_pdf(
                pending_fees,
                title="Hostel Outstanding Fees & Ledger Report",
                subtitle="All active resident accounts with pending balances",
                staff_user=request.user,
            )
            resp = HttpResponse(pdf_bytes, content_type="application/pdf")
            resp["Content-Disposition"] = f'attachment; filename="pending_fees_{date.today().strftime("%Y%m%d")}.pdf"'
            return resp
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="{exp}_report.csv"'
        w = csv.writer(resp)
        if exp == "fees":
            w.writerow(["Roll no", "Name", "Fee", "Amount", "Paid", "Balance", "Due", "Status"]); w.writerows(fee_rows)
        else:
            w.writerow(["Roll no", "Name", "Attendance %"]); w.writerows(att_rows)
        return resp
    floors = {}
    for r in m.Room.objects.all():
        b = floors.setdefault(r.block, [0, 0])
        b[0] += r.capacity; b[1] += r.occupied
    sorted_codes = sorted(floors.keys(), key=floor_order_key)
    occupancy = [(m.Room(block=k).floor_display, floors[k][0], floors[k][1], max(0, floors[k][0] - floors[k][1])) for k in sorted_codes]
    low = sorted([r for r in att_rows if r[2] != ""], key=lambda r: r[2])[:10]
    return show(request, "reports", occupancy=occupancy, fee_rows=fee_rows[:50], low=low, badge=badge)


# ---------- student ----------
def _mine(request):
    return request.user.student


@login_required
def my_announcements(request):
    """
    Dedicated announcement center for students (and parents).
    Provides room-specific notices and general hostel broadcasts with filtering and search.
    """
    from datetime import datetime, timezone
    u = request.user
    q = request.GET.get("q", "").strip()
    filter_type = request.GET.get("type", "all").strip()

    if u.role == "student":
        s = getattr(u, "student", None)
        room = s.room if s else None
        aud = ["all", "student"]
        if room:
            base_qs = m.Announcement.objects.filter(
                Q(room=room, audience__in=aud) | Q(room__isnull=True, audience__in=aud)
            )
        else:
            base_qs = m.Announcement.objects.filter(room__isnull=True, audience__in=aud)
        room_label = f"Room {room}" if room else "Unassigned"
    elif u.role == "parent":
        p = getattr(u, "parent", None)
        child_rooms = [c.room for c in p.children.all() if c.room] if p else []
        child_pks = list(p.children.values_list("pk", flat=True)) if p else []
        aud = ["all", "parent"]
        if child_rooms:
            base_qs = m.Announcement.objects.filter(
                Q(room__in=child_rooms, audience__in=aud) | Q(room__isnull=True, audience__in=aud) | Q(student__in=child_pks)
            )
        else:
            base_qs = m.Announcement.objects.filter(
                Q(room__isnull=True, audience__in=aud) | Q(student__in=child_pks)
            )
        room_label = ", ".join(str(r) for r in child_rooms) if child_rooms else "Children's Rooms"
    else:
        base_qs = m.Announcement.objects.all()
        room_label = "All Rooms"

    all_notices = list(base_qs.select_related("room", "created_by").order_by("-created_at"))
    room_notices = [a for a in all_notices if a.room]
    general_notices = [a for a in all_notices if not a.room]

    notices = all_notices
    if filter_type == "room":
        notices = room_notices
    elif filter_type == "general":
        notices = general_notices

    if q:
        q_lower = q.lower()
        notices = [
            a for a in notices
            if (
                q_lower in a.title.lower() or
                q_lower in a.body.lower() or
                (a.room and q_lower in str(a.room).lower()) or
                (a.created_by and q_lower in a.created_by.display_name.lower())
            )
        ]

    now = datetime.now(timezone.utc)
    for a in notices:
        diff_hours = (now - a.created_at).total_seconds() / 3600
        a.is_recent = diff_hours <= 48

    return show(
        request,
        "my_announcements",
        title="Hostel Announcements & Circulars",
        notices=notices,
        total_count=len(all_notices),
        room_count=len(room_notices),
        general_count=len(general_notices),
        filter_type=filter_type,
        q=q,
        room_label=room_label,
    )


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
        c = form.save(commit=False)
        c.student = s
        c.save()
        staff_users = list(m.User.objects.filter(role__in=STAFF, is_active=True))
        notify_users(
            recipients=staff_users,
            title=f"New Complaint: {c.title}",
            message=f"Filed by {s.user.display_name} ({s.roll_no}) - Room {s.room or 'Unassigned'}: {c.description[:150]}",
            category="complaint",
            link=reverse("complaints_edit", args=[c.pk]),
            actor=request.user,
        )
        messages.success(request, "Complaint submitted to hostel administration.")
        return redirect("my_complaints")

    complaints_list = list(s.complaints.all())
    return show(
        request,
        "my_complaints",
        title="Hostel Complaints & Maintenance Desk",
        form=form,
        complaints=complaints_list,
        total_count=len(complaints_list),
        open_count=sum(1 for x in complaints_list if x.status != "Resolved"),
        resolved_count=sum(1 for x in complaints_list if x.status == "Resolved"),
        badge=badge,
    )


@role_required("student")
@require_POST
def my_complaint_resolve(request, pk):
    s = _mine(request)
    c = get_object_or_404(m.Complaint, pk=pk, student=s)
    if c.status == "Resolved":
        messages.info(request, f"Complaint '{c.title}' is already marked as solved.")
    else:
        c.status = "Resolved"
        c.save(update_fields=["status"])
        staff_users = list(m.User.objects.filter(role__in=STAFF, is_active=True))
        notify_users(
            recipients=staff_users,
            title=f"Complaint Solved: {c.title}",
            message=f"{s.user.display_name} marked complaint #{c.pk} as solved.",
            category="complaint",
            link=reverse("complaints_edit", args=[c.pk]),
            actor=request.user,
        )
        messages.success(request, f"Complaint '{c.title}' has been marked as solved. Thank you!")
    return redirect(request.POST.get("next") or "my_complaints")


@role_required("student")
@require_POST
def my_complaint_reopen(request, pk):
    s = _mine(request)
    c = get_object_or_404(m.Complaint, pk=pk, student=s)
    if c.status != "Resolved":
        messages.info(request, f"Complaint '{c.title}' is currently {c.status}.")
    else:
        c.status = "In Progress"
        c.save(update_fields=["status"])
        staff_users = list(m.User.objects.filter(role__in=STAFF, is_active=True))
        notify_users(
            recipients=staff_users,
            title=f"Complaint Reopened: {c.title}",
            message=f"{s.user.display_name} reopened complaint #{c.pk} for staff review.",
            category="complaint",
            link=reverse("complaints_edit", args=[c.pk]),
            actor=request.user,
        )
        messages.success(request, f"Complaint '{c.title}' reopened for staff review.")
    return redirect(request.POST.get("next") or "my_complaints")


@role_required("student")
def my_leaves(request):
    s = _mine(request)
    if s.is_currently_suspended:
        messages.error(request, "Your hostel resident access is currently suspended. Leave requests are disabled.")
        rows = [(date_s(x.from_date), date_s(x.to_date), x.reason, badge(x.status)) for x in s.leaves.all()]
        return show(request, "plain", title="Leave requests", headers=["From", "To", "Reason", "Status"], rows=rows)
    form = f.LeaveForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        lv = form.save(commit=False); lv.student = s; lv.save()
        staff_users = list(m.User.objects.filter(role__in=STAFF, is_active=True))
        notify_users(
            recipients=staff_users,
            title=f"New Leave Request: {s.user.display_name}",
            message=f"{s.user.display_name} ({s.roll_no}) requested leave from {lv.from_date.strftime('%d %b')} to {lv.to_date.strftime('%d %b')}. Reason: {lv.reason}",
            category="leave",
            link=reverse("leaves"),
            actor=request.user,
        )
        messages.success(request, "Leave request sent to the warden.")
        return redirect("my_leaves")
    rows = [(date_s(x.from_date), date_s(x.to_date), x.reason, badge(x.status)) for x in s.leaves.all()]
    return show(request, "plain", title="Leave requests", headers=["From", "To", "Reason", "Status"], rows=rows,
                form=form, form_title="Request leave", button="Send request")


@role_required("student", "parent")
def my_fees(request):
    u = request.user
    if u.role == "parent":
        p = getattr(u, "parent", None)
        children = list(p.children.select_related("user").all()) if p else []
        if not children:
            return show(
                request, "plain", title="Fees & Mess Bill Breakdown",
                headers=["Fee Title", "Room Rent", "Food Charge", "Mess Cut Rebate", "Late Fine", "Total Amount", "Paid", "Balance", "Due Date", "Status", ""],
                rows=[],
                note="No student accounts are currently linked to your parent profile.",
            )
        child_id = request.GET.get("child")
        s = None
        if child_id:
            s = next((c for c in children if str(c.pk) == str(child_id)), None)
        if not s:
            s = children[0]
    else:
        children = []
        s = _mine(request)

    for f_obj in s.fees.filter(due_date__lt=date.today()):
        f_obj.sync_late_fine()

    rows = [
        (
            x.title,
            f"₹{x.rent_amount}" if x.rent_amount else "—",
            f"₹{x.food_amount}" if x.food_amount else "—",
            f"-₹{x.mess_cut_amount} ({x.mess_cut_days}d)" if (x.mess_cut_amount and x.mess_cut_amount > 0) else "—",
            f"+₹{x.fine_amount} ({x.overdue_days}d late)" if (x.fine_amount and x.fine_amount > 0) else ("Waived" if x.fine_waived else "—"),
            f"₹{x.amount}",
            f"₹{x.paid}",
            f"₹{x.balance}",
            date_s(x.due_date),
            badge(x.status),
            format_html('<a class="btn btn-sm btn-outline-danger" href="{}">Receipt</a>', reverse("fee_pdf", args=[x.pk])),
        )
        for x in s.fees.all()
    ]

    export_url = reverse("my_fees_export_pdf")
    if u.role == "parent":
        export_url += f"?child={s.pk}"

    child_switcher = ""
    if u.role == "parent" and len(children) > 1:
        pills = []
        for c in children:
            active_cls = "btn-navy" if c.pk == s.pk else "btn-outline-secondary"
            pills.append(f'<a href="?child={c.pk}" class="btn btn-sm {active_cls} me-2">{c.user.display_name}</a>')
        child_switcher = f'<div class="mb-3 d-flex align-items-center gap-2"><span class="small fw-semibold text-secondary">Select Ward:</span> {"".join(pills)}</div>'

    note_text = f'Hostel fees must be settled before due date. A late fine of ₹300 is charged for every 5 days past due. <a class="btn btn-sm btn-outline-danger ms-2" href="{export_url}">Export All Fees PDF</a>'
    note = format_html('{}{}', mark_safe(child_switcher), mark_safe(note_text)) if child_switcher else format_html('{}', mark_safe(note_text))

    title = f"Fees & Mess Bill Breakdown · {s.user.display_name} ({s.roll_no})" if u.role == "parent" else "Fees & Mess Bill Breakdown"

    return show(
        request, "plain", title=title,
        headers=["Fee Title", "Room Rent", "Food Charge", "Mess Cut Rebate", "Late Fine", "Total Amount", "Paid", "Balance", "Due Date", "Status", ""],
        rows=rows,
        note=note,
    )


@role_required("student", "parent")
def my_fees_export_pdf(request):
    """Exports all fee statements for the logged-in student (or parent's ward) as a PDF."""
    u = request.user
    if u.role == "parent":
        p = getattr(u, "parent", None)
        children = list(p.children.select_related("user").all()) if p else []
        if not children:
            raise PermissionDenied
        child_id = request.GET.get("child")
        s = None
        if child_id:
            s = next((c for c in children if str(c.pk) == str(child_id)), None)
        if not s:
            s = children[0]
    else:
        s = _mine(request)

    fees = list(s.fees.all())
    for f_obj in fees:
        if f_obj.due_date and f_obj.due_date < date.today():
            f_obj.sync_late_fine()
    pdf_bytes = generate_fees_report_pdf(
        fees,
        title=f"Personal Fee Statement · {s.user.display_name} ({s.roll_no})",
        subtitle=f"Official student account fee ledger for {s.user.display_name}" if u.role == "student" else f"Official ward fee ledger for {s.user.display_name} (Parent: {u.display_name})",
        staff_user=request.user,
    )
    filename = f"my_fees_statement_{s.roll_no}_{date.today().strftime('%Y%m%d')}.pdf"
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required
def menu(request):
    today = date.today()
    today_weekday = today.weekday()  # 0 = Monday, 6 = Sunday
    today_name = dict(m.MealMenu.DAYS).get(today_weekday, "Today")

    meal_meta = [
        {"key": "B", "name": "Breakfast", "timing": "7:30 AM – 9:00 AM"},
        {"key": "L", "name": "Lunch", "timing": "12:30 PM – 2:00 PM"},
        {"key": "S", "name": "Snacks & Tea", "timing": "4:30 PM – 5:30 PM"},
        {"key": "D", "name": "Dinner", "timing": "7:30 PM – 9:00 PM"},
    ]

    all_meals = m.MealMenu.objects.all()
    meals_dict = {(x.day, x.meal): x.items for x in all_meals}

    days_schedule = []
    today_meals = {}

    for day_idx, day_name in m.MealMenu.DAYS:
        day_row = {
            "day_idx": day_idx,
            "day_name": day_name,
            "is_today": (day_idx == today_weekday),
            "meals": [],
        }
        for mm in meal_meta:
            items_str = meals_dict.get((day_idx, mm["key"]), "")
            item_list = [it.strip() for it in items_str.split(",") if it.strip()] if items_str else []
            meal_data = {
                "key": mm["key"],
                "name": mm["name"],
                "timing": mm["timing"],
                "items_str": items_str or "—",
                "items_list": item_list,
            }
            day_row["meals"].append(meal_data)
            if day_idx == today_weekday:
                today_meals[mm["key"]] = meal_data

        days_schedule.append(day_row)

    headers = ["Day"] + [m["name"] for m in meal_meta]
    rows = [
        (d["day_name"], *[m["items_str"] for m in d["meals"]])
        for d in days_schedule
    ]

    return show(
        request,
        "menu",
        title="Weekly Mess Food Menu",
        today=today,
        today_name=today_name,
        today_weekday=today_weekday,
        meal_meta=meal_meta,
        days_schedule=days_schedule,
        today_meals=today_meals,
        headers=headers,
        rows=rows,
        can_manage=request.user.role in STAFF,
    )


@login_required
def student_rooms(request):
    from django.db.models import Prefetch

    q = request.GET.get("q", "").strip()
    block = request.GET.get("block", "").strip()
    sort_by = request.GET.get("sort", "floor_asc").strip()

    distinct_codes = sorted(list(set(m.Room.objects.values_list("block", flat=True))), key=floor_order_key)
    all_blocks = []
    seen_displays = set()
    for b in distinct_codes:
        rm_dummy = m.Room(block=b)
        disp = rm_dummy.floor_display
        if disp not in seen_displays:
            seen_displays.add(disp)
            all_blocks.append({"code": b, "label": disp, "name": rm_dummy.floor_name})

    rooms_qs = m.Room.objects.all().prefetch_related(
        Prefetch(
            "allocations",
            queryset=m.RoomAllocation.objects.filter(end_date__isnull=True).select_related("student__user")
        )
    )

    if block:
        rooms_qs = rooms_qs.filter(block__iexact=block)

    all_rooms = list(rooms_qs)

    if q:
        q_lower = q.lower()
        filtered = []
        for r in all_rooms:
            resident_names = [a.student.user.display_name for a in r.allocations.all()]
            if (q_lower in str(r).lower() or
                q_lower in r.block.lower() or
                q_lower in r.floor_name.lower() or
                q_lower in r.number.lower() or
                any(q_lower in name.lower() for name in resident_names)):
                filtered.append(r)
        all_rooms = filtered

    # Sorting
    if sort_by == "floor_desc":
        all_rooms.sort(key=lambda r: (-floor_order_key(r.block), r.number))
    elif sort_by == "room_asc":
        all_rooms.sort(key=lambda r: r.number)
    elif sort_by == "vacant":
        all_rooms.sort(key=lambda r: (-r.vacancy, floor_order_key(r.block), r.number))
    else:  # floor_asc (default)
        all_rooms.sort(key=lambda r: (floor_order_key(r.block), r.number))

    # Group rooms by floor
    grouped_dict = {}
    room_items = []
    for r in all_rooms:
        residents = [a.student.user.display_name for a in r.allocations.all()]
        item = {
            "room": r,
            "room_str": str(r),
            "block": r.block,
            "floor_name": r.floor_name,
            "floor_display": r.floor_display,
            "number": r.number,
            "capacity": r.capacity,
            "occupied": len(residents),
            "vacancy": r.vacancy,
            "is_blacklisted": r.is_blacklisted,
            "blacklist_reason": r.blacklist_reason,
            "residents": residents,
        }
        room_items.append(item)

        fl_key = r.block
        if fl_key not in grouped_dict:
            grouped_dict[fl_key] = {
                "floor_code": r.block,
                "floor_name": r.floor_name,
                "floor_display": r.floor_display,
                "rooms": [],
                "total_capacity": 0,
                "total_occupied": 0,
            }
        grouped_dict[fl_key]["rooms"].append(item)
        grouped_dict[fl_key]["total_capacity"] += r.capacity
        grouped_dict[fl_key]["total_occupied"] += len(residents)

    if sort_by == "floor_desc":
        sorted_keys = sorted(grouped_dict.keys(), key=lambda k: -floor_order_key(k))
    else:
        sorted_keys = sorted(grouped_dict.keys(), key=floor_order_key)

    floor_groups = [grouped_dict[k] for k in sorted_keys]

    return show(
        request,
        "student_rooms",
        rooms=room_items,
        floor_groups=floor_groups,
        total_rooms=len(room_items),
        blocks=all_blocks,
        selected_block=block,
        sort_by=sort_by,
        q=q,
    )
