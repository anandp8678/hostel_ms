import io
from datetime import date, datetime
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


class NumberedCanvas(canvas.Canvas):
    """
    Two-pass canvas that dynamically calculates the total page count
    and renders 'Page X of Y' in the running footer.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748b"))

        # Footer
        footer_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(self._pagesize[0] - 36, 20, footer_text)
        self.drawString(36, 20, "Hostel Management System · Official Financial & Billing Record")

        # Running footer line
        self.setStrokeColor(colors.HexColor("#e2e8f0"))
        self.setLineWidth(0.5)
        self.line(36, 30, self._pagesize[0] - 36, 30)
        self.restoreState()


def get_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("ReportTitle", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=colors.HexColor("#0b2545")))
    styles.add(ParagraphStyle("ReportSubtitle", fontName="Helvetica", fontSize=9, leading=12, textColor=colors.HexColor("#475569")))
    styles.add(ParagraphStyle("MetaLabel", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.HexColor("#0b2545")))
    styles.add(ParagraphStyle("MetaVal", fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#334155")))
    styles.add(ParagraphStyle("TableHeader", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white, alignment=1))
    styles.add(ParagraphStyle("TableCell", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=colors.HexColor("#1e293b")))
    styles.add(ParagraphStyle("TableCellBold", fontName="Helvetica-Bold", fontSize=7.5, leading=9.5, textColor=colors.HexColor("#0b2545")))
    styles.add(ParagraphStyle("TableCellRight", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=colors.HexColor("#1e293b"), alignment=2))
    styles.add(ParagraphStyle("TableCellRightBold", fontName="Helvetica-Bold", fontSize=7.5, leading=9.5, textColor=colors.HexColor("#0b2545"), alignment=2))
    styles.add(ParagraphStyle("TableCellCenter", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=colors.HexColor("#1e293b"), alignment=1))
    styles.add(ParagraphStyle("BadgePaid", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=colors.HexColor("#166534"), alignment=1))
    styles.add(ParagraphStyle("BadgeOverdue", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=colors.HexColor("#991b1b"), alignment=1))
    styles.add(ParagraphStyle("BadgeUnpaid", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=colors.HexColor("#854d0e"), alignment=1))
    return styles


def generate_fees_report_pdf(fees_qs, title="Hostel Master Fees & Billing Statement", subtitle="", staff_user=None):
    """
    Generates a master fee ledger PDF report in landscape A4.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=40,
    )
    styles = get_styles()
    story = []

    # Title & Metadata banner
    generated_at = datetime.now().strftime("%d %b %Y, %I:%M %p")
    user_str = staff_user.display_name if staff_user else "Hostel Office"
    if not subtitle:
        subtitle = f"Generated on {generated_at} by {user_str} · All active fee schedules and resident balances"

    header_data = [
        [
            Paragraph(f"<b>{title}</b>", styles["ReportTitle"]),
            Paragraph(f"<b>Hostel Administration</b><br/>{generated_at}", styles["TableCellRight"]),
        ],
        [
            Paragraph(subtitle, styles["ReportSubtitle"]),
            Paragraph(
                f"{'Account / Viewer' if (staff_user and getattr(staff_user, 'role', None) in ('student', 'parent')) else 'Authorized Staff'}: <b>{user_str}</b>",
                styles["TableCellRight"],
            ),
        ],
    ]
    header_table = Table(header_data, colWidths=[540, 230])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 10))

    # Synchronize and aggregate fees
    fee_list = list(fees_qs)
    for f in fee_list:
        if f.due_date and f.due_date < date.today():
            f.sync_late_fine()

    total_count = len(fee_list)
    total_rent = sum(f.rent_amount for f in fee_list)
    total_food = sum(f.food_amount for f in fee_list)
    total_mess_cut = sum(f.mess_cut_amount for f in fee_list)
    total_fine = sum(f.fine_amount for f in fee_list)
    total_billed = sum(f.amount for f in fee_list)
    total_paid = sum(f.paid for f in fee_list)
    total_balance = sum(f.balance for f in fee_list)

    # Summary KPI Cards Table
    summary_data = [
        [
            Paragraph("Total Invoices", styles["MetaLabel"]),
            Paragraph("Total Billed", styles["MetaLabel"]),
            Paragraph("Total Collected", styles["MetaLabel"]),
            Paragraph("Outstanding Balance", styles["MetaLabel"]),
            Paragraph("Mess Cut Savings", styles["MetaLabel"]),
            Paragraph("Overdue Late Fines", styles["MetaLabel"]),
        ],
        [
            Paragraph(f"<b>{total_count}</b>", styles["ReportTitle"]),
            Paragraph(f"<b>₹{total_billed:,.2f}</b>", styles["ReportTitle"]),
            Paragraph(f"<b>₹{total_paid:,.2f}</b>", ParagraphStyle("PaidVal", parent=styles["ReportTitle"], textColor=colors.HexColor("#16a34a"))),
            Paragraph(f"<b>₹{total_balance:,.2f}</b>", ParagraphStyle("BalVal", parent=styles["ReportTitle"], textColor=colors.HexColor("#dc2626") if total_balance > 0 else colors.HexColor("#16a34a"))),
            Paragraph(f"-₹{total_mess_cut:,.2f}", ParagraphStyle("MVal", parent=styles["ReportTitle"], fontSize=13, textColor=colors.HexColor("#059669"))),
            Paragraph(f"+₹{total_fine:,.2f}", ParagraphStyle("FVal", parent=styles["ReportTitle"], fontSize=13, textColor=colors.HexColor("#dc2626"))),
        ],
    ]
    col_w = 770 / 6
    summary_table = Table(summary_data, colWidths=[col_w] * 6)
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 12))

    # Master Table Header
    headers = [
        Paragraph("Roll No", styles["TableHeader"]),
        Paragraph("Resident Name", styles["TableHeader"]),
        Paragraph("Room", styles["TableHeader"]),
        Paragraph("Fee Description", styles["TableHeader"]),
        Paragraph("Rent (₹)", styles["TableHeader"]),
        Paragraph("Food (₹)", styles["TableHeader"]),
        Paragraph("Mess Cut (₹)", styles["TableHeader"]),
        Paragraph("Fine (₹)", styles["TableHeader"]),
        Paragraph("Total (₹)", styles["TableHeader"]),
        Paragraph("Paid (₹)", styles["TableHeader"]),
        Paragraph("Balance (₹)", styles["TableHeader"]),
        Paragraph("Due Date", styles["TableHeader"]),
        Paragraph("Status", styles["TableHeader"]),
    ]

    table_data = [headers]

    for f in fee_list:
        st_badge = styles["BadgeUnpaid"]
        if f.status == "Paid":
            st_badge = styles["BadgePaid"]
        elif f.status in ("Overdue", "Overdue (Fine added)"):
            st_badge = styles["BadgeOverdue"]

        room_str = str(f.student.room) if f.student.room else "—"
        rent_str = f"{f.rent_amount:,.2f}" if f.rent_amount else "—"
        food_str = f"{f.food_amount:,.2f}" if f.food_amount else "—"
        mess_str = f"-{f.mess_cut_amount:,.2f}" if f.mess_cut_amount > 0 else "—"
        fine_str = f"+{f.fine_amount:,.2f}" if f.fine_amount > 0 else ("Waived" if f.fine_waived else "—")
        due_str = f.due_date.strftime("%d %b %Y") if f.due_date else "—"

        row = [
            Paragraph(f.student.roll_no, styles["TableCellBold"]),
            Paragraph(f.student.user.display_name, styles["TableCell"]),
            Paragraph(room_str, styles["TableCellCenter"]),
            Paragraph(f.title[:28], styles["TableCell"]),
            Paragraph(rent_str, styles["TableCellRight"]),
            Paragraph(food_str, styles["TableCellRight"]),
            Paragraph(mess_str, styles["TableCellRight"]),
            Paragraph(fine_str, styles["TableCellRight"]),
            Paragraph(f"<b>{f.amount:,.2f}</b>", styles["TableCellRightBold"]),
            Paragraph(f"{f.paid:,.2f}", styles["TableCellRight"]),
            Paragraph(f"<b>{f.balance:,.2f}</b>", styles["TableCellRightBold"]),
            Paragraph(due_str, styles["TableCellCenter"]),
            Paragraph(f.status, st_badge),
        ]
        table_data.append(row)

    # Totals Row
    table_data.append([
        Paragraph("<b>TOTALS</b>", styles["TableCellBold"]),
        Paragraph(f"<b>{total_count} records</b>", styles["TableCellBold"]),
        Paragraph("", styles["TableCell"]),
        Paragraph("", styles["TableCell"]),
        Paragraph(f"<b>₹{total_rent:,.2f}</b>", styles["TableCellRightBold"]),
        Paragraph(f"<b>₹{total_food:,.2f}</b>", styles["TableCellRightBold"]),
        Paragraph(f"<b>-₹{total_mess_cut:,.2f}</b>", styles["TableCellRightBold"]),
        Paragraph(f"<b>+₹{total_fine:,.2f}</b>", styles["TableCellRightBold"]),
        Paragraph(f"<b>₹{total_billed:,.2f}</b>", styles["TableCellRightBold"]),
        Paragraph(f"<b>₹{total_paid:,.2f}</b>", styles["TableCellRightBold"]),
        Paragraph(f"<b>₹{total_balance:,.2f}</b>", styles["TableCellRightBold"]),
        Paragraph("", styles["TableCell"]),
        Paragraph("", styles["TableCell"]),
    ])

    widths = [55, 95, 42, 105, 50, 52, 54, 50, 62, 52, 58, 55, 40]
    t = Table(table_data, colWidths=widths, repeatRows=1)
    
    t_style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b2545")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("GRID", (0, 0), (-1, -2), 0.5, colors.HexColor("#e2e8f0")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e2e8f0")),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#0b2545")),
    ]
    # Alternating row colors
    for r_idx in range(1, len(table_data) - 1):
        if r_idx % 2 == 0:
            t_style.append(("BACKGROUND", (0, r_idx), (-1, r_idx), colors.HexColor("#f8fafc")))

    t.setStyle(TableStyle(t_style))
    story.append(t)

    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()


def generate_fee_invoice_pdf(fee, current_user=None):
    """
    Generates a professional single-student Fee Invoice / Official Receipt in portrait A4.
    """
    if fee.due_date and fee.due_date < date.today():
        fee.sync_late_fine()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=40,
        rightMargin=40,
        topMargin=36,
        bottomMargin=36,
    )
    styles = get_styles()
    story = []

    # Letterhead & Invoice Header
    title_data = [
        [
            Paragraph("<b>HOSTEL MANAGEMENT SYSTEM</b>", ParagraphStyle("HostelName", fontName="Helvetica-Bold", fontSize=15, textColor=colors.HexColor("#0b2545"))),
            Paragraph(f"<b>INVOICE / RECEIPT</b><br/><font size=8 color='#64748b'>Ref: #{fee.pk:05d}</font>", styles["TableCellRightBold"]),
        ],
        [
            Paragraph("Official Resident Fee Invoice & Statement", ParagraphStyle("Sub", fontName="Helvetica", fontSize=9, textColor=colors.HexColor("#64748b"))),
            Paragraph(f"Issue Date: <b>{date.today().strftime('%d %b %Y')}</b>", styles["TableCellRight"]),
        ],
    ]
    title_table = Table(title_data, colWidths=[330, 185])
    title_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(title_table)
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#0b2545"), spaceAfter=12, spaceBefore=6))

    # Resident Details Box
    s = fee.student
    room_name = str(s.room) if s.room else "Unassigned"
    floor_str = f" ({s.room.floor_name})" if s.room else ""
    parent_str = s.parent.user.display_name if s.parent else "—"
    parent_phone = s.parent.user.phone if (s.parent and s.parent.user.phone) else "—"

    info_data = [
        [
            Paragraph("<b>RESIDENT DETAILS</b>", styles["TableCellBold"]),
            Paragraph("<b>BILLING INFORMATION</b>", styles["TableCellBold"]),
        ],
        [
            Paragraph(f"<b>Name:</b> {s.user.display_name}", styles["TableCell"]),
            Paragraph(f"<b>Fee Title:</b> {fee.title}", styles["TableCell"]),
        ],
        [
            Paragraph(f"<b>Roll No:</b> {s.roll_no}", styles["TableCell"]),
            Paragraph(f"<b>Billing Month:</b> {fee.month or '—'}", styles["TableCell"]),
        ],
        [
            Paragraph(f"<b>Room Assigned:</b> {room_name}{floor_str}", styles["TableCell"]),
            Paragraph(f"<b>Due Date:</b> {fee.due_date.strftime('%d %b %Y') if fee.due_date else '—'}", styles["TableCell"]),
        ],
        [
            Paragraph(f"<b>Course:</b> {s.course or '—'}", styles["TableCell"]),
            Paragraph(f"<b>Payment Status:</b> <b>{fee.status}</b>", styles["TableCellBold"]),
        ],
        [
            Paragraph(f"<b>Parent / Guardian:</b> {parent_str} ({parent_phone})", styles["TableCell"]),
            Paragraph(f"<b>Overdue Days:</b> {fee.overdue_days} days" if fee.overdue_days > 0 else "<b>Overdue:</b> No", styles["TableCell"]),
        ],
    ]
    info_table = Table(info_data, colWidths=[260, 255])
    info_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f1fb")),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 14))

    # Fee Itemized Table
    item_header = [
        Paragraph("Description / Item", styles["TableHeader"]),
        Paragraph("Policy / Days", styles["TableHeader"]),
        Paragraph("Rate / Basis", styles["TableHeader"]),
        Paragraph("Amount (₹)", styles["TableHeader"]),
    ]
    items_data = [item_header]

    if fee.rent_amount > 0:
        items_data.append([
            Paragraph("<b>Fixed Hostel Room Rent</b>", styles["TableCellBold"]),
            Paragraph("Monthly Flat Rate", styles["TableCell"]),
            Paragraph("Fixed Monthly Accommodation", styles["TableCell"]),
            Paragraph(f"₹{fee.rent_amount:,.2f}", styles["TableCellRight"]),
        ])
    if fee.food_amount > 0:
        items_data.append([
            Paragraph("<b>Dining & Mess Food Charges</b>", styles["TableCellBold"]),
            Paragraph("Daily Meal Service", styles["TableCell"]),
            Paragraph("Hostel Mess Dining Rate", styles["TableCell"]),
            Paragraph(f"₹{fee.food_amount:,.2f}", styles["TableCellRight"]),
        ])
    if fee.mess_cut_amount > 0:
        items_data.append([
            Paragraph("<b>Attendance Mess Cut Rebate</b>", ParagraphStyle("MessLbl", parent=styles["TableCellBold"], textColor=colors.HexColor("#15803d"))),
            Paragraph(f"{fee.mess_cut_days} Eligible Day(s)", styles["TableCell"]),
            Paragraph("Absence streak ≥ 3 consecutive days", styles["TableCell"]),
            Paragraph(f"-₹{fee.mess_cut_amount:,.2f}", ParagraphStyle("MessVal", parent=styles["TableCellRightBold"], textColor=colors.HexColor("#15803d"))),
        ])
    if fee.fine_amount > 0:
        items_data.append([
            Paragraph("<b>Overdue Late Payment Fine</b>", ParagraphStyle("FineLbl", parent=styles["TableCellBold"], textColor=colors.HexColor("#b91c1c"))),
            Paragraph(f"{fee.overdue_days} Days Past Due", styles["TableCell"]),
            Paragraph("₹300 fine per 5 days overdue", styles["TableCell"]),
            Paragraph(f"+₹{fee.fine_amount:,.2f}", ParagraphStyle("FineVal", parent=styles["TableCellRightBold"], textColor=colors.HexColor("#b91c1c"))),
        ])
    elif fee.fine_waived:
        items_data.append([
            Paragraph("<b>Overdue Late Payment Fine</b>", styles["TableCellBold"]),
            Paragraph(f"{fee.overdue_days} Days Past Due", styles["TableCell"]),
            Paragraph("Late fine waived by warden office", styles["TableCell"]),
            Paragraph("₹0.00 (Waived)", styles["TableCellRight"]),
        ])

    # If simple custom fee without itemized breakdown:
    if len(items_data) == 1:
        items_data.append([
            Paragraph(f"<b>{fee.title}</b>", styles["TableCellBold"]),
            Paragraph("General Hostel Fee", styles["TableCell"]),
            Paragraph("Direct Charge", styles["TableCell"]),
            Paragraph(f"₹{fee.amount:,.2f}", styles["TableCellRightBold"]),
        ])

    # Subtotal / Net Total
    items_data.append([
        Paragraph("<b>TOTAL NET PAYABLE AMOUNT</b>", styles["TableCellBold"]),
        Paragraph("", styles["TableCell"]),
        Paragraph("", styles["TableCell"]),
        Paragraph(f"<b>₹{fee.amount:,.2f}</b>", styles["TableCellRightBold"]),
    ])

    items_table = Table(items_data, colWidths=[215, 100, 110, 90])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b2545")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -2), 0.5, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e2e8f0")),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#0b2545")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 14))

    # Payment History Section
    payments = list(fee.payments.all())
    p_header = [
        Paragraph("Payment Date", styles["TableHeader"]),
        Paragraph("Method", styles["TableHeader"]),
        Paragraph("Transaction / Reference", styles["TableHeader"]),
        Paragraph("Recorded By", styles["TableHeader"]),
        Paragraph("Amount Paid (₹)", styles["TableHeader"]),
    ]
    p_data = [p_header]

    for p in payments:
        p_data.append([
            Paragraph(p.paid_on.strftime("%d %b %Y"), styles["TableCellCenter"]),
            Paragraph(p.method, styles["TableCell"]),
            Paragraph(p.reference or "—", styles["TableCell"]),
            Paragraph(p.recorded_by.display_name if p.recorded_by else "Staff", styles["TableCell"]),
            Paragraph(f"₹{p.amount:,.2f}", styles["TableCellRightBold"]),
        ])

    if not payments:
        p_data.append([
            Paragraph("No payments recorded yet for this invoice.", styles["TableCell"]),
            Paragraph("", styles["TableCell"]),
            Paragraph("", styles["TableCell"]),
            Paragraph("", styles["TableCell"]),
            Paragraph("₹0.00", styles["TableCellRight"]),
        ])

    # Balance Summary
    p_data.append([
        Paragraph("<b>Total Amount Paid:</b>", styles["TableCellBold"]),
        Paragraph(f"<b>₹{fee.paid:,.2f}</b>", ParagraphStyle("PVal", parent=styles["TableCellBold"], textColor=colors.HexColor("#16a34a"))),
        Paragraph("<b>Current Balance Due:</b>", styles["TableCellBold"]),
        Paragraph(f"<b>₹{fee.balance:,.2f}</b>", ParagraphStyle("BVal", parent=styles["TableCellBold"], textColor=colors.HexColor("#dc2626") if fee.balance > 0 else colors.HexColor("#16a34a"))),
        Paragraph(f"<b>{fee.status}</b>", styles["TableCellCenter"]),
    ])

    p_table = Table(p_data, colWidths=[90, 85, 140, 110, 90])
    p_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -2), 0.5, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f1f5f9")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(p_table)
    story.append(Spacer(1, 24))

    # Notes & Signature Block
    notes_str = fee.notes or "Please present this invoice at the hostel warden's office when making payment."
    auth_data = [
        [
            Paragraph(f"<b>Notes & Remarks:</b><br/>{notes_str}", styles["TableCell"]),
            Paragraph("<b>Authorized Signature & Stamp</b><br/><br/><br/>____________________________<br/>Hostel Warden / Accounts", styles["TableCellCenter"]),
        ]
    ]
    auth_table = Table(auth_data, colWidths=[315, 200])
    auth_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (0, 0), 0.5, colors.HexColor("#e2e8f0")),
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#f8fafc")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(KeepTogether(auth_table))

    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()
