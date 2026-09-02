from django.urls import path

from gtfs.views import RouteCreateView, StopListView, StopSuggestionView


urlpatterns = [
    path('routes/', RouteCreateView.as_view(), name='route-create'),
    path(
        'stops/suggest/',
        StopSuggestionView.as_view(),
        name='stop-suggest',
    ),
    path('stops/', StopListView.as_view(), name='stop-list'),
]
