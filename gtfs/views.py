from rest_framework import generics

from gtfs.models import Stop
from gtfs.serializers import StopSerializer


class StopListView(generics.ListAPIView):
    queryset = Stop.objects.all()
    serializer_class = StopSerializer
