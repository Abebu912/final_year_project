from django.urls import path
from . import views
from users import views as user_views
urlpatterns = [
    path('dashboard/', views.teacher_dashboard, name='teacher_dashboard'),
    path('enter-grades/', views.enter_grades, name='enter_grades'),
    path('class-rosters/', views.class_rosters, name='class_rosters'),
    path('performance-reports/', views.performance_reports, name='performance_reports'),
    path('update-grade/<int:enrollment_id>/', views.update_student_grade, name='update_student_grade'),
    # Registrar/teacher helper to enter numeric score for a student-subject
    path('enter-score/<int:student_id>/<int:subject_id>/', user_views.enter_numeric_score, name='enter_numeric_score'),
    path('bulk-score-upload/<int:subject_id>/', views.bulk_grade_upload, name='bulk_grade_upload'),
    path('claim-subject/<int:subject_id>/', views.claim_subject, name='claim_subject'),
    path('subject-statistics/<int:subject_id>/', views.get_subject_statistics, name='get_subject_statistics'),
    path('assign-subjects/', views.assign_subjects_for_term, name='assign_subjects_for_term'),
    path('assign-selected-subjects/', views.assign_selected_subjects, name='assign_selected_subjects'),
    path('save-score/', views.save_student_score, name='save_student_score'),
]