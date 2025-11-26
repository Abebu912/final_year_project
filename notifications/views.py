# In your views.py (wherever the announcements view is defined)
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def student_announcements_api(request):
    """API endpoint for student announcements"""
    # Your logic to get announcements for the student
    announcements = [
        {
            "id": 1,
            "title": "Welcome Back to School!",
            "content": "We're excited to start the new school year! Remember to bring your supplies tomorrow.",
            "created_at": "2025-01-20T10:00:00Z",
            "is_important": True
        },
        {
            "id": 2, 
            "title": "Math Test Next Week",
            "content": "There will be a math test on addition and subtraction next Monday.",
            "created_at": "2025-01-19T14:30:00Z",
            "is_important": False
        }
    ]
    
    return Response({
        "announcements": announcements,
        "count": len(announcements)
    })
from django.shortcuts import render
from django.contrib.auth.decorators import login_required

@login_required
def student_announcements(request):
    """Render the student announcements page"""
    # Add any context data you need
    context = {
        'page_title': 'School Announcements',
        'student': request.user.student_profile
    }
    return render(request, 'students/announcements.html', context)
# In your views.py (could be in announcements app or students app)
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from datetime import timedelta

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def student_announcements_api(request):
    """API endpoint for student announcements"""
    
    # Sample announcements data - replace with your actual data
    announcements = [
        {
            "id": 1,
            "title": "Welcome Back to School! 🎒",
            "content": "We're excited to start the new school year! Remember to bring your supplies tomorrow and wear your school uniform.",
            "created_at": timezone.now().isoformat(),
            "is_important": True
        },
        {
            "id": 2,
            "title": "Math Test Next Week ➕",
            "content": "There will be a math test on addition and subtraction next Monday. Practice your math facts every day!",
            "created_at": (timezone.now() - timedelta(days=1)).isoformat(),
            "is_important": False
        },
        {
            "id": 3,
            "title": "Art Show This Friday 🎨",
            "content": "Our annual art show will be this Friday in the school gym. All parents are welcome to attend from 2-4 PM.",
            "created_at": (timezone.now() - timedelta(days=3)).isoformat(),
            "is_important": True
        }
    ]
    
    return Response({
        "announcements": announcements,
        "count": len(announcements),
        "last_updated": timezone.now().isoformat()
    })