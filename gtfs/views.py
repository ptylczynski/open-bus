from rest_framework import generics, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from gtfs.models import Stop
from gtfs.serializers import RouteRequestSerializer, StopSerializer
from gtfs.services import RouteSelectionService


class RouteCreateView(APIView):
    def post(self, request: Request) -> Response:
        request_serializer = RouteRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        from_stop = request_serializer.validated_data['from_stop']
        to_stop = request_serializer.validated_data['to_stop']
        route_service = RouteSelectionService()
        stop_ids = route_service.find_route(
            from_stop.stop_id,
            to_stop.stop_id,
            departure_time=request_serializer.validated_data['departure_time'],
            min_exchange_time=(
                request_serializer.validated_data['min_exchange_time']
            ),
            max_exchange_time=(
                request_serializer.validated_data['max_exchange_time']
            ),
        )
        if stop_ids is None:
            return Response(
                {
                    'detail': (
                        'No route found within '
                        f'{route_service.max_hops} hop(s).'
                    ),
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        stops_by_id = Stop.objects.in_bulk(stop_ids)
        stops = [stops_by_id[stop_id] for stop_id in stop_ids]

        return Response({'stops': StopSerializer(stops, many=True).data})


class StopListView(generics.ListAPIView):
    queryset = Stop.objects.all()
    serializer_class = StopSerializer
