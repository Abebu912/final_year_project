# students/urls.py
from django.urls import path
from . import views

urlpatterns = [
    # Template URLs only
    path('dashboard/', views.student_dashboard, name='student_dashboard'),
    path('subject-registration/', views.subject_registration, name='subject_registration'),
    path('transcripts/', views.view_transcripts, name='view_transcripts'),
    path('pay-fees/', views.pay_fees, name='pay_fees'),
    path('ai-advisor/', views.interact_with_ai, name='interact_with_ai'),
    path('announcements/', views.view_announcements, name='view_announcements'),
]