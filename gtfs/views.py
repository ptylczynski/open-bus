import unicodedata
from collections.abc import Iterable

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
            'Find route alternatives between two stops after the requested '
            'departure time.'
        ),
    )
    def post(self, request: Request) -> Response:
        request_serializer = RouteRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        from_stop = request_serializer.validated_data['from_stop']
        to_stop = request_serializer.validated_data['to_stop']
        route_service = RouteSelectionService()
        routes = route_service.find_routes(
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
        if not routes:
            return Response(
                {
                    'detail': (
                        'No route found within '
                        f'{route_service.max_hops} hop(s).'
                    ),
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        stops_by_id = Stop.objects.in_bulk(
            {
                stop_id
                for route in routes
                for stop_id in route.stop_ids
            },
        )
        response_routes = [
            {
                'hops': route.hops,
                'transfers': [
                    {
                        'stop': stops_by_id[next_leg.from_stop_id],
                        'arrival_time': previous_leg.arrival_time,
                        'departure_time': next_leg.departure_time,
                        'wait_time': (
                            next_leg.departure_time
                            - previous_leg.arrival_time
                        ),
                        'from_route_id': previous_leg.route_id,
                        'from_line_number': previous_leg.line_number,
                        'to_route_id': next_leg.route_id,
                        'to_line_number': next_leg.line_number,
                    }
                    for previous_leg, next_leg in zip(
                        route.legs,
                        route.legs[1:],
                    )
                ],
                'legs': [
                    {
                        'trip_id': leg.trip_id,
                        'route_id': leg.route_id,
                        'line_number': leg.line_number,
                        'line_name': leg.line_name,
                        'direction': leg.direction,
                        'direction_id': leg.direction_id,
                        'from_stop': stops_by_id[leg.from_stop_id],
                        'to_stop': stops_by_id[leg.to_stop_id],
                        'departure_time': leg.departure_time,
                        'arrival_time': leg.arrival_time,
                        'stops': [
                            stops_by_id[stop_id]
                            for stop_id in leg.stop_ids
                        ],
                    }
                    for leg in route.legs
                ],
                'stops': [
                    stops_by_id[stop_id]
                    for stop_id in route.stop_ids
                ],
            }
            for route in routes
        ]

        response_serializer = RouteResponseSerializer(
            {'routes': response_routes},
        )
        return Response(response_serializer.data)


class StopListView(generics.ListAPIView):
    """List one representative of each imported stop name."""

    queryset = Stop.objects.order_by('stop_name', 'stop_code', 'stop_id')
    serializer_class = StopSerializer

    def list(
        self,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        stops = deduplicate_stops_by_name(self.get_queryset())
        return Response(self.get_serializer(stops, many=True).data)


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
        matching_stops = [
            stop
            for stop in Stop.objects.all()
            if normalize_stop_name(stop.stop_name).startswith(prefix)
        ]
        stops = deduplicate_stops_by_name(matching_stops)
        return Response(StopSerializer(stops, many=True).data)


def deduplicate_stops_by_name(stops: Iterable[Stop]) -> list[Stop]:
    unique_stops = []
    seen_stop_names = set()
    for stop in stops:
        if stop.stop_name in seen_stop_names:
            continue
        unique_stops.append(stop)
        seen_stop_names.add(stop.stop_name)
    return unique_stops


def normalize_stop_name(value: str) -> str:
    value = value.translate(str.maketrans({'ł': 'l', 'Ł': 'L'}))
    decomposed = unicodedata.normalize('NFKD', value)
    return ''.join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    ).casefold()
