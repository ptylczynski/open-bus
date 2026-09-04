from decimal import Decimal

from gtfs.models import Stop
from gtfs.services.routing_data import great_circle_distance


class NearestStopService:
    """Resolve geographic coordinates to the closest imported GTFS stop."""

    @staticmethod
    def find_nearest(
        latitude: Decimal,
        longitude: Decimal,
    ) -> Stop | None:
        coordinates = (float(latitude), float(longitude))
        return min(
            Stop.objects.order_by().iterator(),
            key=lambda stop: (
                great_circle_distance(
                    coordinates,
                    (float(stop.stop_lat), float(stop.stop_lon)),
                ),
                stop.stop_id,
            ),
            default=None,
        )
