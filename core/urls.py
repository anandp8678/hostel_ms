from django.contrib.auth import views as auth
from django.urls import path

from . import views as v

urlpatterns = [
    path("login/", auth.LoginView.as_view(template_name="core/login.html"), name="login"),
    path("logout/", auth.LogoutView.as_view(), name="logout"),
    path("password/", v.PwChange.as_view(), name="password"),
    path("", v.home, name="home"),
    path("children/<int:pk>/", v.child, name="child"),
    path("students/", v.student_list, name="students"),
    path("students/new/", v.student_new, name="students_new"),
    path("students/import/", v.student_import, name="students_import"),
    path("students/import/template/", v.import_template, name="import_template"),
    path("allocations/import/", v.allocation_import, name="allocations_import"),
    path("allocations/import/template/", v.allocation_import_template, name="allocations_import_template"),
    path("rooms/bulk/", v.room_bulk_add, name="rooms_bulk_add"),
    path("rooms/bulk/template/", v.room_bulk_template, name="rooms_bulk_template"),
    path("rooms/directory/", v.student_rooms, name="student_rooms"),
    path("rooms/<int:pk>/", v.room_detail, name="room_detail"),
    path("rooms/<int:pk>/announcement/", v.room_announcement_create, name="room_announcement_create"),
    path("rooms/announcements/<int:pk>/delete/", v.room_announcement_delete, name="room_announcement_delete"),
    path("rooms/", v.room_list, name="rooms"),
    path("rooms/<int:pk>/toggle-blacklist/", v.room_toggle_blacklist, name="room_toggle_blacklist"),
    path("buildings/", v.buildings_list, name="buildings"),
    path("students/<int:pk>/", v.student_detail, name="student_detail"),
    path("students/<int:pk>/allocate/", v.student_allocate, name="student_allocate"),
    path("students/<int:pk>/suspend/", v.student_suspend, name="student_suspend"),
    path("students/<int:pk>/reinstate/", v.student_reinstate, name="student_reinstate"),
    path("attendance/", v.attendance, name="attendance"),
    path("attendance/history/", v.attendance_history, name="attendance_history"),
    path("leaves/", v.leaves, name="leaves"),
    path("leaves/<int:pk>/<str:decision>/", v.leave_decide, name="leave_decide"),
    path("gate/", v.gate_pass_list, name="gate_passes"),
    path("gate/<int:pk>/checkout/", v.gate_pass_checkout, name="gate_pass_checkout"),
    path("gate/<int:pk>/checkin/", v.gate_pass_checkin, name="gate_pass_checkin"),
    path("gate/<int:pk>/pdf/", v.gate_pass_pdf, name="gate_pass_pdf"),
    path("gate/<int:pk>/<str:decision>/", v.gate_pass_decide, name="gate_pass_decide"),
    path("visitors/", v.visitors_list, name="visitors"),
    path("visitors/<int:pk>/checkout/", v.visitor_checkout, name="visitor_checkout"),
    path("allocations/<int:pk>/vacate/", v.allocation_vacate, name="allocation_vacate"),
    path("fees/", v.fees_list, name="fees"),
    path("fees/structure/", v.fee_structure, name="fee_structure"),
    path("fees/calculate/", v.fees_calculate_monthly, name="fees_calculate_monthly"),
    path("fees/export/pdf/", v.fees_export_pdf, name="fees_export_pdf"),
    path("fees/new/", v.fee_create, name="fees_new"),
    path("fees/<int:fee_id>/pay/", v.payment_new, name="payment_new"),
    path("fees/<int:pk>/waive-fine/", v.fee_waive_fine, name="fee_waive_fine"),
    path("fees/<int:pk>/pdf/", v.fee_invoice_pdf, name="fee_pdf"),
    path("wardens/new/", v.warden_new, name="wardens_new"),
    path("reports/", v.reports, name="reports"),
    path("menu/", v.menu, name="menu"),
    path("me/announcements/", v.my_announcements, name="my_announcements"),
    path("me/attendance/", v.my_attendance, name="my_attendance"),
    path("me/leave/", v.my_leaves, name="my_leaves"),
    path("me/gate-pass/", v.my_gate_passes, name="my_gate_passes"),
    path("me/gate-pass/<int:pk>/ack/", v.gate_pass_parent_ack, name="gate_pass_parent_ack"),
    path("me/complaints/", v.my_complaints, name="my_complaints"),
    path("me/complaints/<int:pk>/resolve/", v.my_complaint_resolve, name="my_complaint_resolve"),
    path("me/complaints/<int:pk>/reopen/", v.my_complaint_reopen, name="my_complaint_reopen"),
    path("me/fees/", v.my_fees, name="my_fees"),
    path("me/fees/pdf/", v.my_fees_export_pdf, name="my_fees_export_pdf"),
    path("notifications/", v.notifications_list, name="notifications"),
    path("notifications/read-all/", v.notifications_mark_all_read, name="notifications_mark_all_read"),
    path("notifications/<int:pk>/read/", v.notification_read, name="notification_read"),
    path("notifications/<int:pk>/delete/", v.notification_delete, name="notification_delete"),
    path("complaints/<int:pk>/edit/", v.complaint_edit, name="complaints_edit"),
    path("announcements/new/", v.announcement_new, name="announcements_new"),
]
special = {("fees", "list"), ("fees", "new"), ("wardens", "new"), ("students", "new"), ("complaints", "new"), ("complaints", "edit"), ("announcements", "new"), ("rooms", "list"), ("students", "list"), ("buildings", "list")}
for key, d in v.V.items():
    path_key = "menu/manage" if key == "menu_manage" else key
    for kind, view in d.items():
        if (key, kind) in special:
            continue
        suffix = {"list": "", "new": "new/", "edit": "<int:pk>/edit/", "delete": "<int:pk>/delete/"}[kind]
        name = key if kind == "list" else f"{key}_{kind}"
        urlpatterns.append(path(f"{path_key}/{suffix}", view, name=name))
