import logging
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import asin, ceil, cos, floor, radians, sin, sqrt
from threading import RLock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings

from gtfs.models import (
    Agency,
    Calendar,
    CalendarDate,
    ExceptionType,
    GtfsDatasetState,
    PickupDropOffType,
    Stop,
    StopTime,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RoutingEvent:
    stop_id: str
    arrival: int
    departure: int
    pickup_allowed: bool
    drop_off_allowed: bool


@dataclass(frozen=True, slots=True)
class RoutingTrip:
    trip_id: str
    route_id: str
    line_number: str
    line_name: str
    direction: str
    direction_id: int | None
    events: tuple[RoutingEvent, ...]


@dataclass(frozen=True, slots=True)
class RoutingPattern:
    stops: tuple[str, ...]
    trips: tuple[RoutingTrip, ...]
    departures_by_position: tuple[tuple[int, ...], ...]


@dataclass(frozen=True, slots=True)
class Footpath:
    to_stop_id: str
    distance_meters: int
    duration: int


@dataclass(frozen=True, slots=True)
class RoutingSnapshot:
    revision: int
    service_date: date
    patterns: tuple[RoutingPattern, ...]
    patterns_by_stop: dict[str, tuple[tuple[int, int], ...]]
    footpaths: dict[str, tuple[Footpath, ...]]
    stop_ids: frozenset[str]
    departure_times_by_stop: dict[str, tuple[int, ...]]


_snapshot_cache: OrderedDict[
    tuple[int, date, int, float],
    RoutingSnapshot,
] = OrderedDict()
_snapshot_lock = RLock()


def default_service_date() -> date:
    """Return today in the feed timezone used for GTFS service times."""
    timezones = tuple(
        Agency.objects.order_by('agency_timezone')
        .values_list('agency_timezone', flat=True)
        .distinct()
    )
    if len(timezones) > 1:
        raise ValueError(
            'service_date is required when agencies use multiple timezones'
        )
    timezone_name = timezones[0] if timezones else settings.TIME_ZONE
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(
            f'Unknown agency timezone: {timezone_name}'
        ) from error
    return datetime.now(timezone).date()


def clear_routing_snapshot_cache() -> None:
    with _snapshot_lock:
        _snapshot_cache.clear()


def get_routing_snapshot(service_date: date) -> RoutingSnapshot:
    revision = (
        GtfsDatasetState.objects.filter(singleton_id=1)
        .values_list('revision', flat=True)
        .first()
        or 0
    )
    radius = settings.ROUTE_MAX_WALK_DISTANCE_METERS
    speed = settings.ROUTE_WALK_SPEED_METERS_PER_SECOND
    cache_key = (revision, service_date, radius, speed)

    # Revision zero represents fixtures or a database not populated by the
    # importer. Rebuild those snapshots so transactional tests and manual data
    # edits never observe a stale process cache.
    if revision == 0:
        return _build_snapshot(revision, service_date, radius, speed)

    with _snapshot_lock:
        snapshot = _snapshot_cache.get(cache_key)
        if snapshot is not None:
            _snapshot_cache.move_to_end(cache_key)
            return snapshot

        snapshot = _build_snapshot(revision, service_date, radius, speed)
        _snapshot_cache[cache_key] = snapshot
        _snapshot_cache.move_to_end(cache_key)
        while len(_snapshot_cache) > settings.ROUTE_SNAPSHOT_CACHE_SIZE:
            _snapshot_cache.popitem(last=False)
        return snapshot


def _active_service_ids(service_date: date) -> set[str]:
    weekday_field = service_date.strftime('%A').lower()
    active = {
        service_id
        for service_id, operates, start_date, end_date in Calendar.objects.values_list(
            'service_id',
            weekday_field,
            'start_date',
            'end_date',
        )
        if operates and start_date <= service_date <= end_date
    }
    for service_id, exception_type in CalendarDate.objects.filter(
        date=service_date,
    ).values_list('service_id', 'exception_type'):
        if exception_type == ExceptionType.ADDED:
            active.add(service_id)
        elif exception_type == ExceptionType.REMOVED:
            active.discard(service_id)
    return active


def _seconds(value: timedelta | None) -> int | None:
    if value is None:
        return None
    return int(value.total_seconds())


def _build_snapshot(
    revision: int,
    service_date: date,
    radius: int,
    speed: float,
) -> RoutingSnapshot:
    if radius < 0:
        raise ValueError('ROUTE_MAX_WALK_DISTANCE_METERS must not be negative')
    if speed <= 0:
        raise ValueError('ROUTE_WALK_SPEED_METERS_PER_SECOND must be positive')

    active_services = _active_service_ids(service_date)
    trips: dict[str, list[RoutingEvent]] = defaultdict(list)
    trip_metadata: dict[
        str,
        tuple[str, str, str, str, int | None],
    ] = {}
    stop_times = (
        StopTime.objects.filter(trip__service_id__in=active_services)
        .order_by('trip_id', 'stop_sequence')
        .values_list(
            'trip_id',
            'trip__route_id',
            'trip__route__route_short_name',
            'trip__route__route_long_name',
            'trip__trip_headsign',
            'trip__direction_id',
            'stop_id',
            'arrival_time',
            'departure_time',
            'pickup_type',
            'drop_off_type',
        )
    )
    for (
        trip_id,
        route_id,
        line_number,
        line_name,
        direction,
        direction_id,
        stop_id,
        arrival_time,
        departure_time,
        pickup_type,
        drop_off_type,
    ) in stop_times:
        arrival = _seconds(arrival_time)
        departure = _seconds(departure_time)
        if arrival is None and departure is None:
            continue
        arrival = departure if arrival is None else arrival
        departure = arrival if departure is None else departure
        if arrival is None or departure is None:
            continue
        trip_metadata[trip_id] = (
            route_id,
            line_number,
            line_name,
            direction,
            direction_id,
        )
        trips[trip_id].append(
            RoutingEvent(
                stop_id=stop_id,
                arrival=arrival,
                departure=departure,
                pickup_allowed=pickup_type != PickupDropOffType.NONE,
                drop_off_allowed=drop_off_type != PickupDropOffType.NONE,
            )
        )

    grouped: dict[
        tuple[str, tuple[str, ...], tuple[bool, ...], tuple[bool, ...]],
        list[RoutingTrip],
    ] = defaultdict(list)
    for trip_id, events_list in trips.items():
        if len(events_list) < 2:
            continue
        metadata = trip_metadata[trip_id]
        events = tuple(events_list)
        trip = RoutingTrip(
            trip_id=trip_id,
            route_id=metadata[0],
            line_number=metadata[1],
            line_name=metadata[2],
            direction=metadata[3],
            direction_id=metadata[4],
            events=events,
        )
        grouped[
            (
                trip.route_id,
                tuple(event.stop_id for event in events),
                tuple(event.pickup_allowed for event in events),
                tuple(event.drop_off_allowed for event in events),
            )
        ].append(trip)

    patterns = []
    for key, pattern_trips in grouped.items():
        for partition in _partition_non_overtaking(pattern_trips):
            ordered_trips = tuple(partition)
            patterns.append(
                RoutingPattern(
                    stops=key[1],
                    trips=ordered_trips,
                    departures_by_position=tuple(
                        tuple(trip.events[index].departure for trip in ordered_trips)
                        for index in range(len(key[1]))
                    ),
                )
            )
    patterns.sort(
        key=lambda pattern: (
            pattern.trips[0].route_id,
            pattern.stops,
            pattern.trips[0].trip_id,
        )
    )

    patterns_by_stop_lists: dict[str, list[tuple[int, int]]] = defaultdict(list)
    departure_times: dict[str, list[int]] = defaultdict(list)
    for pattern_index, pattern in enumerate(patterns):
        for position, stop_id in enumerate(pattern.stops):
            patterns_by_stop_lists[stop_id].append((pattern_index, position))
            for trip in pattern.trips:
                event = trip.events[position]
                if event.pickup_allowed and position < len(pattern.stops) - 1:
                    departure_times[stop_id].append(event.departure)

    coordinates = {
        stop_id: (float(latitude), float(longitude))
        for stop_id, latitude, longitude in Stop.objects.values_list(
            'stop_id',
            'stop_lat',
            'stop_lon',
        )
    }
    footpaths = _build_footpaths(coordinates, radius, speed)
    snapshot = RoutingSnapshot(
        revision=revision,
        service_date=service_date,
        patterns=tuple(patterns),
        patterns_by_stop={
            stop_id: tuple(entries)
            for stop_id, entries in patterns_by_stop_lists.items()
        },
        footpaths=footpaths,
        stop_ids=frozenset(coordinates),
        departure_times_by_stop={
            stop_id: tuple(sorted(set(times)))
            for stop_id, times in departure_times.items()
        },
    )
    logger.info(
        'Built routing snapshot: revision=%d, service_date=%s, trips=%d, '
        'patterns=%d, stops=%d, footpaths=%d',
        revision,
        service_date,
        sum(len(pattern.trips) for pattern in snapshot.patterns),
        len(snapshot.patterns),
        len(snapshot.stop_ids),
        sum(len(paths) for paths in snapshot.footpaths.values()),
    )
    return snapshot


def _partition_non_overtaking(
    trips: list[RoutingTrip],
) -> list[list[RoutingTrip]]:
    ordered = sorted(
        trips,
        key=lambda trip: (
            trip.events[0].departure,
            tuple(event.arrival for event in trip.events),
            trip.trip_id,
        ),
    )
    partitions: list[list[RoutingTrip]] = []
    for trip in ordered:
        compatible = [
            partition
            for partition in partitions
            if all(
                previous.arrival <= current.arrival
                and previous.departure <= current.departure
                for previous, current in zip(partition[-1].events, trip.events)
            )
        ]
        if compatible:
            partition = max(
                compatible,
                key=lambda candidate: candidate[-1].events[0].departure,
            )
            partition.append(trip)
        else:
            partitions.append([trip])
    return partitions


def _build_footpaths(
    coordinates: dict[str, tuple[float, float]],
    radius: int,
    speed: float,
) -> dict[str, tuple[Footpath, ...]]:
    if radius == 0 or not coordinates:
        return {}
    mean_latitude = sum(value[0] for value in coordinates.values()) / len(coordinates)
    longitude_scale = max(0.01, cos(radians(mean_latitude))) * 111_320
    latitude_scale = 111_320
    grid: dict[tuple[int, int], list[str]] = defaultdict(list)
    projected = {}
    for stop_id, (latitude, longitude) in coordinates.items():
        x = longitude * longitude_scale
        y = latitude * latitude_scale
        projected[stop_id] = (x, y)
        grid[(floor(x / radius), floor(y / radius))].append(stop_id)

    result: dict[str, list[Footpath]] = defaultdict(list)
    for stop_id in sorted(coordinates):
        x, y = projected[stop_id]
        cell_x, cell_y = floor(x / radius), floor(y / radius)
        for x_delta in (-1, 0, 1):
            for y_delta in (-1, 0, 1):
                for other_id in grid.get((cell_x + x_delta, cell_y + y_delta), ()):
                    if other_id <= stop_id:
                        continue
                    distance = great_circle_distance(
                        coordinates[stop_id],
                        coordinates[other_id],
                    )
                    if distance > radius:
                        continue
                    rounded_distance = int(round(distance))
                    duration = max(1, ceil(distance / speed))
                    result[stop_id].append(
                        Footpath(other_id, rounded_distance, duration)
                    )
                    result[other_id].append(
                        Footpath(stop_id, rounded_distance, duration)
                    )
    return {
        stop_id: tuple(sorted(paths, key=lambda path: (path.duration, path.to_stop_id)))
        for stop_id, paths in result.items()
    }


def great_circle_distance(
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> float:
    origin_latitude, origin_longitude = map(radians, origin)
    destination_latitude, destination_longitude = map(radians, destination)
    latitude_delta = destination_latitude - origin_latitude
    longitude_delta = destination_longitude - origin_longitude
    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(origin_latitude)
        * cos(destination_latitude)
        * sin(longitude_delta / 2) ** 2
    )
    return 12_742_000 * asin(min(1.0, sqrt(haversine)))
