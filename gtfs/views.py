from drf_spectacular.utils import extend_schema
import unicodedata

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from gtfs.models import Stop
from gtfs.serializers import (
    ErrorResponseSerializer,
    RouteRequestSerializer,
    RouteResponseSerializer,
    StopSerializer,
    StopSuggestionQuerySerializer,
)
from gtfs.services import RouteSelectionService


class RouteCreateView(APIView):
    @extend_schema(
        request=RouteRequestSerializer,
        responses={
            200: RouteResponseSerializer,
            404: ErrorResponseSerializer,
        },
        description=(
            'Find the earliest route between two stops after the requested '
            'departure time.'
        ),
    )
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
    """List all imported stops ordered by name and stop code."""

    queryset = Stop.objects.all()
    serializer_class = StopSerializer


class StopSuggestionView(APIView):
    @extend_schema(
        parameters=[
            OpenApiParameter(
                name='name',
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    'At least three characters. Matching ignores case and '
                    'diacritics.'
                ),
                required=True,
            ),
        ],
        responses=StopSerializer(many=True),
        description='Suggest stops whose names start with the given prefix.',
    )
    def get(self, request: Request) -> Response:
        query_serializer = StopSuggestionQuerySerializer(
            data=request.query_params,
        )
        query_serializer.is_valid(raise_exception=True)
        prefix = normalize_stop_name(query_serializer.validated_data['name'])
        stops = [
            stop
            for stop in Stop.objects.all()
            if normalize_stop_name(stop.stop_name).startswith(prefix)
        ]
        return Response(StopSerializer(stops, many=True).data)


def normalize_stop_name(value: str) -> str:
    value = value.translate(str.maketrans({'ł': 'l', 'Ł': 'L'}))
    decomposed = unicodedata.normalize('NFKD', value)
    return ''.join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    ).casefold()
