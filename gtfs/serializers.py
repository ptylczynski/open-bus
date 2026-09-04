from datetime import timedelta

from django.conf import settings
from rest_framework import serializers

from gtfs.models import Stop
from gtfs.services.routing_data import default_service_date
from gtfs.services.stop_locator import NearestStopService


class RouteRequestSerializer(serializers.Serializer):
    from_stop_id = serializers.PrimaryKeyRelatedField(
        queryset=Stop.objects.all(),
        source='from_stop',
        required=False,
    )
    to_stop_id = serializers.PrimaryKeyRelatedField(
        queryset=Stop.objects.all(),
        source='to_stop',
        required=False,
    )
    from_lat = serializers.DecimalField(
        max_digits=15,
        decimal_places=12,
        min_value=-90,
        max_value=90,
        required=False,
    )
    from_lon = serializers.DecimalField(
        max_digits=15,
        decimal_places=12,
        min_value=-180,
        max_value=180,
        required=False,
    )
    to_lat = serializers.DecimalField(
        max_digits=15,
        decimal_places=12,
        min_value=-90,
        max_value=90,
        required=False,
    )
    to_lon = serializers.DecimalField(
        max_digits=15,
        decimal_places=12,
        min_value=-180,
        max_value=180,
        required=False,
    )
    departure_time = serializers.DurationField(
        min_value=timedelta(0),
    )
    service_date = serializers.DateField(required=False)
    hops = serializers.IntegerField(
        min_value=0,
        required=False,
    )
    min_exchange_time = serializers.DurationField(
        min_value=timedelta(0),
        required=False,
    )
    max_exchange_time = serializers.DurationField(
        min_value=timedelta(0),
        required=False,
    )

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        location_errors = {}
        for prefix in ('from', 'to'):
            location_errors.update(self._resolve_stop(attrs, prefix))
        if location_errors:
            raise serializers.ValidationError(location_errors)

        if 'service_date' not in attrs:
            try:
                attrs['service_date'] = default_service_date()
            except ValueError as error:
                raise serializers.ValidationError(
                    {'service_date': str(error)},
                ) from error
        min_exchange_time = attrs.setdefault(
            'min_exchange_time',
            timedelta(seconds=settings.ROUTE_MIN_EXCHANGE_TIME_SECONDS),
        )
        max_exchange_time = attrs.setdefault(
            'max_exchange_time',
            timedelta(seconds=settings.ROUTE_MAX_EXCHANGE_TIME_SECONDS),
        )
        if min_exchange_time > max_exchange_time:
            raise serializers.ValidationError(
                {
                    'max_exchange_time': (
                        'Must be greater than or equal to min_exchange_time.'
                    ),
                },
            )
        return attrs

    @staticmethod
    def _resolve_stop(
        attrs: dict[str, object],
        prefix: str,
    ) -> dict[str, str]:
        stop_key = f'{prefix}_stop'
        latitude_key = f'{prefix}_lat'
        longitude_key = f'{prefix}_lon'
        has_stop = stop_key in attrs
        has_latitude = latitude_key in attrs
        has_longitude = longitude_key in attrs

        if has_stop and (has_latitude or has_longitude):
            return {
                f'{prefix}_stop_id': (
                    'Do not combine a stop ID with coordinates.'
                ),
            }
        if has_stop:
            return {}
        if not has_latitude and not has_longitude:
            return {
                f'{prefix}_stop_id': (
                    f'Provide {prefix}_stop_id or both {latitude_key} and '
                    f'{longitude_key}.'
                ),
            }
        if not has_latitude:
            return {latitude_key: 'This field is required with coordinates.'}
        if not has_longitude:
            return {longitude_key: 'This field is required with coordinates.'}

        stop = NearestStopService.find_nearest(
            attrs[latitude_key],
            attrs[longitude_key],
        )
        if stop is None:
            return {latitude_key: 'No stops are available.'}
        attrs[stop_key] = stop
        return {}


class StopSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stop
        fields = (
            'stop_id',
            'stop_code',
            'stop_name',
            'stop_lat',
            'stop_lon',
            'zone_id',
        )


class RouteLegSerializer(serializers.Serializer):
    trip_id = serializers.CharField()
    route_id = serializers.CharField()
    line_number = serializers.CharField(allow_blank=True)
    line_name = serializers.CharField(allow_blank=True)
    direction = serializers.CharField(allow_blank=True)
    direction_id = serializers.IntegerField(allow_null=True)
    from_stop = StopSerializer()
    to_stop = StopSerializer()
    departure_time = serializers.DurationField()
    arrival_time = serializers.DurationField()
    stops = StopSerializer(many=True)


class RouteTransferSerializer(serializers.Serializer):
    stop = StopSerializer()
    from_stop = StopSerializer()
    to_stop = StopSerializer()
    arrival_time = serializers.DurationField()
    departure_time = serializers.DurationField()
    wait_time = serializers.DurationField()
    walk_time = serializers.DurationField()
    from_route_id = serializers.CharField()
    from_line_number = serializers.CharField(allow_blank=True)
    to_route_id = serializers.CharField()
    to_line_number = serializers.CharField(allow_blank=True)


class RouteSegmentSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=('transit', 'walk'))
    from_stop = StopSerializer()
    to_stop = StopSerializer()
    departure_time = serializers.DurationField()
    arrival_time = serializers.DurationField()
    duration = serializers.DurationField()
    distance_meters = serializers.IntegerField(min_value=0, required=False)
    trip_id = serializers.CharField(required=False)
    route_id = serializers.CharField(required=False)
    line_number = serializers.CharField(allow_blank=True, required=False)
    line_name = serializers.CharField(allow_blank=True, required=False)
    direction = serializers.CharField(allow_blank=True, required=False)
    direction_id = serializers.IntegerField(allow_null=True, required=False)
    stops = StopSerializer(many=True, required=False)


class RouteAlternativeSerializer(serializers.Serializer):
    hops = serializers.IntegerField(min_value=0)
    departure_time = serializers.DurationField()
    arrival_time = serializers.DurationField()
    duration = serializers.DurationField()
    walking_time = serializers.DurationField()
    transfers = RouteTransferSerializer(many=True)
    legs = RouteLegSerializer(many=True)
    segments = RouteSegmentSerializer(many=True)
    stops = StopSerializer(many=True)


class RouteResponseSerializer(serializers.Serializer):
    service_date = serializers.DateField()
    from_stop = StopSerializer()
    to_stop = StopSerializer()
    routes = RouteAlternativeSerializer(many=True)


class ErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class GeocodeQuerySerializer(serializers.Serializer):
    text = serializers.CharField(min_length=4, trim_whitespace=True)


class CoordinatesSerializer(serializers.Serializer):
    lat = serializers.FloatField()
    lng = serializers.FloatField()


class GeocodeSuggestionSerializer(serializers.Serializer):
    name = serializers.CharField()
    coordinates = CoordinatesSerializer()


class StopSuggestionQuerySerializer(serializers.Serializer):
    name = serializers.CharField(min_length=3, trim_whitespace=True)
