from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Q, Count
from django.db.models import Avg
from users.decorators import teacher_required
from users.models import User
from subjects.models import Subject, Enrollment
from ranks.models import Grade, rank_students_for_subject
from ranks.forms import GradeForm
from django.http import JsonResponse
from django.db.utils import OperationalError
from django.views.decorators.http import require_POST
from .forms import BulkAssignForm
from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from notifications.models import Notification


def get_teacher_profile(user):
    """Ensure a Teacher profile exists for the given User. Returns Teacher instance or None."""
    try:
        from subjects.models import Teacher as TeacherModel
        teacher_obj, created = TeacherModel.objects.get_or_create(
            user=user,
            defaults={
                'teacher_id': f'T{user.id}',
                'department': '',
                'phone_number': '',
                'office_location': ''
            }
        )
        return teacher_obj
    except Exception:
        return None


def _get_current_academic_year():
    import datetime
    now = datetime.datetime.now()
    if now.month >= 8:
        return f"{now.year}-{now.year + 1}"
    else:
        return f"{now.year - 1}-{now.year}"


def _get_current_semester():
    import datetime
    now = datetime.datetime.now()
    if 1 <= now.month <= 5:
        return 'second'
    elif 6 <= now.month <= 7:
        return 'summer'
    else:
        return 'first'


def enroll_students_for_subject(subject, academic_year=None, semester=None, status='approved'):
    """Enroll all students in the system matching subject.grade_level into this subject
    for the provided academic_year and semester if they are not already enrolled."""
    if academic_year is None:
        academic_year = _get_current_academic_year()
    if semester is None:
        semester = _get_current_semester()

    # find students with matching grade level
    students = User.objects.filter(role='student', studentprofile__grade_level=subject.grade_level)
    enrolled = 0
    for student in students:
        try:
            existing = Enrollment.objects.filter(student=student, subject=subject, academic_year=academic_year, semester=semester).first()
            if existing:
                # If there's an existing enrollment (e.g., pending), promote it to the desired status
                if existing.status != status:
                    existing.status = status
                    existing.is_auto_assigned = True
                    try:
                        existing.save()
                    except Exception:
                        pass
                    enrolled += 1
                continue

            Enrollment.objects.create(
                student=student,
                subject=subject,
                academic_year=academic_year,
                semester=semester,
                status=status,
                is_auto_assigned=True
            )
            enrolled += 1
        except Exception:
            continue
    return enrolled

@teacher_required
def teacher_dashboard(request):
    # annotate each subject with the number of approved students
    # count students with status 'approved' or 'active'
    teacher_subjects = Subject.objects.filter(instructor__user=request.user, is_active=True).annotate(
        student_count=Count('enrollments', filter=Q(enrollments__status__in=['approved', 'active']))
    )

    total_students = Enrollment.objects.filter(
        subject__instructor__user=request.user,
        status__in=['approved', 'active']
    ).count()

    # pending scores across all subjects for this teacher
    try:
        pending = Grade.objects.filter(
            subject__instructor__user=request.user,
            score__isnull=True
        ).count()
    except OperationalError:
        pending = 0

    # per-subject pending counts (dictionary subject_id -> pending_count)
    per_subject_pending = {}
    try:
        pending_qs = Grade.objects.filter(subject__instructor__user=request.user, score__isnull=True).values('subject').annotate(count=Count('id'))
        per_subject_pending = {p['subject']: p['count'] for p in pending_qs}
    except OperationalError:
        per_subject_pending = {}

    # attach pending_count to each subject for easy template access
    for s in teacher_subjects:
        s.pending_count = per_subject_pending.get(s.id, 0)

    # subjects that are currently unassigned (available for claiming)
    # allow optional filtering by grade_level, academic_year and semester via GET params
    grade_level_filter = request.GET.get('grade_level')
    academic_year_filter = request.GET.get('academic_year')
    semester_filter = request.GET.get('semester')

    available_qs = Subject.objects.filter(instructor__isnull=True, is_active=True)
    if grade_level_filter:
        try:
            available_qs = available_qs.filter(grade_level=int(grade_level_filter))
        except Exception:
            pass
    # academic year and semester may be represented on the Subject model differently in some projects
    # attempt to filter if those fields exist
    if academic_year_filter and hasattr(Subject, 'academic_year'):
        available_qs = available_qs.filter(academic_year=academic_year_filter)
    if semester_filter and hasattr(Subject, 'semester'):
        available_qs = available_qs.filter(semester__iexact=semester_filter)

    available_subjects = available_qs.annotate(
        student_count=Count('enrollments', filter=Q(enrollments__status='approved'))
    )

    # distinct students across this teacher's subjects
    enrolled_qs = Enrollment.objects.filter(subject__in=teacher_subjects, status__in=['approved', 'active']).select_related('student')
    distinct_students = {}
    for en in enrolled_qs:
        distinct_students[en.student.id] = en.student

    # compute per-student average across this teacher's subjects
    student_averages = {}
    for student_id, student in distinct_students.items():
        avg = Grade.objects.filter(student__id=student_id, subject__in=teacher_subjects).aggregate(avg=Avg('score'))['avg']
        student_averages[student_id] = {'student': student, 'average': round(avg, 2) if avg is not None else None}

    # compute ranking (higher average -> better rank). Students with None average go last.
    ranked = sorted([{'id': sid, 'avg': data['average'], 'student': data['student']} for sid, data in student_averages.items()], key=lambda x: (-(x['avg'] or -1), x['student'].get_full_name()))
    # assign ranks (ties receive same rank)
    last_avg = None
    rank = 0
    ties = 0
    for i, item in enumerate(ranked, start=1):
        if item['avg'] is None:
            item['rank'] = None
            continue
        if item['avg'] != last_avg:
            rank = i
            last_avg = item['avg']
        item['rank'] = rank

    # build per-subject averages
    subject_averages = {}
    for s in teacher_subjects:
        avg = Grade.objects.filter(subject=s).aggregate(avg=Avg('score'))['avg']
        subject_averages[s.id] = round(avg, 2) if avg is not None else None
    # Get unread notifications for the teacher
    unread_notifications = Notification.objects.filter(user=request.user, is_read=False).order_by('-created_at')[:5]
    unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
    
    context = {
        'teacher_subjects': teacher_subjects,
        'total_students': total_students,
        'pending_scores': pending,
        'available_subjects': available_subjects,
        'filter_grade_level': grade_level_filter,
        'filter_academic_year': academic_year_filter,
        'filter_semester': semester_filter,
        'distinct_student_count': len(distinct_students),
        'student_averages': student_averages,
        'student_ranking': ranked,
        'subject_averages': subject_averages,
        'unread_notifications': unread_notifications,
        'unread_count': unread_count,
    }
    return render(request, 'teachers/teacher_dashboard.html', context)

@teacher_required
def claim_subject(request, subject_id):
    """Allow a teacher to claim/assign themselves as the instructor for a subject."""
    subject = get_object_or_404(Subject, id=subject_id)
    try:
        teacher_obj = get_teacher_profile(request.user)
    except Exception:
        messages.error(request, 'No teacher profile found for your account. Please contact admin.')
        return redirect('teacher_dashboard')

    # Assign and save
    subject.instructor = teacher_obj
    subject.save()
    # auto-enroll students of the subject grade for current term
    try:
        enrolled = enroll_students_for_subject(subject)
        if enrolled:
            messages.info(request, f'Auto-enrolled {enrolled} students for {subject.code}.')
    except Exception:
        pass
    messages.success(request, f'Subject {subject.code} assigned to you.')
    return redirect('teacher_dashboard')


@teacher_required
@require_POST
def assign_subjects_for_term(request):
    """Bulk assign subjects to the logged-in teacher for a given grade/term.

    POST params: grade_level, academic_year, semester
    Only assigns subjects that are active and currently have no instructor.
    """
    form = BulkAssignForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Invalid input for bulk assignment.')
        return redirect('teacher_dashboard')

    grade_level = form.cleaned_data['grade_level']
    academic_year = form.cleaned_data['academic_year']
    semester = form.cleaned_data['semester']

    teacher_obj = get_teacher_profile(request.user)
    if not teacher_obj:
        messages.error(request, 'No teacher profile found for your account. Please contact admin.')
        return redirect('teacher_dashboard')

    # Find subjects for this grade level and term (term info is not on Subject model,
    # so we filter by grade_level and is_active and only assign if instructor is None)
    subjects_qs = Subject.objects.filter(grade_level=grade_level, is_active=True, instructor__isnull=True)
    assigned = 0
    for subj in subjects_qs:
        # if academic_year/semester provided, only assign if subject has available slots
        try:
            if academic_year and semester:
                if not subj.is_available(academic_year, semester):
                    continue
        except Exception:
            # if subject doesn't support availability checks, continue assigning
            pass

        subj.instructor = teacher_obj
        subj.save()
        try:
            # enroll students for provided term
            enrolled = enroll_students_for_subject(subj, academic_year=academic_year, semester=semester)
            if enrolled:
                messages.info(request, f'Auto-enrolled {enrolled} students for {subj.code}.')
        except Exception:
            pass
        assigned += 1

    messages.success(request, f'Assigned {assigned} subjects for Grade {grade_level} ({academic_year} - {semester}).')
    return redirect('teacher_dashboard')


@teacher_required
@require_POST
def assign_selected_subjects(request):
    """Assign the list of subject IDs posted by the teacher to themselves.

    POST param: subject_ids (one or more)
    """
    subject_ids = request.POST.getlist('subject_ids')
    if not subject_ids:
        messages.error(request, 'No subjects selected to assign.')
        return redirect('teacher_dashboard')

    teacher_obj = get_teacher_profile(request.user)
    if not teacher_obj:
        messages.error(request, 'No teacher profile found for your account. Please contact admin.')
        return redirect('teacher_dashboard')

    # allow optional academic_year/semester in POST to control enrollments
    academic_year = request.POST.get('academic_year') or None
    semester = request.POST.get('semester') or None

    assigned = 0
    for sid in subject_ids:
        try:
            subj = Subject.objects.get(id=int(sid), is_active=True)
        except Exception:
            continue
        # only assign if currently unassigned
        if subj.instructor is not None:
            continue
        subj.instructor = teacher_obj
        subj.save()
        try:
            enrolled = enroll_students_for_subject(subj, academic_year=academic_year, semester=semester)
            if enrolled:
                messages.info(request, f'Auto-enrolled {enrolled} students for {subj.code}.')
        except Exception:
            pass
        assigned += 1

    messages.success(request, f'Assigned {assigned} selected subjects to you.')
    return redirect('teacher_dashboard')

@teacher_required
def enter_grades(request):
    teacher_subjects = Subject.objects.filter(instructor__user=request.user, is_active=True)
    selected_subject_id = request.GET.get('subject_id')
    
    if selected_subject_id:
        subject = get_object_or_404(Subject, id=selected_subject_id)
        # Only allow entering grades if the logged-in teacher is the instructor
        if subject.instructor and subject.instructor.user != request.user:
            messages.error(request, 'You are not the instructor for the selected subject.')
            return redirect('teacher_dashboard')
        # include both approved and active enrollments so assigned students appear
        enrollments = Enrollment.objects.filter(subject=subject, status__in=['approved', 'active']).select_related('student')
        
        # load existing grades for this subject to prefill the form
        existing_grades_qs = Grade.objects.filter(subject=subject).select_related('student')
        existing_grades = {g.student.id: g.score for g in existing_grades_qs}
        # attach current_score to enrollment objects for easy template access
        for en in enrollments:
            en.current_score = existing_grades.get(en.student.id)

        if request.method == 'POST':
            updated = 0
            for enrollment in enrollments:
                grade_value = request.POST.get(f'grade_{enrollment.student.id}')
                if grade_value is None or grade_value == '':
                    continue
                try:
                    score = int(float(grade_value))
                except ValueError:
                    continue
                # Clamp score to 0-100
                if score < 0:
                    score = 0
                if score > 100:
                    score = 100

                try:
                    grade_obj, created = Grade.objects.get_or_create(
                        student=enrollment.student,
                        subject=subject,
                        defaults={'score': score}
                    )
                except OperationalError:
                    messages.error(request, 'Database tables for the `ranks` app are missing. Please run `python manage.py migrate`.')
                    return redirect(request.path + f'?subject_id={subject.id}')

                if not created:
                    grade_obj.score = score
                    grade_obj.save()
                # store remarks/result if provided
                result_val = request.POST.get(f'result_{enrollment.student.id}')
                if result_val is not None:
                    # save into Grade.remarks and Enrollment.result when available
                    try:
                        grade_obj.remarks = result_val
                        grade_obj.save()
                    except Exception:
                        pass
                    try:
                        enrollment.result = result_val
                        enrollment.save()
                    except Exception:
                        pass
                updated += 1

            messages.success(request, f'Scores updated for {updated} students.')
            return redirect(request.path + f'?subject_id={subject.id}')
    else:
        subject = None
        enrollments = []
    
    context = {
        'teacher_subjects': teacher_subjects,
        'selected_subject': subject if selected_subject_id else None,
        'enrollments': enrollments,
        'student_scores': existing_grades if selected_subject_id else {},
    }
    return render(request, 'teachers/enter_grades.html', context)

@teacher_required
def class_rosters(request):
    teacher_subjects = Subject.objects.filter(instructor__user=request.user, is_active=True)
    selected_subject_id = request.GET.get('subject_id')
    
    if selected_subject_id:
        subject = get_object_or_404(Subject, id=selected_subject_id)
        # allow viewing rosters even for unassigned subjects; but if assigned to another teacher, restrict
        if subject.instructor and subject.instructor.user != request.user:
            messages.error(request, 'You are not the instructor for the selected subject.')
            return redirect('teacher_dashboard')
        enrollments = Enrollment.objects.filter(subject=subject).select_related('student')
    else:
        subject = None
        enrollments = []
    
    context = {
        'teacher_subjects': teacher_subjects,
        'selected_subject': subject,
        'enrollments': enrollments,
    }
    return render(request, 'teachers/class_rosters.html', context)

@teacher_required
def performance_reports(request):
    teacher_subjects = Subject.objects.filter(instructor__user=request.user, is_active=True)
    selected_subject_id = request.GET.get('subject_id')
    
    if selected_subject_id:
        subject = get_object_or_404(Subject, id=selected_subject_id)
        if subject.instructor and subject.instructor.user != request.user:
            messages.error(request, 'You are not the instructor for the selected subject.')
            return redirect('teacher_dashboard')
        try:
            grades = Grade.objects.filter(subject=subject).select_related('student')
        except OperationalError:
            messages.error(request, 'Database tables for the `ranks` app are missing. Please run `python manage.py migrate`.')
            grades = []

        # Calculate numeric statistics (average score out of 100) and ranking
        if grades:
            numeric_scores = [g.score for g in grades if g.score is not None]
            avg_score = sum(numeric_scores) / len(numeric_scores) if numeric_scores else 0
            # build simple distribution buckets
            score_distribution = {
                '90-100': len([s for s in numeric_scores if s >= 90]),
                '80-89': len([s for s in numeric_scores if 80 <= s < 90]),
                '70-79': len([s for s in numeric_scores if 70 <= s < 80]),
                '60-69': len([s for s in numeric_scores if 60 <= s < 70]),
                '0-59': len([s for s in numeric_scores if s < 60]),
            }
            # compute ranks using helper
            subject_ranking = rank_students_for_subject(subject)
        else:
            avg_score = 0
            score_distribution = {}
            subject_ranking = []
    else:
        subject = None
        grades = []
        avg_score = 0
        score_distribution = {}
        subject_ranking = []
    
    context = {
        'teacher_subjects': teacher_subjects,
        'selected_subject': subject,
        'grades': grades,
        'avg_score': round(avg_score, 2),
        'score_distribution': score_distribution,
        'subject_ranking': subject_ranking,
    }
    return render(request, 'teachers/performance_reports.html', context)

@teacher_required
def update_student_grade(request, enrollment_id):
    enrollment = get_object_or_404(Enrollment, id=enrollment_id, subject__instructor__user=request.user)
    
    if request.method == 'POST':
        form = GradeForm(request.POST)
        if form.is_valid():
            try:
                grade, created = Grade.objects.get_or_create(
                    student=enrollment.student,
                    subject=enrollment.subject,
                    defaults={'score': form.cleaned_data.get('score')}
                )
            except OperationalError:
                messages.error(request, 'Database tables for the `ranks` app are missing. Please run `python manage.py migrate`.')
                return redirect('enter_grades')
            if not created:
                for attr, value in form.cleaned_data.items():
                    setattr(grade, attr, value)
                grade.save()
            
            messages.success(request, 'Score updated successfully!')
            return redirect('enter_grades')
    else:
        try:
            try:
                grade = Grade.objects.get(student=enrollment.student, subject=enrollment.subject)
                form = GradeForm(instance=grade)
            except Grade.DoesNotExist:
                form = GradeForm()
        except OperationalError:
            messages.error(request, 'Database tables for the `ranks` app are missing. Please run `python manage.py migrate`.')
            form = GradeForm()
    
    context = {
        'form': form,
        'enrollment': enrollment,
    }
    return render(request, 'teachers/update_grade.html', context)

@teacher_required
def view_student_performance(request, student_id):
    """View individual student performance across all teacher's courses"""
    student = get_object_or_404(User, id=student_id, role='student')
    
    # Get subjects taught by this teacher that the student is enrolled in
    student_courses = Subject.objects.filter(
        instructor__user=request.user,
        enrollments__student=student,
        enrollments__status='approved'
    ).distinct()
    
    # Get grades for these courses
    try:
        student_grades = Grade.objects.filter(
            student=student,
            subject__in=student_courses
        ).select_related('subject')
    except OperationalError:
        messages.error(request, 'Database tables for the `ranks` app are missing. Please run `python manage.py migrate`.')
        student_grades = []
    
    context = {
        'student': student,
        'student_subjects': student_courses,
        'student_grades': student_grades,
    }
    return render(request, 'teachers/student_performance.html', context)

@teacher_required
def bulk_grade_upload(request, subject_id):
    """Handle bulk score upload via CSV"""
    subject = get_object_or_404(Subject, id=subject_id, instructor__user=request.user)
    
    if request.method == 'POST' and request.FILES.get('grade_file'):
        grade_file = request.FILES['grade_file']
        # Simple CSV processing (you might want to use csv module for more complex files)
        try:
            # This is a simplified example - implement proper CSV parsing
            lines = grade_file.read().decode('utf-8').split('\n')
            processed_count = 0
            
            for line in lines[1:]:  # Skip header
                if line.strip():
                    parts = line.split(',')
                    if len(parts) >= 2:
                        student_id = parts[0].strip()
                        grade_value = parts[1].strip()
                        
                        # Find student and update grade
                        try:
                            student = User.objects.get(
                                studentprofile__student_id=student_id,
                                role='student'
                            )
                            try:
                                grade, created = Grade.objects.get_or_create(
                                    student=student,
                                    subject=subject,
                                    defaults={'score': None}
                                )
                            except OperationalError:
                                continue
                            if not created:
                                # store the submitted letter grade into remarks for trace
                                grade.remarks = f'entered_grade:{grade_value}'
                                grade.save()
                            if not created:
                                grade.grade = grade_value
                                grade.save()
                            processed_count += 1
                        except User.DoesNotExist:
                            continue
            
            messages.success(request, f'Successfully processed grades for {processed_count} students.')
            
        except Exception as e:
            messages.error(request, f'Error processing file: {str(e)}')
        
        return redirect('enter_grades')
    
    context = {
        'subject': subject,
    }
    return render(request, 'teachers/bulk_grade_upload.html', context)

@teacher_required
def get_subject_statistics(request, subject_id):
    """API endpoint for subject statistics (for charts)"""
    subject = get_object_or_404(Subject, id=subject_id, instructor__user=request.user)
    try:
        grades = Grade.objects.filter(subject=subject)
    except OperationalError:
        messages.error(request, 'Database tables for the `ranks` app are missing. Please run `python manage.py migrate`.')
        grades = []
    
    # Numeric distribution buckets and average score
    numeric_scores = [g.score for g in grades if g.score is not None]
    distribution = {
        '90-100': len([s for s in numeric_scores if s >= 90]),
        '80-89': len([s for s in numeric_scores if 80 <= s < 90]),
        '70-79': len([s for s in numeric_scores if 70 <= s < 80]),
        '60-69': len([s for s in numeric_scores if 60 <= s < 70]),
        '0-59': len([s for s in numeric_scores if s < 60]),
    }
    avg_score = sum(numeric_scores) / len(numeric_scores) if numeric_scores else 0

    data = {
        'score_distribution': distribution,
        'average_score': round(avg_score, 2),
        'total_students': grades.count(),
        'scored_students': len(numeric_scores),
    }
    
    return JsonResponse(data)


@teacher_required
@require_POST
def save_student_score(request):
    """AJAX endpoint to save one student's score and optional result for a subject.

    POST params: student_id, subject_id, score, result (optional)
    Returns JSON {success: true, message: '', avg: <new subject average>} on success.
    """
    student_id = request.POST.get('student_id')
    subject_id = request.POST.get('subject_id')
    score = request.POST.get('score')
    result = request.POST.get('result')

    if not (student_id and subject_id):
        return JsonResponse({'success': False, 'message': 'Missing student_id or subject_id'}, status=400)

    try:
        subject = Subject.objects.get(id=subject_id, instructor__user=request.user)
    except Subject.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Subject not found or you are not the instructor'}, status=403)

    try:
        student = User.objects.get(id=student_id, role='student')
    except User.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Student not found'}, status=404)

    try:
        enrollment = Enrollment.objects.get(student=student, subject=subject, status__in=['approved', 'active'])
    except Enrollment.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Student is not enrolled in this subject'}, status=400)

    # validate and store score
    parsed_score = None
    if score is not None and score != '':
        try:
            parsed_score = int(float(score))
        except ValueError:
            return JsonResponse({'success': False, 'message': 'Invalid score'}, status=400)
        if parsed_score < 0:
            parsed_score = 0
        if parsed_score > 100:
            parsed_score = 100

    # Create or update Grade (ranks.models.Grade)
    try:
        grade_obj, created = Grade.objects.get_or_create(student=student, subject=subject, defaults={'score': parsed_score})
    except Exception:
        return JsonResponse({'success': False, 'message': 'Database error creating grade'}, status=500)

    if not created:
        grade_obj.score = parsed_score
    if result is not None:
        grade_obj.remarks = result
    grade_obj.save()

    # Save enrollment-level result
    if result is not None:
        try:
            enrollment.result = result
            enrollment.save()
        except Exception:
            pass

    # compute updated subject average
    avg = Grade.objects.filter(subject=subject, score__isnull=False).aggregate(avg=Avg('score'))['avg']
    avg = round(avg, 2) if avg is not None else None

    return JsonResponse({'success': True, 'message': 'Saved', 'avg': avg})

def get_grade_point(grade_letter):
    """Convert letter grade to grade point - helper function"""
    if not grade_letter:
        return 0.0
        
    grade_points = {
        'A': 4.0, 'A-': 3.7,
        'B+': 3.3, 'B': 3.0, 'B-': 2.7,
        'C+': 2.3, 'C': 2.0, 'C-': 1.7,
        'D+': 1.3, 'D': 1.0, 'D-': 0.7,
        'F': 0.0
    }
    return grade_points.get(grade_letter.upper(), 0.0)