from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from . import models as m

admin.site.register(m.User, UserAdmin)
admin.site.register([m.Parent, m.Student, m.Room, m.RoomAllocation, m.Attendance, m.LeaveRequest, m.Complaint,
                     m.MealMenu, m.Fee, m.Payment, m.Announcement])
