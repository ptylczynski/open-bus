from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models


GTFS_ID_MAX_LENGTH = 255

hex_color_validator = RegexValidator(
    regex=r"^[0-9A-Fa-f]{6}$",
    message="Enter a six-character hexadecimal color without the leading #.",
)


class RouteType(models.IntegerChoices):
    TRAM = 0, "Tram, streetcar, or light rail"
    SUBWAY = 1, "Subway or metro"
    RAIL = 2, "Rail"
    BUS = 3, "Bus"
    FERRY = 4, "Ferry"
    CABLE_TRAM = 5, "Cable tram"
    AERIAL_LIFT = 6, "Aerial lift"
    FUNICULAR = 7, "Funicular"
    TROLLEYBUS = 11, "Trolleybus"
    MONORAIL = 12, "Monorail"


class PickupDropOffType(models.IntegerChoices):
    REGULAR = 0, "Regularly scheduled"
    NONE = 1, "Not available"
    PHONE_AGENCY = 2, "Arrange by phone"
    COORDINATE_WITH_DRIVER = 3, "Coordinate with driver"


class ExceptionType(models.IntegerChoices):
    ADDED = 1, "Service added"
    REMOVED = 2, "Service removed"


class WheelchairAccessibility(models.IntegerChoices):
    UNKNOWN = 0, "No accessibility information"
    ACCESSIBLE = 1, "Wheelchair accessible"
    NOT_ACCESSIBLE = 2, "Not wheelchair accessible"


class Agency(models.Model):
    agency_id = models.CharField(primary_key=True, max_length=GTFS_ID_MAX_LENGTH)
    agency_name = models.CharField(max_length=255)
    agency_url = models.URLField(max_length=500)
    agency_timezone = models.CharField(max_length=63)
    agency_phone = models.CharField(max_length=255, blank=True)
    agency_lang = models.CharField(max_length=35, blank=True)

    class Meta:
        verbose_name_plural = "agencies"

    def __str__(self):
        return self.agency_name


class Calendar(models.Model):
    service_id = models.CharField(primary_key=True, max_length=GTFS_ID_MAX_LENGTH)
    monday = models.BooleanField()
    tuesday = models.BooleanField()
    wednesday = models.BooleanField()
    thursday = models.BooleanField()
    friday = models.BooleanField()
    saturday = models.BooleanField()
    sunday = models.BooleanField()
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ("service_id",)

    def __str__(self):
        return self.service_id


class CalendarDate(models.Model):
    service = models.ForeignKey(
        Calendar,
        on_delete=models.CASCADE,
        related_name="exceptions",
    )
    date = models.DateField()
    exception_type = models.PositiveSmallIntegerField(choices=ExceptionType)

    class Meta:
        ordering = ("date", "service_id")
        constraints = [
            models.UniqueConstraint(
                fields=("service", "date"),
                name="gtfs_unique_service_exception_date",
            ),
        ]

    def __str__(self):
        return f"{self.service_id} on {self.date}"


class FeedInfo(models.Model):
    feed_publisher_name = models.CharField(max_length=255)
    feed_publisher_url = models.URLField(max_length=500)
    feed_lang = models.CharField(max_length=35)
    feed_start_date = models.DateField(null=True, blank=True)
    feed_end_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = "feed information"
        verbose_name_plural = "feed information"

    def __str__(self):
        return self.feed_publisher_name


class Route(models.Model):
    route_id = models.CharField(primary_key=True, max_length=GTFS_ID_MAX_LENGTH)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="routes",
    )
    route_short_name = models.CharField(max_length=255, blank=True)
    route_long_name = models.CharField(max_length=255, blank=True)
    route_desc = models.TextField(blank=True)
    route_type = models.PositiveSmallIntegerField(choices=RouteType)
    route_color = models.CharField(
        max_length=6,
        blank=True,
        validators=(hex_color_validator,),
    )
    route_text_color = models.CharField(
        max_length=6,
        blank=True,
        validators=(hex_color_validator,),
    )

    class Meta:
        ordering = ("route_short_name", "route_id")

    def __str__(self):
        return self.route_short_name or self.route_long_name or self.route_id


class Shape(models.Model):
    shape_id = models.CharField(max_length=GTFS_ID_MAX_LENGTH, db_index=True)
    shape_pt_lat = models.DecimalField(
        max_digits=15,
        decimal_places=12,
        validators=(MinValueValidator(-90), MaxValueValidator(90)),
    )
    shape_pt_lon = models.DecimalField(
        max_digits=15,
        decimal_places=12,
        validators=(MinValueValidator(-180), MaxValueValidator(180)),
    )
    shape_pt_sequence = models.PositiveIntegerField()

    class Meta:
        ordering = ("shape_id", "shape_pt_sequence")
        constraints = [
            models.UniqueConstraint(
                fields=("shape_id", "shape_pt_sequence"),
                name="gtfs_unique_shape_point_sequence",
            ),
        ]

    def __str__(self):
        return f"{self.shape_id} point {self.shape_pt_sequence}"


class Stop(models.Model):
    stop_id = models.CharField(primary_key=True, max_length=GTFS_ID_MAX_LENGTH)
    stop_code = models.CharField(max_length=255, blank=True)
    stop_name = models.CharField(max_length=255)
    stop_lat = models.DecimalField(
        max_digits=15,
        decimal_places=12,
        validators=(MinValueValidator(-90), MaxValueValidator(90)),
    )
    stop_lon = models.DecimalField(
        max_digits=15,
        decimal_places=12,
        validators=(MinValueValidator(-180), MaxValueValidator(180)),
    )
    zone_id = models.CharField(max_length=GTFS_ID_MAX_LENGTH, blank=True)

    class Meta:
        ordering = ("stop_name", "stop_code")

    def __str__(self):
        return self.stop_name


class Trip(models.Model):
    route = models.ForeignKey(
        Route,
        on_delete=models.CASCADE,
        related_name="trips",
    )
    service = models.ForeignKey(
        Calendar,
        on_delete=models.CASCADE,
        related_name="trips",
    )
    trip_id = models.CharField(primary_key=True, max_length=GTFS_ID_MAX_LENGTH)
    trip_headsign = models.CharField(max_length=255, blank=True)
    direction_id = models.PositiveSmallIntegerField(
        choices=((0, "Direction 0"), (1, "Direction 1")),
        null=True,
        blank=True,
    )
    shape_id = models.CharField(
        max_length=GTFS_ID_MAX_LENGTH,
        blank=True,
        db_index=True,
    )
    wheelchair_accessible = models.PositiveSmallIntegerField(
        choices=WheelchairAccessibility,
        default=WheelchairAccessibility.UNKNOWN,
    )
    brigade = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("route_id", "trip_id")

    def __str__(self):
        return self.trip_headsign or self.trip_id


class StopTime(models.Model):
    trip = models.ForeignKey(
        Trip,
        on_delete=models.CASCADE,
        related_name="stop_times",
    )
    # DurationField preserves valid GTFS times at and beyond 24:00:00.
    arrival_time = models.DurationField(null=True, blank=True)
    departure_time = models.DurationField(null=True, blank=True)
    stop = models.ForeignKey(
        Stop,
        on_delete=models.CASCADE,
        related_name="stop_times",
    )
    stop_sequence = models.PositiveIntegerField()
    stop_headsign = models.CharField(max_length=255, blank=True)
    pickup_type = models.PositiveSmallIntegerField(
        choices=PickupDropOffType,
        default=PickupDropOffType.REGULAR,
    )
    drop_off_type = models.PositiveSmallIntegerField(
        choices=PickupDropOffType,
        default=PickupDropOffType.REGULAR,
    )

    class Meta:
        ordering = ("trip_id", "stop_sequence")
        constraints = [
            models.UniqueConstraint(
                fields=("trip", "stop_sequence"),
                name="gtfs_unique_trip_stop_sequence",
            ),
        ]

    def __str__(self):
        return f"{self.trip_id} stop {self.stop_sequence}"
