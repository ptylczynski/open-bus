from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from itertools import chain

from django.conf import settings

from gtfs.models import Stop, StopTime


@dataclass(frozen=True)
class StopEvent:
    stop_id: str
    arrival_time: timedelta | None
    departure_time: timedelta | None


@dataclass(frozen=True)
class TripInfo:
    route_id: str
    line_number: str
    line_name: str
    direction: str
    direction_id: int | None


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class RouteState:
    stop_id: str
    stop_ids: tuple[str, ...]
    available_time: timedelta
    trip_ids: tuple[str, ...]
    trips_taken: int
    legs: tuple[RouteLeg, ...]


@dataclass(frozen=True)
class RouteOption:
    stop_ids: tuple[str, ...]
    hops: int
    arrival_time: timedelta
    legs: tuple[RouteLeg, ...] = ()


TripPath = tuple[StopEvent, ...]
TripDeparture = tuple[str, int]


class RouteSelectionService:
    def __init__(
        self,
        max_hops: int | None = None,
        max_workers: int | None = None,
        max_alternatives_per_hop: int | None = None,
    ) -> None:
        self.max_hops = (
            settings.ROUTE_MAX_HOPS if max_hops is None else max_hops
        )
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
        if self.max_hops < 0:
            raise ValueError('ROUTE_MAX_HOPS must be zero or greater')
        if self.max_workers < 1:
            raise ValueError('ROUTE_CALCULATION_WORKERS must be at least one')
        if self.max_alternatives_per_hop < 1:
            raise ValueError(
                'ROUTE_MAX_ALTERNATIVES_PER_HOP must be at least one'
            )

        self._trip_stops: dict[str, TripPath] = {}
        self._trip_info: dict[str, TripInfo] = {}
        self._departures: dict[str, tuple[TripDeparture, ...]] = {}
        self._min_exchange_time = timedelta(0)
        self._max_exchange_time = timedelta(0)

    def find_route(
        self,
        from_stop_id: str,
        to_stop_id: str,
        departure_time: timedelta,
        min_exchange_time: timedelta | None = None,
        max_exchange_time: timedelta | None = None,
    ) -> list[str] | None:
        routes = self.find_routes(
            from_stop_id,
            to_stop_id,
            departure_time,
            min_exchange_time,
            max_exchange_time,
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
    ) -> list[RouteOption]:
        if from_stop_id == to_stop_id:
            return [
                RouteOption(
                    stop_ids=(from_stop_id,),
                    hops=0,
                    arrival_time=departure_time,
                ),
            ]

        self._min_exchange_time = (
            timedelta(seconds=settings.ROUTE_MIN_EXCHANGE_TIME_SECONDS)
            if min_exchange_time is None
            else min_exchange_time
        )
        self._max_exchange_time = (
            timedelta(seconds=settings.ROUTE_MAX_EXCHANGE_TIME_SECONDS)
            if max_exchange_time is None
            else max_exchange_time
        )
        if self._min_exchange_time < timedelta(0):
            raise ValueError('min_exchange_time must be zero or greater')
        if self._max_exchange_time < timedelta(0):
            raise ValueError('max_exchange_time must be zero or greater')
        if self._min_exchange_time > self._max_exchange_time:
            raise ValueError(
                'min_exchange_time must not exceed max_exchange_time'
            )

        self._load_graph()
        starting_stop_ids = self._starting_stop_ids(from_stop_id)
        frontier = [
            RouteState(
                stop_id=starting_stop_id,
                stop_ids=(starting_stop_id,),
                available_time=departure_time,
                trip_ids=(),
                trips_taken=0,
                legs=(),
            )
            for starting_stop_id in starting_stop_ids
        ]
        seen_states = {
            ((starting_stop_id,), departure_time, (), 0)
            for starting_stop_id in starting_stop_ids
        }
        routes = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # One trip is a direct connection. Every additional trip adds one
            # transfer, so max_hops permits max_hops + 1 search levels.
            for _ in range(self.max_hops + 1):
                next_frontier: dict[
                    tuple[
                        tuple[str, ...],
                        timedelta,
                        tuple[str, ...],
                        int,
                    ],
                    RouteState,
                ] = {}
                destination_candidates = []
                expansions = executor.map(
                    self._expand_from_stop,
                    frontier,
                )
                for candidate in chain.from_iterable(expansions):
                    state_key = (
                        candidate.stop_ids,
                        candidate.available_time,
                        candidate.trip_ids,
                        candidate.trips_taken,
                    )
                    if state_key in seen_states:
                        continue
                    if state_key in next_frontier:
                        continue
                    if candidate.stop_id == to_stop_id:
                        destination_candidates.append(candidate)
                        continue
                    next_frontier[state_key] = candidate

                if destination_candidates:
                    selected = self._select_destinations(
                        destination_candidates,
                    )
                    routes.extend(
                        RouteOption(
                            stop_ids=candidate.stop_ids,
                            hops=candidate.trips_taken - 1,
                            arrival_time=candidate.available_time,
                            legs=candidate.legs,
                        )
                        for candidate in selected
                    )

                if not next_frontier:
                    break
                seen_states.update(next_frontier)
                frontier = list(next_frontier.values())

        return routes

    @staticmethod
    def _starting_stop_ids(from_stop_id: str) -> tuple[str, ...]:
        stop_name = Stop.objects.filter(
            stop_id=from_stop_id,
        ).values_list(
            'stop_name',
            flat=True,
        ).first()
        if stop_name is None:
            return (from_stop_id,)
        return tuple(
            Stop.objects.filter(stop_name=stop_name)
            .order_by('stop_id')
            .values_list('stop_id', flat=True),
        )

    def _select_destinations(
        self,
        candidates: list[RouteState],
    ) -> list[RouteState]:
        selected = []
        selected_journeys = set()
        for candidate in sorted(candidates, key=self._route_sort_key):
            journey_key = (
                candidate.stop_ids,
                tuple(leg.route_id for leg in candidate.legs),
            )
            if journey_key in selected_journeys:
                continue
            selected.append(candidate)
            selected_journeys.add(journey_key)
            if len(selected) == self.max_alternatives_per_hop:
                break
        return selected

    @staticmethod
    def _route_sort_key(
        candidate: RouteState,
    ) -> tuple[
        timedelta,
        int,
        tuple[str, ...],
        tuple[str, ...],
    ]:
        return (
            candidate.available_time,
            len(candidate.stop_ids),
            candidate.stop_ids,
            candidate.trip_ids,
        )

    def _load_graph(self) -> None:
        trips: dict[str, list[StopEvent]] = defaultdict(list)
        stop_times = StopTime.objects.order_by(
            'trip_id',
            'stop_sequence',
        ).values_list(
            'trip_id',
            'trip__route_id',
            'trip__route__route_short_name',
            'trip__route__route_long_name',
            'trip__trip_headsign',
            'trip__direction_id',
            'stop_id',
            'arrival_time',
            'departure_time',
        )
        trip_info = {}
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
        ) in stop_times:
            trip_info[trip_id] = TripInfo(
                route_id=route_id,
                line_number=line_number,
                line_name=line_name,
                direction=direction,
                direction_id=direction_id,
            )
            trips[trip_id].append(
                StopEvent(
                    stop_id=stop_id,
                    arrival_time=arrival_time,
                    departure_time=departure_time,
                ),
            )

        departures: dict[str, list[TripDeparture]] = defaultdict(list)
        for trip_id, stop_events in trips.items():
            for index, stop_event in enumerate(stop_events[:-1]):
                departures[stop_event.stop_id].append((trip_id, index))

        self._trip_stops = {
            trip_id: tuple(stop_events)
            for trip_id, stop_events in trips.items()
        }
        self._trip_info = trip_info
        self._departures = {
            stop_id: tuple(stop_departures)
            for stop_id, stop_departures in departures.items()
        }

    def _expand_from_stop(
        self,
        state: RouteState,
    ) -> list[RouteState]:
        candidates = []
        for trip_id, stop_index in self._departures.get(state.stop_id, ()):
            if trip_id in state.trip_ids:
                continue
            trip_events = self._trip_stops[trip_id]
            trip_info = self._trip_info[trip_id]
            boarding_event = trip_events[stop_index]
            boarding_time = (
                boarding_event.departure_time or boarding_event.arrival_time
            )
            if boarding_time is None:
                continue

            earliest_boarding_time = state.available_time
            latest_boarding_time = None
            if state.trips_taken:
                earliest_boarding_time += self._min_exchange_time
                latest_boarding_time = (
                    state.available_time + self._max_exchange_time
                )
            if boarding_time < earliest_boarding_time:
                continue
            if (
                latest_boarding_time is not None
                and boarding_time > latest_boarding_time
            ):
                continue

            for destination_index in range(
                stop_index + 1,
                len(trip_events),
            ):
                destination_event = trip_events[destination_index]
                arrival_time = (
                    destination_event.arrival_time
                    or destination_event.departure_time
                )
                if arrival_time is None or arrival_time < boarding_time:
                    continue
                segment_stop_ids = tuple(
                    event.stop_id
                    for event in trip_events[
                        stop_index + 1:
                        destination_index + 1
                    ]
                )
                if (
                    len(set(segment_stop_ids)) != len(segment_stop_ids)
                    or set(state.stop_ids).intersection(segment_stop_ids)
                ):
                    continue
                candidates.append(
                    RouteState(
                        stop_id=destination_event.stop_id,
                        stop_ids=state.stop_ids + segment_stop_ids,
                        available_time=arrival_time,
                        trip_ids=state.trip_ids + (trip_id,),
                        trips_taken=state.trips_taken + 1,
                        legs=state.legs + (
                            RouteLeg(
                                trip_id=trip_id,
                                route_id=trip_info.route_id,
                                line_number=trip_info.line_number,
                                line_name=trip_info.line_name,
                                direction=trip_info.direction,
                                direction_id=trip_info.direction_id,
                                from_stop_id=state.stop_id,
                                to_stop_id=destination_event.stop_id,
                                stop_ids=(state.stop_id,) + segment_stop_ids,
                                departure_time=boarding_time,
                                arrival_time=arrival_time,
                            ),
                        ),
                    ),
                )
        return candidates
