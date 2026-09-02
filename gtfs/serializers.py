from datetime import timedelta

from django.conf import settings
from rest_framework import serializers

from gtfs.models import Stop


class RouteRequestSerializer(serializers.Serializer):
    from_stop_id = serializers.PrimaryKeyRelatedField(
        queryset=Stop.objects.all(),
        source='from_stop',
    )
    to_stop_id = serializers.PrimaryKeyRelatedField(
        queryset=Stop.objects.all(),
        source='to_stop',
    )
    departure_time = serializers.DurationField(
        min_value=timedelta(0),
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
    arrival_time = serializers.DurationField()
    departure_time = serializers.DurationField()
    wait_time = serializers.DurationField()
    from_route_id = serializers.CharField()
    from_line_number = serializers.CharField(allow_blank=True)
    to_route_id = serializers.CharField()
    to_line_number = serializers.CharField(allow_blank=True)


class RouteAlternativeSerializer(serializers.Serializer):
    hops = serializers.IntegerField(min_value=0)
    transfers = RouteTransferSerializer(many=True)
    legs = RouteLegSerializer(many=True)
    stops = StopSerializer(many=True)


class RouteResponseSerializer(serializers.Serializer):
    routes = RouteAlternativeSerializer(many=True)


class ErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class StopSuggestionQuerySerializer(serializers.Serializer):
    name = serializers.CharField(min_length=3, trim_whitespace=True)
