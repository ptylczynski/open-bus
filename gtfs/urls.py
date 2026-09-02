from django.urls import path

from gtfs.views import StopListView


urlpatterns = [
    path('stops/', StopListView.as_view(), name='stop-list'),
]
