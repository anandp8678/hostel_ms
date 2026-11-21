def nav(request):
    u = request.user
    if not u.is_authenticated:
        return {}
    if u.role in ("admin", "warden"):
        items = [("home", "Dashboard"), ("students", "Students"), ("rooms", "Rooms"), ("allocations", "Allocations"),
                 ("attendance", "Attendance"), ("leaves", "Leave requests"), ("complaints", "Complaints"),
                 ("menu_manage", "Meal menu"), ("fees", "Fees"), ("announcements", "Announcements"), ("reports", "Reports")]
        if u.role == "admin":
            items.append(("wardens", "Wardens"))
    elif u.role == "student":
        items = [("home", "Dashboard"), ("my_attendance", "Attendance"), ("my_leaves", "Leave"), ("my_complaints", "Complaints"),
                 ("my_fees", "Fees"), ("menu", "Meal menu")]
    else:
        items = [("home", "My children"), ("menu", "Meal menu")]
    return {"nav_items": items}
