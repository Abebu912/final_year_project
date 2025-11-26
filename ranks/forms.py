from django import forms
from .models import Grade

class GradeForm(forms.ModelForm):
    score = forms.IntegerField(min_value=0, max_value=100, required=False)
    remarks = forms.CharField(widget=forms.Textarea(attrs={'rows':3}), required=False)

    class Meta:
        model = Grade
        fields = ['score', 'remarks']
