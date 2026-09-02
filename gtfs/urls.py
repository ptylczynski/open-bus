from django.urls import path

from gtfs.views import RouteCreateView, StopListView


urlpatterns = [
    path('routes/', RouteCreateView.as_view(), name='route-create'),
    path('stops/', StopListView.as_view(), name='stop-list'),
]
