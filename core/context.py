def nav(request):
    u = request.user
    if not u.is_authenticated:
        return {}
    if u.role in ("admin", "warden"):
        items = [("home", "Dashboard"), ("students", "Students"), ("rooms", "Rooms"), ("allocations", "Allocations"),
                 ("attendance", "Attendance"), ("leaves", "Leave requests"), ("gate_passes", "Gate passes"), ("visitors", "Visitors"),
                 ("complaints", "Complaints"), ("menu_manage", "Meal menu"), ("fees", "Fees"), ("announcements", "Announcements"), ("reports", "Reports")]
        if u.role == "admin":
            items.insert(3, ("buildings", "Buildings"))
            items.append(("wardens", "Wardens"))
    elif u.role == "student":
        items = [("home", "Dashboard"), ("my_announcements", "Announcements"), ("student_rooms", "Room directory"), ("my_attendance", "Attendance"),
                 ("my_leaves", "Leave"), ("my_gate_passes", "Gate pass"), ("my_complaints", "Complaints"), ("my_fees", "Fees"), ("menu", "Meal menu")]
    else:
        items = [("home", "My children"), ("my_gate_passes", "Gate passes"), ("my_fees", "Fees"), ("my_announcements", "Announcements"), ("student_rooms", "Room directory"), ("menu", "Meal menu")]

    # Real-time notifications for the topbar bell icon
    notif_qs = u.notifications.all()
    unread_notifs_count = notif_qs.filter(is_read=False).count()
    recent_notifications = list(notif_qs.select_related("actor")[:8])

    return {
        "nav_items": items,
        "unread_notifs_count": unread_notifs_count,
        "recent_notifications": recent_notifications,
        "scoped_building": getattr(u, "scoped_building", None),
        "warden_role_label": getattr(u, "warden_role_label", ""),
    }
