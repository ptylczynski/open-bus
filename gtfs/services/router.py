import logging
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from math import ceil
from time import perf_counter

from django.conf import settings

from gtfs.models import Stop
from gtfs.services.routing_data import (
    Footpath,
    RoutingPattern,
    RoutingSnapshot,
    default_service_date,
    great_circle_distance,
    get_routing_snapshot,
)


logger = logging.getLogger(__name__)
COORDINATE_ENDPOINT_MAX_WALK_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class RouteLeg:
    trip_id: str
    route_id: str
    line_number: str
    line_name: str
    direction: str
    direction_id: int | None
    from_stop_id: str
    to_stop_id: str
    stop_ids: tuple[str, ...]
    departure_time: timedelta
    arrival_time: timedelta


@dataclass(frozen=True, slots=True)
class WalkLeg:
    from_stop_id: str
    to_stop_id: str
    departure_time: timedelta
    arrival_time: timedelta
    distance_meters: int

    @property
    def duration(self) -> timedelta:
        return self.arrival_time - self.departure_time


RouteSegment = RouteLeg | WalkLeg
Coordinates = tuple[float, float]


@dataclass(frozen=True, slots=True)
class RouteOption:
    stop_ids: tuple[str, ...]
    hops: int
    arrival_time: timedelta
    legs: tuple[RouteLeg, ...] = ()
    segments: tuple[RouteSegment, ...] = ()
    departure_time: timedelta | None = None
    walking_time: timedelta = timedelta(0)

    @property
    def duration(self) -> timedelta:
        departure = self.departure_time or self.arrival_time
        return self.arrival_time - departure


@dataclass(frozen=True, slots=True)
class _Label:
    stop_id: str
    time: int
    last_transit_arrival: int | None
    boardings: int
    walking_seconds: int
    stop_ids: tuple[str, ...]
    trip_ids: tuple[str, ...]
    legs: tuple[RouteLeg, ...]
    segments: tuple[RouteSegment, ...]
    signature: tuple[tuple[object, ...], ...]
    walked_since_transit: bool


class RouteSelectionService:
    """Find bounded Pareto journey alternatives using RAPTOR-style rounds."""

    def __init__(
        self,
        max_hops: int | None = None,
        max_workers: int | None = None,
        max_alternatives_per_hop: int | None = None,
        max_routes: int | None = None,
    ) -> None:
        self.max_hops = (
            settings.ROUTE_MAX_HOPS if max_hops is None else max_hops
        )
        # Kept as a compatibility parameter. Python route scans are deliberately
        # single-threaded; shared immutable snapshots provide the useful speedup.
        self.max_workers = (
            settings.ROUTE_CALCULATION_WORKERS
            if max_workers is None
            else max_workers
        )
        self.max_alternatives_per_hop = (
            settings.ROUTE_MAX_ALTERNATIVES_PER_HOP
            if max_alternatives_per_hop is None
            else max_alternatives_per_hop
        )
        self.max_routes = (
            settings.ROUTE_MAX_ROUTES if max_routes is None else max_routes
        )
        self.max_labels_per_stop = settings.ROUTE_MAX_LABELS_PER_STOP
        self.max_coordinate_stops = settings.ROUTE_MAX_COORDINATE_STOPS
        self.service_date: date | None = None
        self._min_exchange_seconds = 0
        self._max_exchange_seconds = 0
        self._latest_useful_arrival: int | None = None

        if self.max_hops < 0:
            raise ValueError('ROUTE_MAX_HOPS must be zero or greater')
        if self.max_workers < 1:
            raise ValueError('ROUTE_CALCULATION_WORKERS must be at least one')
        if self.max_alternatives_per_hop < 1:
            raise ValueError(
                'ROUTE_MAX_ALTERNATIVES_PER_HOP must be at least one'
            )
        if self.max_routes < 1:
            raise ValueError('ROUTE_MAX_ROUTES must be at least one')
        if self.max_labels_per_stop < 1:
            raise ValueError('ROUTE_MAX_LABELS_PER_STOP must be at least one')
        if self.max_coordinate_stops < 1:
            raise ValueError(
                'ROUTE_MAX_COORDINATE_STOPS must be at least one'
            )

    def find_route(
        self,
        from_stop_id: str,
        to_stop_id: str,
        departure_time: timedelta,
        min_exchange_time: timedelta | None = None,
        max_exchange_time: timedelta | None = None,
        service_date: date | None = None,
        from_coordinates: Coordinates | None = None,
        to_coordinates: Coordinates | None = None,
    ) -> list[str] | None:
        routes = self.find_routes(
            from_stop_id,
            to_stop_id,
            departure_time,
            min_exchange_time,
            max_exchange_time,
            service_date,
            from_coordinates=from_coordinates,
            to_coordinates=to_coordinates,
        )
        if not routes:
            return None
        return list(routes[0].stop_ids)

    def find_routes(
        self,
        from_stop_id: str,
        to_stop_id: str,
        departure_time: timedelta,
        min_exchange_time: timedelta | None = None,
        max_exchange_time: timedelta | None = None,
        service_date: date | None = None,
        from_coordinates: Coordinates | None = None,
        to_coordinates: Coordinates | None = None,
    ) -> list[RouteOption]:
        started_at = perf_counter()
        departure_seconds = int(departure_time.total_seconds())
        if departure_seconds < 0:
            raise ValueError('departure_time must be zero or greater')
        self.service_date = service_date or default_service_date()
        self._configure_exchange_times(min_exchange_time, max_exchange_time)

        logger.info(
            'Starting RAPTOR route search: from_stop=%s, to_stop=%s, '
            'service_date=%s, departure=%s, max_hops=%d, '
            'alternatives_per_hop=%d, max_routes=%d',
            from_stop_id,
            to_stop_id,
            self.service_date,
            departure_time,
            self.max_hops,
            self.max_alternatives_per_hop,
            self.max_routes,
        )
        from_stop_ids = (
            (from_stop_id,)
            if from_coordinates is not None
            else self._same_name_stop_ids(from_stop_id)
        )
        to_stop_ids = (
            (to_stop_id,)
            if to_coordinates is not None
            else self._same_name_stop_ids(to_stop_id)
        )
        common_stop_ids = set(from_stop_ids) & set(to_stop_ids)
        if common_stop_ids:
            reached_stop_id = (
                from_stop_id
                if from_stop_id in common_stop_ids
                else min(common_stop_ids)
            )
            return [
                RouteOption(
                    stop_ids=(reached_stop_id,),
                    hops=0,
                    departure_time=departure_time,
                    arrival_time=departure_time,
                )
            ]

        snapshot = self._with_coordinate_endpoints(
            get_routing_snapshot(self.service_date),
            from_stop_id,
            from_coordinates,
            to_stop_id,
            to_coordinates,
        )
        from_stop_ids = tuple(
            stop_id for stop_id in from_stop_ids if stop_id in snapshot.stop_ids
        )
        to_stop_ids = tuple(
            stop_id for stop_id in to_stop_ids if stop_id in snapshot.stop_ids
        )
        if not from_stop_ids or not to_stop_ids:
            return []
        to_stop_id_set = set(to_stop_ids)

        quick_bounds = self._find_quick_bounds(
            snapshot,
            from_stop_ids,
            to_stop_id_set,
            departure_seconds,
        )

        initial_labels = [
            _Label(
                stop_id=starting_stop_id,
                time=departure_seconds,
                last_transit_arrival=None,
                boardings=0,
                walking_seconds=0,
                stop_ids=(starting_stop_id,),
                trip_ids=(),
                legs=(),
                segments=(),
                signature=(),
                walked_since_transit=False,
            )
            for starting_stop_id in from_stop_ids
        ]
        previous_round = self._group_and_prune(
            [
                candidate
                for initial in initial_labels
                for candidate in (
                    initial,
                    *self._walk_from_label(initial, snapshot, initial_walk=True),
                )
            ]
        )
        destination_labels = [
            label
            for destination_stop_id in to_stop_ids
            for label in previous_round.get(destination_stop_id, ())
        ]

        for round_number in range(1, self.max_hops + 2):
            quick_bound = quick_bounds.get(round_number)
            self._latest_useful_arrival = (
                None
                if quick_bound is None
                else self._useful_arrival_limit(
                    quick_bound,
                    departure_seconds,
                )
            )
            logger.info(
                'Searching RAPTOR round %d: marked_stops=%d, labels=%d',
                round_number,
                len(previous_round),
                sum(len(labels) for labels in previous_round.values()),
            )
            transit_candidates: list[_Label] = []
            for stop_id, labels in previous_round.items():
                for pattern_index, position in snapshot.patterns_by_stop.get(
                    stop_id,
                    (),
                ):
                    pattern = snapshot.patterns[pattern_index]
                    for label in labels:
                        transit_candidates.extend(
                            self._ride_pattern(label, pattern, position)
                        )

            transit_round = self._group_and_prune(transit_candidates)
            if not transit_round:
                break
            next_candidates = [
                label
                for labels in transit_round.values()
                for label in labels
            ]
            for label in tuple(next_candidates):
                next_candidates.extend(self._walk_from_label(label, snapshot))
            destination_labels.extend(
                label
                for label in next_candidates
                if label.stop_id in to_stop_id_set
            )
            next_candidates = self._within_destination_bound(
                next_candidates,
                destination_labels,
                departure_seconds,
            )
            previous_round = self._group_and_prune(next_candidates)

        routes = self._select_routes(destination_labels, departure_seconds)
        logger.info(
            'RAPTOR route search complete: from_stop=%s, to_stop=%s, '
            'routes=%d, elapsed=%.3fs',
            from_stop_id,
            to_stop_id,
            len(routes),
            perf_counter() - started_at,
        )
        return routes

    def _with_coordinate_endpoints(
        self,
        snapshot: RoutingSnapshot,
        from_stop_id: str,
        from_coordinates: Coordinates | None,
        to_stop_id: str,
        to_coordinates: Coordinates | None,
    ) -> RoutingSnapshot:
        if from_coordinates is None and to_coordinates is None:
            return snapshot

        stop_coordinates = {
            stop_id: (float(latitude), float(longitude))
            for stop_id, latitude, longitude in Stop.objects.filter(
                stop_id__in=snapshot.stop_ids,
            ).values_list('stop_id', 'stop_lat', 'stop_lon')
        }
        footpaths = {
            stop_id: list(paths)
            for stop_id, paths in snapshot.footpaths.items()
        }
        stop_ids = set(snapshot.stop_ids)

        if from_coordinates is not None:
            stop_ids.add(from_stop_id)
            footpaths[from_stop_id] = self._endpoint_footpaths(
                from_coordinates,
                stop_coordinates,
            )
        if to_coordinates is not None:
            stop_ids.add(to_stop_id)
            for path in self._endpoint_footpaths(
                to_coordinates,
                stop_coordinates,
            ):
                footpaths.setdefault(path.to_stop_id, []).append(
                    Footpath(
                        to_stop_id=to_stop_id,
                        distance_meters=path.distance_meters,
                        duration=path.duration,
                    )
                )
        if from_coordinates is not None and to_coordinates is not None:
            direct_distance = great_circle_distance(
                from_coordinates,
                to_coordinates,
            )
            direct_path = self._footpath(to_stop_id, direct_distance)
            if direct_path.duration <= COORDINATE_ENDPOINT_MAX_WALK_SECONDS:
                footpaths[from_stop_id].append(
                    direct_path,
                )

        return replace(
            snapshot,
            footpaths={
                stop_id: tuple(
                    sorted(
                        paths,
                        key=lambda path: (path.duration, path.to_stop_id),
                    ),
                )
                for stop_id, paths in footpaths.items()
            },
            stop_ids=frozenset(stop_ids),
        )

    def _endpoint_footpaths(
        self,
        endpoint: Coordinates,
        stop_coordinates: dict[str, Coordinates],
    ) -> list[Footpath]:
        paths = []
        for stop_id, coordinates in stop_coordinates.items():
            distance = great_circle_distance(endpoint, coordinates)
            path = self._footpath(stop_id, distance)
            if path.duration <= COORDINATE_ENDPOINT_MAX_WALK_SECONDS:
                paths.append((distance, path))
        return [
            path
            for _, path in sorted(
                paths,
                key=lambda item: (item[0], item[1].to_stop_id),
            )[:self.max_coordinate_stops]
        ]

    @staticmethod
    def _footpath(stop_id: str, distance: float) -> Footpath:
        return Footpath(
            to_stop_id=stop_id,
            distance_meters=int(round(distance)),
            duration=max(
                1,
                ceil(distance / settings.ROUTE_WALK_SPEED_METERS_PER_SECOND),
            ),
        )

    def _find_quick_bounds(
        self,
        snapshot: RoutingSnapshot,
        from_stop_ids: tuple[str, ...],
        to_stop_ids: set[str],
        departure: int,
    ) -> dict[int, int]:
        previous: dict[str, tuple[int, int | None]] = {
            stop_id: (departure, None) for stop_id in from_stop_ids
        }
        for from_stop_id in from_stop_ids:
            for footpath in snapshot.footpaths.get(from_stop_id, ()):
                walked_arrival = departure + footpath.duration
                current = previous.get(footpath.to_stop_id)
                if current is None or walked_arrival < current[0]:
                    previous[footpath.to_stop_id] = (walked_arrival, None)
        destination_arrivals = [
            ready_time
            for stop_id, (ready_time, _) in previous.items()
            if stop_id in to_stop_ids
        ]
        bounds = {}
        initial_bound = min(destination_arrivals, default=None)
        if initial_bound is not None:
            bounds[0] = initial_bound

        for round_number in range(1, self.max_hops + 2):
            transit: dict[str, tuple[int, int]] = {}
            for stop_id, (ready_time, last_arrival) in previous.items():
                earliest = ready_time
                latest = None
                if round_number > 1:
                    if last_arrival is None:
                        continue
                    earliest += self._min_exchange_seconds
                    latest = last_arrival + self._max_exchange_seconds
                for pattern_index, position in snapshot.patterns_by_stop.get(
                    stop_id,
                    (),
                ):
                    pattern = snapshot.patterns[pattern_index]
                    if position >= len(pattern.stops) - 1:
                        continue
                    departures = pattern.departures_by_position[position]
                    trip_index = bisect_left(departures, earliest)
                    while trip_index < len(pattern.trips):
                        trip = pattern.trips[trip_index]
                        boarding = trip.events[position]
                        if latest is not None and boarding.departure > latest:
                            break
                        if boarding.pickup_allowed:
                            for event in trip.events[position + 1:]:
                                if not event.drop_off_allowed:
                                    continue
                                current = transit.get(event.stop_id)
                                if current is None or event.arrival < current[0]:
                                    transit[event.stop_id] = (
                                        event.arrival,
                                        event.arrival,
                                    )
                            break
                        trip_index += 1
            if not transit:
                break

            next_round: dict[str, tuple[int, int | None]] = dict(transit)
            for stop_id, (arrival, last_arrival) in transit.items():
                for footpath in snapshot.footpaths.get(stop_id, ()):
                    walked_arrival = arrival + footpath.duration
                    current = next_round.get(footpath.to_stop_id)
                    if current is None or walked_arrival < current[0]:
                        next_round[footpath.to_stop_id] = (
                            walked_arrival,
                            last_arrival,
                        )
            destination_arrivals = [
                ready_time
                for stop_id, (ready_time, _) in next_round.items()
                if stop_id in to_stop_ids
            ]
            if destination_arrivals:
                bounds[round_number] = min(destination_arrivals)
            previous = next_round
        return bounds

    @staticmethod
    def _same_name_stop_ids(stop_id: str) -> tuple[str, ...]:
        stop_name = (
            Stop.objects.filter(stop_id=stop_id)
            .values_list('stop_name', flat=True)
            .first()
        )
        if stop_name is None:
            return ()
        return tuple(
            Stop.objects.filter(stop_name=stop_name)
            .order_by('stop_id')
            .values_list('stop_id', flat=True)
        )

    @staticmethod
    def _useful_arrival_limit(
        best_arrival: int,
        requested_departure: int,
    ) -> int:
        return min(
            best_arrival + settings.ROUTE_MAX_EXTRA_TRAVEL_SECONDS,
            requested_departure
            + int(
                (best_arrival - requested_departure)
                * settings.ROUTE_MAX_EXTRA_TRAVEL_RATIO
            ),
        )

    @staticmethod
    def _within_destination_bound(
        labels: list[_Label],
        destination_labels: list[_Label],
        requested_departure: int,
    ) -> list[_Label]:
        if not destination_labels:
            return labels
        best_arrival = min(label.time for label in destination_labels)
        latest_useful_arrival = RouteSelectionService._useful_arrival_limit(
            best_arrival,
            requested_departure,
        )
        return [
            label for label in labels if label.time <= latest_useful_arrival
        ]

    def _configure_exchange_times(
        self,
        minimum: timedelta | None,
        maximum: timedelta | None,
    ) -> None:
        minimum = (
            timedelta(seconds=settings.ROUTE_MIN_EXCHANGE_TIME_SECONDS)
            if minimum is None
            else minimum
        )
        maximum = (
            timedelta(seconds=settings.ROUTE_MAX_EXCHANGE_TIME_SECONDS)
            if maximum is None
            else maximum
        )
        if minimum < timedelta(0):
            raise ValueError('min_exchange_time must be zero or greater')
        if maximum < timedelta(0):
            raise ValueError('max_exchange_time must be zero or greater')
        if minimum > maximum:
            raise ValueError(
                'min_exchange_time must not exceed max_exchange_time'
            )
        self._min_exchange_seconds = int(minimum.total_seconds())
        self._max_exchange_seconds = int(maximum.total_seconds())

    def _ride_pattern(
        self,
        label: _Label,
        pattern: RoutingPattern,
        position: int,
    ) -> list[_Label]:
        if position >= len(pattern.stops) - 1:
            return []
        earliest = label.time
        latest = None
        if label.boardings:
            earliest += self._min_exchange_seconds
            if label.last_transit_arrival is None:
                return []
            latest = label.last_transit_arrival + self._max_exchange_seconds
            if earliest > latest:
                return []

        departures = pattern.departures_by_position[position]
        first_trip = bisect_left(departures, earliest)
        final_trip = (
            len(departures)
            if latest is None
            else bisect_right(departures, latest)
        )
        if self._latest_useful_arrival is not None:
            final_trip = min(
                final_trip,
                bisect_right(departures, self._latest_useful_arrival),
            )
        candidates = []
        visited_stops = set(label.stop_ids)
        for trip_index in range(first_trip, final_trip):
            trip = pattern.trips[trip_index]
            boarding = trip.events[position]
            if not boarding.pickup_allowed or trip.trip_id in label.trip_ids:
                continue
            for destination_position in range(position + 1, len(trip.events)):
                destination = trip.events[destination_position]
                if (
                    self._latest_useful_arrival is not None
                    and destination.arrival > self._latest_useful_arrival
                ):
                    continue
                if destination.stop_id in visited_stops:
                    break
                if not destination.drop_off_allowed:
                    continue
                segment_stops = pattern.stops[position:destination_position + 1]
                leg = RouteLeg(
                    trip_id=trip.trip_id,
                    route_id=trip.route_id,
                    line_number=trip.line_number,
                    line_name=trip.line_name,
                    direction=trip.direction,
                    direction_id=trip.direction_id,
                    from_stop_id=label.stop_id,
                    to_stop_id=destination.stop_id,
                    stop_ids=segment_stops,
                    departure_time=timedelta(seconds=boarding.departure),
                    arrival_time=timedelta(seconds=destination.arrival),
                )
                candidates.append(
                    _Label(
                        stop_id=destination.stop_id,
                        time=destination.arrival,
                        last_transit_arrival=destination.arrival,
                        boardings=label.boardings + 1,
                        walking_seconds=label.walking_seconds,
                        stop_ids=label.stop_ids + segment_stops[1:],
                        trip_ids=label.trip_ids + (trip.trip_id,),
                        legs=label.legs + (leg,),
                        segments=label.segments + (leg,),
                        signature=label.signature
                        + (
                            (
                                'transit',
                                trip.route_id,
                                label.stop_id,
                                destination.stop_id,
                            ),
                        ),
                        walked_since_transit=False,
                    )
                )
        return candidates

    def _walk_from_label(
        self,
        label: _Label,
        snapshot: RoutingSnapshot,
        initial_walk: bool = False,
    ) -> list[_Label]:
        if label.walked_since_transit:
            return []
        walked = []
        for footpath in snapshot.footpaths.get(label.stop_id, ()):
            if footpath.to_stop_id in label.stop_ids:
                continue
            walked.append(self._apply_footpath(label, footpath, initial_walk))
        return walked

    @staticmethod
    def _apply_footpath(
        label: _Label,
        footpath: Footpath,
        initial_walk: bool,
    ) -> _Label:
        walk = WalkLeg(
            from_stop_id=label.stop_id,
            to_stop_id=footpath.to_stop_id,
            departure_time=timedelta(seconds=label.time),
            arrival_time=timedelta(seconds=label.time + footpath.duration),
            distance_meters=footpath.distance_meters,
        )
        return _Label(
            stop_id=footpath.to_stop_id,
            time=label.time + footpath.duration,
            last_transit_arrival=(
                None if initial_walk else label.last_transit_arrival
            ),
            boardings=label.boardings,
            walking_seconds=label.walking_seconds + footpath.duration,
            stop_ids=label.stop_ids + (footpath.to_stop_id,),
            trip_ids=label.trip_ids,
            legs=label.legs,
            segments=label.segments + (walk,),
            signature=label.signature
            + (('walk', label.stop_id, footpath.to_stop_id),),
            walked_since_transit=True,
        )

    def _group_and_prune(
        self,
        labels: list[_Label],
    ) -> dict[str, tuple[_Label, ...]]:
        grouped: dict[str, list[_Label]] = defaultdict(list)
        for label in labels:
            grouped[label.stop_id].append(label)
        return {
            stop_id: self._prune_labels(stop_labels)
            for stop_id, stop_labels in grouped.items()
        }

    def _prune_labels(self, labels: list[_Label]) -> tuple[_Label, ...]:
        unique = {}
        for label in labels:
            key = (
                label.time,
                label.last_transit_arrival,
                label.boardings,
                label.walking_seconds,
            )
            existing = unique.get(key)
            if existing is None or (
                label.signature,
                label.stop_ids,
            ) < (
                existing.signature,
                existing.stop_ids,
            ):
                unique[key] = label

        ordered = sorted(
            unique.values(),
            key=lambda label: (
                label.time,
                label.walking_seconds,
                self._signature(label),
                label.trip_ids,
            ),
        )
        retained = []
        for candidate in ordered:
            if any(
                self._continuation_dominates(item, candidate)
                for item in retained
            ):
                continue
            retained.append(candidate)
        if len(retained) <= self.max_labels_per_stop:
            return tuple(retained)

        # Preserve early arrivals and later boarding windows. Later labels can
        # be essential when max_exchange_time is a hard constraint.
        early_count = (self.max_labels_per_stop + 1) // 2
        selected = retained[:early_count]
        for label in sorted(
            retained[early_count:],
            key=lambda item: (
                -(item.last_transit_arrival or item.time),
                item.walking_seconds,
                self._signature(item),
            ),
        ):
            if label not in selected:
                selected.append(label)
            if len(selected) == self.max_labels_per_stop:
                break
        return tuple(
            sorted(
                selected,
                key=lambda item: (item.time, self._signature(item)),
            )
        )

    def _continuation_dominates(self, first: _Label, second: _Label) -> bool:
        if first.walking_seconds > second.walking_seconds:
            return False
        if first.time > second.time:
            return False
        if first.boardings == 0:
            return True
        if first.last_transit_arrival is None or second.last_transit_arrival is None:
            return False
        first_latest = first.last_transit_arrival + self._max_exchange_seconds
        second_latest = second.last_transit_arrival + self._max_exchange_seconds
        return first_latest >= second_latest

    def _select_routes(
        self,
        labels: list[_Label],
        requested_departure: int,
    ) -> list[RouteOption]:
        labels = self._collapse_terminal_walk_alternatives(labels)
        distinct = {}
        for label in labels:
            signature = self._signature(label)
            existing = distinct.get(signature)
            if existing is None or (
                label.time,
                label.walking_seconds,
                label.stop_ids,
            ) < (
                existing.time,
                existing.walking_seconds,
                existing.stop_ids,
            ):
                distinct[signature] = label
        candidates = list(distinct.values())
        best_by_hop = {}
        for label in candidates:
            hops = max(0, label.boardings - 1)
            best_by_hop[hops] = min(
                best_by_hop.get(hops, label.time),
                label.time,
            )

        useful = []
        for label in candidates:
            hops = max(0, label.boardings - 1)
            best_arrival = best_by_hop[hops]
            best_duration = max(0, best_arrival - requested_departure)
            duration = max(0, label.time - requested_departure)
            if (
                label.time
                > best_arrival + settings.ROUTE_MAX_EXTRA_TRAVEL_SECONDS
            ):
                continue
            if (
                best_duration
                and duration
                > best_duration * settings.ROUTE_MAX_EXTRA_TRAVEL_RATIO
            ):
                continue
            useful.append(label)

        pareto_signatures = {
            self._signature(candidate)
            for candidate in useful
            if not any(
                self._route_dominates(other, candidate)
                for other in useful
                if other is not candidate
            )
        }
        useful.sort(
            key=lambda label: (
                self._signature(label) not in pareto_signatures,
                max(0, label.boardings - 1),
                label.walking_seconds,
                label.time,
                self._signature(label),
            )
        )

        routes = []
        per_hop: dict[int, int] = defaultdict(int)
        used_from_stops = set()
        used_to_stops = set()
        used_endpoint_pairs = set()
        remaining = list(useful)
        while remaining:
            eligible = [
                (index, label)
                for index, label in enumerate(remaining)
                if per_hop[max(0, label.boardings - 1)]
                < self.max_alternatives_per_hop
            ]
            if not eligible:
                break
            selected_index, label = min(
                eligible,
                key=lambda item: self._diversity_preference(
                    item[1],
                    used_from_stops,
                    used_to_stops,
                    used_endpoint_pairs,
                    item[0],
                ),
            )
            remaining.pop(selected_index)
            hops = max(0, label.boardings - 1)
            departure = (
                label.segments[0].departure_time
                if label.segments
                else timedelta(seconds=requested_departure)
            )
            routes.append(
                RouteOption(
                    stop_ids=label.stop_ids,
                    hops=hops,
                    departure_time=departure,
                    arrival_time=timedelta(seconds=label.time),
                    walking_time=timedelta(seconds=label.walking_seconds),
                    legs=label.legs,
                    segments=label.segments,
                )
            )
            from_stop_id, to_stop_id = self._route_endpoint_pair(label)
            used_from_stops.add(from_stop_id)
            used_to_stops.add(to_stop_id)
            used_endpoint_pairs.add((from_stop_id, to_stop_id))
            per_hop[hops] += 1
            if len(routes) == self.max_routes:
                break
        return routes

    @staticmethod
    def _diversity_preference(
        label: _Label,
        used_from_stops: set[str],
        used_to_stops: set[str],
        used_endpoint_pairs: set[tuple[str, str]],
        quality_index: int,
    ) -> tuple[int, int, int, bool, int]:
        from_stop_id, to_stop_id = RouteSelectionService._route_endpoint_pair(
            label,
        )
        new_endpoint_count = (
            int(from_stop_id not in used_from_stops)
            + int(to_stop_id not in used_to_stops)
        )
        return (
            max(0, label.boardings - 1),
            label.walking_seconds,
            -new_endpoint_count,
            (from_stop_id, to_stop_id) in used_endpoint_pairs,
            quality_index,
        )

    @staticmethod
    def _route_endpoint_pair(label: _Label) -> tuple[str, str]:
        if label.legs:
            return (
                label.legs[0].from_stop_id,
                label.legs[-1].to_stop_id,
            )
        if label.segments:
            return (
                label.segments[0].from_stop_id,
                label.segments[-1].to_stop_id,
            )
        return label.stop_ids[0], label.stop_ids[-1]

    @staticmethod
    def _collapse_terminal_walk_alternatives(
        labels: list[_Label],
    ) -> list[_Label]:
        grouped: dict[tuple[object, ...], list[_Label]] = defaultdict(list)
        ungrouped = []
        for label in labels:
            terminal_walk = (
                label.segments[-1]
                if label.segments
                and isinstance(label.segments[-1], WalkLeg)
                else None
            )
            transit_index = len(label.segments) - 1
            if terminal_walk is not None:
                transit_index -= 1
            if (
                transit_index < 0
                or not isinstance(label.segments[transit_index], RouteLeg)
            ):
                ungrouped.append(label)
                continue

            final_transit = label.segments[transit_index]
            key = (
                label.segments[:transit_index],
                final_transit.trip_id,
                final_transit.from_stop_id,
                final_transit.departure_time,
            )
            grouped[key].append(label)

        collapsed = list(ungrouped)
        for alternatives in grouped.values():
            if not any(
                isinstance(label.segments[-1], WalkLeg)
                for label in alternatives
            ):
                collapsed.extend(alternatives)
                continue
            collapsed.append(
                min(
                    alternatives,
                    key=RouteSelectionService._terminal_preference,
                )
            )
        return collapsed

    @staticmethod
    def _terminal_preference(label: _Label) -> tuple[object, ...]:
        terminal_walk = (
            label.segments[-1]
            if isinstance(label.segments[-1], WalkLeg)
            else None
        )
        final_transit = (
            label.segments[-2]
            if terminal_walk is not None
            else label.segments[-1]
        )
        assert isinstance(final_transit, RouteLeg)
        return (
            terminal_walk.distance_meters if terminal_walk is not None else 0,
            -len(final_transit.stop_ids),
            label.time,
            RouteSelectionService._signature(label),
        )

    @staticmethod
    def _route_dominates(first: _Label, second: _Label) -> bool:
        first_values = (
            first.time,
            max(0, first.boardings - 1),
            first.walking_seconds,
        )
        second_values = (
            second.time,
            max(0, second.boardings - 1),
            second.walking_seconds,
        )
        return all(
            first_value <= second_value
            for first_value, second_value in zip(first_values, second_values)
        ) and first_values != second_values

    @staticmethod
    def _signature(label: _Label) -> tuple[tuple[object, ...], ...]:
        return label.signature
