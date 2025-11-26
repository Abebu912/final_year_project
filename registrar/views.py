from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.utils import timezone
from django.http import HttpResponse
from users.decorators import registrar_required
from users.models import User
from subjects.models import Subject, Enrollment
from ranks.models import Grade
from django.db.utils import OperationalError
import csv
import io
# ReportLab is an optional dependency used to generate PDF transcripts.
# Import it lazily; if it's missing, set a flag so the rest of the app
# (and migrations) can continue to run without ReportLab installed.
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

@registrar_required
def registrar_dashboard(request):
    pending_enrollments = Enrollment.objects.filter(status='pending').count()
    total_students = User.objects.filter(role='student', is_approved=True).count()
    total_subjects = Subject.objects.filter(is_active=True).count()
    
    context = {
        'pending_enrollments': pending_enrollments,
        'total_students': total_students,
        'total_subjects': total_subjects,
    }
    return render(request, 'registrar/registrar_dashboard.html', context)

@registrar_required
def approve_registrations(request):
    pending_enrollments = list(Enrollment.objects.filter(status='pending').select_related('student', 'subject'))
    waitlisted_enrollments = list(Enrollment.objects.filter(status='waitlisted').select_related('student', 'subject'))

    # Precompute current enrollment counts for template use (templates can't call methods with args)
    for enrollment in pending_enrollments + waitlisted_enrollments:
        try:
            enrollment.current_count = enrollment.subject.current_enrollment_count(enrollment.academic_year, enrollment.semester)
        except Exception:
            enrollment.current_count = 0
    
    if request.method == 'POST':
        enrollment_id = request.POST.get('enrollment_id')
        action = request.POST.get('action')
        enrollment = get_object_or_404(Enrollment, id=enrollment_id)
        
        if action == 'approve':
            # Check if subject has capacity
            subject = enrollment.subject
            current_count = subject.current_enrollment_count(enrollment.academic_year, enrollment.semester)
            if current_count < subject.max_capacity:
                enrollment.status = 'approved'
                messages.success(request, f'Enrollment approved for {enrollment.student.username}.')
            else:
                enrollment.status = 'waitlisted'
                messages.warning(request, 'Subject is full. Student added to waitlist.')
        elif action == 'reject':
            enrollment.status = 'rejected'
            messages.success(request, f'Enrollment rejected for {enrollment.student.username}.')
        elif action == 'approve_waitlist':
            # Manually approve waitlisted student
            enrollment.status = 'approved'
            # nothing to add to course/student lists here; enrollment.save will persist the status
            messages.success(request, f'Waitlisted enrollment approved for {enrollment.student.username}.')
        
        enrollment.save()
        return redirect('approve_registrations')
    
    context = {
        'pending_enrollments': pending_enrollments,
        'waitlisted_enrollments': waitlisted_enrollments,
    }
    return render(request, 'registrar/approve_registrations.html', context)

@registrar_required
def manage_academic_records(request):
    students = User.objects.filter(role='student', is_approved=True).select_related('studentprofile')
    selected_student_id = request.GET.get('student_id')
    
    if selected_student_id:
        student = get_object_or_404(User, id=selected_student_id, role='student')
        enrollments = Enrollment.objects.filter(student=student).select_related('subject')
        try:
            grades = Grade.objects.filter(student=student).select_related('subject')
        except OperationalError:
            from django.contrib import messages
            messages.error(request, 'Database tables for the `ranks` app are missing. Please run `python manage.py migrate`.')
            grades = []
    else:
        student = None
        enrollments = []
        grades = []
    
    context = {
        'students': students,
        'selected_student': student,
        'enrollments': enrollments,
        'grades': grades,
    }
    return render(request, 'registrar/academic_records.html', context)

@registrar_required
def handle_waitlist(request, subject_id):
    subject = get_object_or_404(Subject, id=subject_id)
    waitlisted_enrollments = Enrollment.objects.filter(subject=subject, status='waitlisted')
    
    if request.method == 'POST':
        enrollment_id = request.POST.get('enrollment_id')
        action = request.POST.get('action')
        
        if action == 'approve':
            enrollment = get_object_or_404(Enrollment, id=enrollment_id)
            current_count = subject.current_enrollment_count(enrollment.academic_year, enrollment.semester)
            if current_count < subject.max_capacity:
                enrollment.status = 'approved'
                enrollment.save()
                messages.success(request, 'Student approved from waitlist.')
            else:
                messages.error(request, 'Subject is at full capacity.')
        
        return redirect('handle_waitlist', subject_id=subject_id)
    
    context = {
        'subject': subject,
        'waitlisted_enrollments': waitlisted_enrollments,
    }
    return render(request, 'registrar/handle_waitlist.html', context)

@registrar_required
def generate_transcripts(request):
    students = User.objects.filter(role='student', is_approved=True)
    selected_student_id = request.GET.get('student_id')
    
    student = None
    grades = []
    
    if selected_student_id:
        student = get_object_or_404(User, id=selected_student_id, role='student')
        try:
            grades = Grade.objects.filter(student=student).select_related('subject')
        except OperationalError:
            from django.contrib import messages
            messages.error(request, 'Database tables for the `ranks` app are missing. Please run `python manage.py migrate`.')
            grades = []
        
        if request.GET.get('format') == 'pdf':
            return generate_pdf_transcript(student, grades)
        elif request.GET.get('format') == 'csv':
            return generate_csv_transcript(student, grades)
    
    context = {
        'students': students,
        'selected_student': student,
        'grades': grades,
    }
    return render(request, 'registrar/generate_transcripts.html', context)

def generate_csv_transcript(student, grades):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{student.username}_transcript.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['Student ID', 'Name', 'Course Code', 'Course Title', 'Grade', 'Credits'])
    
    for grade in grades:
        writer.writerow([
            student.studentprofile.student_id,
            student.get_full_name(),
            grade.subject.code,
            grade.subject.title,
            grade.grade or 'N/A',
            grade.credits
        ])
    
    return response

def generate_pdf_transcript(student, grades):
    """Generate PDF transcript using ReportLab"""
    if not REPORTLAB_AVAILABLE:
        return HttpResponse("PDF generation requires ReportLab. Install with `pip install reportlab`.", status=503)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    
    # Title
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        alignment=1,  # Center alignment
        spaceAfter=30
    )
    story.append(Paragraph("ACADEMIC TRANSCRIPT", title_style))
    
    # Student Information
    student_info = f"""
    <b>Student Name:</b> {student.get_full_name() or student.username}<br/>
    <b>Student ID:</b> {getattr(student.studentprofile, 'student_id', 'N/A')}<br/>
    <b>Program:</b> {getattr(student.studentprofile, 'program', 'N/A')}<br/>
    <b>Generated Date:</b> {timezone.now().strftime('%Y-%m-%d')}
    """
    story.append(Paragraph(student_info, styles["Normal"]))
    story.append(Spacer(1, 20))
    
    # Grades Table
    if grades:
        data = [['Course Code', 'Course Title', 'Credits', 'Grade']]
        total_credits = 0
        total_points = 0
        
        for grade in grades:
            grade_point = get_grade_point(grade.grade)
            credits = grade.credits
            data.append([
                grade.subject.code,
                grade.subject.title,
                str(credits),
                grade.grade or 'In Progress'
            ])
            if grade.grade and credits:
                total_credits += credits
                total_points += grade_point * credits
        
        # Create table
        table = Table(data)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        story.append(table)
        story.append(Spacer(1, 20))
        
        # GPA Calculation
        gpa = total_points / total_credits if total_credits > 0 else 0
        gpa_info = f"<b>Cumulative GPA:</b> {gpa:.2f} | <b>Total Credits:</b> {total_credits}"
        story.append(Paragraph(gpa_info, styles["Normal"]))
    else:
        story.append(Paragraph("No grades available.", styles["Normal"]))
    
    # Build PDF
    doc.build(story)
    buffer.seek(0)
    
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{student.username}_transcript.pdf"'
    return response

def get_grade_point(grade):
    """Convert letter grade to grade point"""
    if not grade:
        return 0.0
        
    grade_points = {
        'A': 4.0, 'A-': 3.7,
        'B+': 3.3, 'B': 3.0, 'B-': 2.7,
        'C+': 2.3, 'C': 2.0, 'C-': 1.7,
        'D+': 1.3, 'D': 1.0, 'D-': 0.7,
        'F': 0.0
    }
    return grade_points.get(grade.upper(), 0.0)