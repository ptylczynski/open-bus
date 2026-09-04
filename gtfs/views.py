import unicodedata
from datetime import timedelta

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from gtfs.models import Stop
from gtfs.serializers import (
    ErrorResponseSerializer,
    GeocodeQuerySerializer,
    GeocodeSuggestionSerializer,
    RouteRequestSerializer,
    RouteResponseSerializer,
    StopSerializer,
    StopSuggestionQuerySerializer,
)
from gtfs.services import (
    HereAutosuggestConfigurationError,
    HereAutosuggestError,
    HereAutosuggestService,
    RouteLeg,
    RouteOption,
    RouteSelectionService,
    WalkLeg,
)


class GeocodeView(APIView):
    @extend_schema(
        parameters=[
            OpenApiParameter(
                name='text',
                type=str,
                location=OpenApiParameter.QUERY,
                description='Text to geocode. Must be longer than 3 characters.',
                required=True,
            ),
        ],
        responses={
            200: GeocodeSuggestionSerializer(many=True),
            502: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
        description='Return HERE autosuggestions with names and coordinates.',
    )
    def get(self, request: Request) -> Response:
        query_serializer = GeocodeQuerySerializer(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)
        try:
            suggestions = HereAutosuggestService().suggest(
                query_serializer.validated_data['text'],
            )
        except HereAutosuggestConfigurationError as error:
            return Response(
                {'detail': str(error)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except HereAutosuggestError as error:
            return Response(
                {'detail': str(error)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(GeocodeSuggestionSerializer(suggestions, many=True).data)


class RouteCreateView(APIView):
    @extend_schema(
        request=RouteRequestSerializer,
        responses={
            200: RouteResponseSerializer,
            404: ErrorResponseSerializer,
        },
        description=(
            'Find route alternatives between two stops, or the stops nearest '
            'to supplied coordinates, after the requested departure time.'
        ),
    )
    def post(self, request: Request) -> Response:
        request_serializer = RouteRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        from_stop = request_serializer.validated_data['from_stop']
        to_stop = request_serializer.validated_data['to_stop']
        route_service = RouteSelectionService(
            max_hops=request_serializer.validated_data.get('hops'),
        )
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
            service_date=request_serializer.validated_data['service_date'],
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
        requested_departure = request_serializer.validated_data['departure_time']
        response_routes = [
            route_response(route, stops_by_id, requested_departure)
            for route in routes
        ]

        response_serializer = RouteResponseSerializer(
            {
                'service_date': request_serializer.validated_data['service_date'],
                'from_stop': from_stop,
                'to_stop': to_stop,
                'routes': response_routes,
            },
        )
        return Response(response_serializer.data)


class StopListView(generics.ListAPIView):
    """List one representative physical stop for each stop name."""

    queryset = (
        Stop.objects.order_by('stop_name', 'stop_code', 'stop_id')
        .distinct('stop_name')
    )
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
        matching_stops = sorted(
            [
                stop
                for stop in Stop.objects.all()
                if normalize_stop_name(stop.stop_name).startswith(prefix)
            ],
            key=lambda stop: (stop.stop_name, stop.stop_code, stop.stop_id),
        )
        unique_stops = {}
        for stop in matching_stops:
            unique_stops.setdefault(stop.stop_name, stop)
        return Response(
            StopSerializer(unique_stops.values(), many=True).data,
        )


def normalize_stop_name(value: str) -> str:
    value = value.translate(str.maketrans({'ł': 'l', 'Ł': 'L'}))
    decomposed = unicodedata.normalize('NFKD', value)
    return ''.join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    ).casefold()


def route_response(
    route: RouteOption,
    stops_by_id: dict[str, Stop],
    requested_departure: timedelta,
) -> dict[str, object]:
    segments = route.segments or route.legs
    departure = (
        route.departure_time
        or (segments[0].departure_time if segments else requested_departure)
    )
    return {
        'hops': route.hops,
        'departure_time': departure,
        'arrival_time': route.arrival_time,
        'duration': route.arrival_time - departure,
        'walking_time': route.walking_time,
        'transfers': transfer_responses(route, stops_by_id),
        'legs': [leg_response(leg, stops_by_id) for leg in route.legs],
        'segments': [segment_response(segment, stops_by_id) for segment in segments],
        'stops': [stops_by_id[stop_id] for stop_id in route.stop_ids],
    }


def leg_response(
    leg: RouteLeg,
    stops_by_id: dict[str, Stop],
) -> dict[str, object]:
    return {
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
        'stops': [stops_by_id[stop_id] for stop_id in leg.stop_ids],
    }


def segment_response(
    segment: RouteLeg | WalkLeg,
    stops_by_id: dict[str, Stop],
) -> dict[str, object]:
    if isinstance(segment, RouteLeg):
        return {
            'mode': 'transit',
            'duration': segment.arrival_time - segment.departure_time,
            **leg_response(segment, stops_by_id),
        }
    return {
        'mode': 'walk',
        'from_stop': stops_by_id[segment.from_stop_id],
        'to_stop': stops_by_id[segment.to_stop_id],
        'departure_time': segment.departure_time,
        'arrival_time': segment.arrival_time,
        'duration': segment.duration,
        'distance_meters': segment.distance_meters,
    }


def transfer_responses(
    route: RouteOption,
    stops_by_id: dict[str, Stop],
) -> list[dict[str, object]]:
    segments = route.segments or route.legs
    transit_positions = [
        index
        for index, segment in enumerate(segments)
        if isinstance(segment, RouteLeg)
    ]
    responses = []
    for transfer_index, (previous_leg, next_leg) in enumerate(
        zip(route.legs, route.legs[1:]),
    ):
        walking_time = timedelta(0)
        if len(transit_positions) > transfer_index + 1:
            previous_position = transit_positions[transfer_index]
            next_position = transit_positions[transfer_index + 1]
            walking_time = sum(
                (
                    segment.duration
                    for segment in segments[previous_position + 1:next_position]
                    if isinstance(segment, WalkLeg)
                ),
                timedelta(0),
            )
        responses.append(
            {
                'stop': stops_by_id[next_leg.from_stop_id],
                'from_stop': stops_by_id[previous_leg.to_stop_id],
                'to_stop': stops_by_id[next_leg.from_stop_id],
                'arrival_time': previous_leg.arrival_time,
                'departure_time': next_leg.departure_time,
                'wait_time': next_leg.departure_time - previous_leg.arrival_time,
                'walk_time': walking_time,
                'from_route_id': previous_leg.route_id,
                'from_line_number': previous_leg.line_number,
                'to_route_id': next_leg.route_id,
                'to_line_number': next_leg.line_number,
            }
        )
    return responses
