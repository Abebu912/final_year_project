from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Q, Count
from django.utils import timezone
from django.http import JsonResponse
from users.decorators import student_required
from users.models import User
from subjects.models import Subject, Enrollment
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from ranks.models import Grade
from payments.models import Payment, FeeStructure
from notifications.models import Announcement
from ai_advisor.models import AIConversation, AIMessage
import json
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import Student
from .serializers import StudentSerializer
from subjects.models import Subject
from subjects.serializers import SubjectSerializer

from types import SimpleNamespace
from django.conf import settings


def get_assigned_subjects_for_student(user, grade_level_override=None):
    """Return a list of pseudo-enrollment objects for core subjects matching student's grade.

    These objects mimic Enrollment with attributes used in templates: subject, status, final_grade, enrolled_date
    They are not saved to the DB; they are used to display assigned subjects when no enrollments exist yet.
    """
    if grade_level_override is not None:
        grade_level = grade_level_override
    else:
        try:
            grade_level = user.studentprofile.grade_level
        except Exception:
            try:
                grade_level = user.student_profile.grade_level
            except Exception:
                return []

    subjects = Subject.objects.filter(
        grade_level=grade_level,
        is_active=True,
        subject_type__in=getattr(settings, 'AUTO_ENROLL_SUBJECT_TYPES', ['core'])
    ).order_by('code')

    pseudo = []
    for s in subjects:
        obj = SimpleNamespace()
        obj.subject = s
        obj.status = 'assigned'
        obj.final_grade = None
        obj.enrolled_date = None
        pseudo.append(obj)
    return pseudo

@student_required
def student_dashboard(request):
    # Include approved/active/pending/waitlisted enrollments so dashboard shows current registrations
    student_enrollments = Enrollment.objects.filter(
        student=request.user,
        status__in=['approved', 'active', 'pending', 'waitlisted']
    ).select_related('subject')
    
    recent_grades = Grade.objects.filter(student=request.user).select_related('subject')[:5]
    pending_payments = Payment.objects.filter(student=request.user, status='pending')
    
    context = {
        'enrollments': student_enrollments,
        'recent_grades': recent_grades,
        'pending_payments': pending_payments,
        'total_subjects': student_enrollments.count(),
    }
    # Build enrolled_courses list for dashboard template from enrollments
    enrolled_courses = []
    for enr in student_enrollments:
        subj = enr.subject
        enrolled_courses.append({
            'name': subj.name,
            'code': subj.code,
            'instructor': subj.instructor.user.get_full_name() if getattr(subj, 'instructor', None) else 'TBA',
            'schedule': subj.schedule_display,
            'is_auto_assigned': getattr(enr, 'is_auto_assigned', False),
            'status': getattr(enr, 'status', None),
            'enrollment': enr,
        })
    context['enrolled_courses'] = enrolled_courses
    # If student has no enrollments, provide assigned/core subjects for their grade
    if student_enrollments.count() == 0:
        assigned = get_assigned_subjects_for_student(request.user)
        if assigned:
            # Provide subjects list expected by template as `enrolled_courses`
            context['enrollments'] = assigned
            context['assigned_only'] = True
            context['total_subjects'] = len(assigned)
            # Map assigned pseudo-enrollments to subject-like dicts for the dashboard
            enrolled_courses = []
            for a in assigned:
                subj = a.subject if hasattr(a, 'subject') else a
                enrolled_courses.append({
                    'name': subj.name,
                    'code': subj.code,
                    'instructor': getattr(subj, 'instructor', None).user.get_full_name() if getattr(subj, 'instructor', None) else 'TBA',
                    'schedule': subj.schedule_display,
                    'is_auto_assigned': True,
                })
            context['enrolled_courses'] = enrolled_courses
    return render(request, 'students/student_dashboard.html', context)

@student_required
def subject_registration(request):
    # Get enrolled subject IDs to exclude them from available subjects
    enrolled_subject_ids = Enrollment.objects.filter(
        student=request.user
    ).values_list('subject_id', flat=True)
    
    # Get available subjects (not enrolled in) with enrollment count
    available_subjects = Subject.objects.filter(
        is_active=True
    ).exclude(
        id__in=enrolled_subject_ids
    ).annotate(
        current_enrollment=Count('enrollments')
    )
    
    # Determine current academic year / semester / grade from student profile
    student_profile = getattr(request.user, 'studentprofile', None)
    if student_profile:
        current_academic_year = student_profile.academic_year
        current_semester = student_profile.current_semester
        current_grade = student_profile.grade_level
    else:
        # sensible defaults
        current_academic_year = getattr(settings, 'DEFAULT_ACADEMIC_YEAR', '2024-2025')
        current_semester = getattr(settings, 'DEFAULT_SEMESTER', 'first')
        current_grade = 1

    # Allow optional grade selection via GET param to view registration for other grades
    requested_grade = request.GET.get('grade')
    if requested_grade:
        try:
            current_grade = int(requested_grade)
        except ValueError:
            pass

    # Filter available subjects to the selected/current grade
    try:
        available_subjects = available_subjects.filter(grade_level=current_grade)
    except Exception:
        # if current_grade is invalid, fall back to showing all active subjects
        pass

    # Get student's current enrollments
    student_enrollments = Enrollment.objects.filter(
        student=request.user
    ).select_related('subject')
    
    enrolled_subjects = student_enrollments.filter(status__in=['approved', 'pending'])

    # If no enrolled subjects found, provide assigned/core subjects for display
    assigned_only = False
    if not enrolled_subjects.exists():
        assigned = get_assigned_subjects_for_student(request.user, grade_level_override=current_grade)
        if assigned:
            # Optionally persist as Enrollment rows if enabled in settings
            if getattr(settings, 'AUTO_CREATE_ENROLLMENTS', False):
                # Create Enrollment rows (status='pending') for each assigned subject if not exists
                created = 0
                failures = []
                for a in assigned:
                    subj = a.subject
                    try:
                        exists = Enrollment.objects.filter(
                            student=request.user,
                            subject=subj,
                            academic_year=current_academic_year,
                            semester=current_semester
                        ).exists()
                        if not exists:
                            Enrollment.objects.create(
                                student=request.user,
                                subject=subj,
                                academic_year=current_academic_year,
                                semester=current_semester,
                                status='pending',
                                is_auto_assigned=True
                            )
                            created += 1
                    except Exception as e:
                        failures.append((subj, str(e)))

                # Reload enrolled_subjects from DB to show persisted rows
                student_enrollments = Enrollment.objects.filter(student=request.user).select_related('subject')
                enrolled_subjects = student_enrollments.filter(status__in=['approved', 'pending'])
                assigned_only = True
            else:
                enrolled_subjects = assigned
                assigned_only = True
    
    if request.method == 'POST':
        subject_ids = request.POST.getlist('subject_ids')
        
        if subject_ids:
            success_count = 0
            for subject_id in subject_ids:
                try:
                    subject = Subject.objects.get(id=subject_id, is_active=True)
                    
                    # Check if already enrolled
                    if not Enrollment.objects.filter(student=request.user, subject=subject).exists():
                        # Ensure subject grade level matches student's grade to avoid model ValidationError
                        student_grade = getattr(student_profile, 'grade_level', None) if student_profile else None

                        if student_grade is not None and subject.grade_level != student_grade:
                            messages.error(request, f"Cannot register {subject.name}: Subject is for Grade {subject.grade_level} but you are Grade {student_grade}.")
                            continue

                        # Check capacity
                        current_enroll_count = Enrollment.objects.filter(
                            subject=subject, 
                            status__in=['approved', 'pending']
                        ).count()

                        try:
                            if current_enroll_count < subject.max_capacity:
                                Enrollment.objects.create(
                                    student=request.user,
                                    subject=subject,
                                    status='pending',
                                    academic_year=current_academic_year,
                                    semester=current_semester
                                )
                                success_count += 1
                                messages.success(request, f'Successfully registered for {subject.name}. Waiting for approval.')
                            else:
                                # Add to waitlist if subject is full
                                Enrollment.objects.create(
                                    student=request.user,
                                    subject=subject,
                                    status='waitlisted'
                                )
                                messages.info(request, f'Subject {subject.name} is full. You have been added to the waitlist.')
                        except ValidationError as ve:
                            messages.error(request, f'Failed to register {subject.name}: {"; ".join(ve.messages) if hasattr(ve, "messages") else str(ve)}')
                        except IntegrityError as ie:
                            messages.error(request, f'Failed to register {subject.name}: {str(ie)}')
                    else:
                        messages.warning(request, f'You are already enrolled in {subject.name}.')
                        
                except Subject.DoesNotExist:
                    messages.error(request, "Invalid subject selected.")
            
            if success_count > 0:
                messages.success(request, f'Successfully registered for {success_count} subject(s)!')
            return redirect('subject_registration')
    
    context = {
        'available_subjects': available_subjects,
        'enrolled_subjects': enrolled_subjects,
        'enrolled_subject_ids': list(enrolled_subject_ids),
        'enrollments': student_enrollments,
        'current_academic_year': current_academic_year,
        'current_semester': current_semester,
        'current_grade': current_grade,
        'grade_choices': list(range(1, 9)),
    }
    if assigned_only:
        context['assigned_only'] = True
    return render(request, 'students/subject_registration.html', context)

def view_transcripts(request):
    try:
        student = Student.objects.get(user=request.user)
        
        # Get all completed enrollments
        completed_enrollments = Enrollment.objects.filter(
            student=student,
            status='completed'
        ).select_related('class_enrolled__subject')
        
        transcript_data = []
        total_credits = 0
        total_points = 0
        
        for enrollment in completed_enrollments:
            subject = enrollment.class_enrolled.subject
            
            # Calculate average score for this subject
            scores = StudentScore.objects.filter(
                enrollment=enrollment,
                points_earned__isnull=False
            )
            
            if scores.exists():
                average_score = sum(float(score.points_earned) for score in scores) / scores.count()
                
                # For elementary, use 1.0 credit per subject or calculate based on something else
                credits = getattr(subject, 'credits', 1.0)  # Default to 1.0 if credits doesn't exist
                
                # Convert score to grade points (elementary-friendly)
                if average_score >= 90:
                    grade = 'A'
                    grade_points = 4.0
                elif average_score >= 80:
                    grade = 'B' 
                    grade_points = 3.0
                elif average_score >= 70:
                    grade = 'C'
                    grade_points = 2.0
                elif average_score >= 60:
                    grade = 'D'
                    grade_points = 1.0
                else:
                    grade = 'F'
                    grade_points = 0.0
                
                transcript_data.append({
                    'subject_code': subject.code,
                    'subject_name': subject.name,
                    'grade': grade,
                    'score': round(average_score, 2),
                    'credits': float(credits),
                    'grade_points': grade_points,
                    'academic_year': enrollment.class_enrolled.academic_year.name,
                    'semester': enrollment.class_enrolled.semester.name
                })
                
                total_credits += float(credits)
                total_points += grade_points * float(credits)
        
        # Calculate GPA
        gpa = total_points / total_credits if total_credits > 0 else 0
        
        # Elementary-friendly performance summary
        performance_summary = get_performance_summary(gpa)
        
        return render(request, 'students/transcript.html', {
            'student': student,
            'transcript_data': transcript_data,
            'total_credits': round(total_credits, 2),
            'gpa': round(gpa, 2),
            'performance_summary': performance_summary
        })
        
    except Student.DoesNotExist:
        return render(request, 'error.html', {'message': 'Student profile not found'})

def get_performance_summary(gpa):
    """Get elementary-friendly performance description"""
    if gpa >= 3.5:
        return "🌟 Outstanding! You're doing excellent work!"
    elif gpa >= 3.0:
        return "👍 Very Good! Keep up the great work!"
    elif gpa >= 2.0:
        return "✅ Good job! You're making good progress."
    elif gpa >= 1.0:
        return "📝 Keep practicing! You're getting there."
    else:
        return "💪 Don't give up! Let's work together to improve."

@student_required
def pay_fees(request):
    fee_structures = FeeStructure.objects.filter(is_active=True)
    student_payments = Payment.objects.filter(student=request.user).select_related('fee_structure')
    
    if request.method == 'POST':
        fee_structure_id = request.POST.get('fee_structure_id')
        fee_structure = get_object_or_404(FeeStructure, id=fee_structure_id)
        
        # Check if payment already exists
        if Payment.objects.filter(student=request.user, fee_structure=fee_structure, status='completed').exists():
            messages.warning(request, 'You have already paid this fee.')
        else:
            # Create payment (simplified - integrate with payment gateway in production)
            payment = Payment.objects.create(
                student=request.user,
                fee_structure=fee_structure,
                amount_paid=fee_structure.amount,
                payment_method='Online',
                transaction_id=f"TXN{request.user.id}{timezone.now().timestamp()}",
                status='completed'
            )
            
            messages.success(request, f'Payment of ${fee_structure.amount} completed successfully!')
        
        return redirect('pay_fees')
    
    context = {
        'fee_structures': fee_structures,
        'payments': student_payments,
    }
    return render(request, 'students/pay_fees.html', context)

@student_required
def view_announcements(request):
    announcements = Announcement.objects.filter(
        Q(target_roles__contains=[request.user.role]) | Q(target_roles__contains=[]),
        is_active=True
    ).order_by('-created_at')
    
    context = {
        'announcements': announcements,
    }
    return render(request, 'students/announcements.html', context)

@student_required
def interact_with_ai(request):
    # Get or create conversation
    conversation, created = AIConversation.objects.get_or_create(user=request.user)
    messages = AIMessage.objects.filter(conversation=conversation).order_by('created_at')
    
    if request.method == 'POST':
        user_message = request.POST.get('message')
        
        if user_message:
            # Save user message
            AIMessage.objects.create(
                conversation=conversation,
                content=user_message,
                is_user=True
            )
            
            # Generate AI response (simplified - integrate with actual AI in production)
            ai_response = generate_ai_response(user_message, request.user)
            
            # Save AI response
            AIMessage.objects.create(
                conversation=conversation,
                content=ai_response,
                is_user=False
            )
            
            return redirect('interact_with_ai')
    
    context = {
        'conversation': conversation,
        'messages': messages,
    }
    return render(request, 'students/ai_advisor.html', context)

def generate_ai_response(message, user):
    # Simplified AI response generation
    # In production, integrate with actual AI service like OpenAI GPT
    responses = {
        'subject': f"I can help you with subject selection. Based on your profile, I recommend checking available subjects in your program.",
        'grade': "I can provide information about grading policies and help you understand your academic performance.",
        'fee': "For fee-related queries, please check the finance section or contact the finance office.",
        'default': "I'm here to help with your academic journey. How can I assist you with subject selection, grades, or general academic advice?"
    }
    
    message_lower = message.lower()
    for key in responses:
        if key in message_lower:
            return responses[key]
    
    return responses['default']

def get_grade_point(grade_letter):
    """Convert letter grade to grade point"""
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

class StudentViewSet(viewsets.ModelViewSet):
    serializer_class = StudentSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        # Return only the student profile for the current user
        return Student.objects.filter(user=self.request.user)
    
    @action(detail=False, methods=['get'])
    def subject_registration(self, request):
        """
        GET /api/students/subject_registration/
        Returns available subjects for registration
        """
        try:
            student = request.user.student_profile
            
            # Get available subjects (subjects not enrolled by this student)
            available_subjects = Subject.objects.exclude(
                enrollments__student=student
            ).filter(is_active=True)
            
            # Get currently enrolled subjects
            enrolled_subjects = Subject.objects.filter(
                enrollments__student=student
            )
            
            available_serializer = SubjectSerializer(available_subjects, many=True)
            enrolled_serializer = SubjectSerializer(enrolled_subjects, many=True)
            
            return Response({
                'available_subjects': available_serializer.data,
                'enrolled_subjects': enrolled_serializer.data,
                'total_available': available_subjects.count(),
                'total_enrolled': enrolled_subjects.count()
            })
            
        except AttributeError:
            return Response(
                {'error': 'Student profile not found. Please complete your student profile.'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=False, methods=['get'])
    def subject_registration(self, request):
        """Temporary redirect to subject_registration"""
        return Response({
            "message": "This endpoint has been moved to subject_registration",
            "new_endpoint": "/api/students/subject_registration/",
            "note": "Please update your application to use the new endpoint"
        })

def debug_urls(request):
    return JsonResponse({
        'available_urls': [
            '/api/students/subject_registration/',
            '/api/students/',
        ],
        'requested_url': request.build_absolute_uri(),
        'message': 'Use /api/students/subject_registration/ instead of course-registration'
    })
    