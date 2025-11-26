from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.registrar_dashboard, name='registrar_dashboard'),
    path('approve-registrations/', views.approve_registrations, name='approve_registrations'),
    path('academic-records/', views.manage_academic_records, name='manage_academic_records'),
    path('waitlist/<int:subject_id>/', views.handle_waitlist, name='handle_waitlist'),
    path('generate-transcripts/', views.generate_transcripts, name='generate_transcripts'),
]