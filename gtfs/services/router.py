from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from itertools import chain

from django.conf import settings

from gtfs.models import StopTime


@dataclass(frozen=True)
class StopEvent:
    stop_id: str
    arrival_time: timedelta | None
    departure_time: timedelta | None


@dataclass(frozen=True)
class RouteState:
    stop_id: str
    stop_ids: tuple[str, ...]
    available_time: timedelta
    trips_taken: int


TripPath = tuple[StopEvent, ...]
TripDeparture = tuple[str, int]


class RouteSelectionService:
    def __init__(
        self,
        max_hops: int | None = None,
        max_workers: int | None = None,
    ) -> None:
        self.max_hops = (
            settings.ROUTE_MAX_HOPS if max_hops is None else max_hops
        )
        self.max_workers = (
            settings.ROUTE_CALCULATION_WORKERS
            if max_workers is None
            else max_workers
        )
        if self.max_hops < 0:
            raise ValueError('ROUTE_MAX_HOPS must be zero or greater')
        if self.max_workers < 1:
            raise ValueError('ROUTE_CALCULATION_WORKERS must be at least one')

        self._trip_stops: dict[str, TripPath] = {}
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
        if from_stop_id == to_stop_id:
            return [from_stop_id]

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
        frontier = [
            RouteState(
                stop_id=from_stop_id,
                stop_ids=(from_stop_id,),
                available_time=departure_time,
                trips_taken=0,
            ),
        ]
        seen_states = {(from_stop_id, departure_time)}

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # One trip is a direct connection. Every additional trip adds one
            # transfer, so max_hops permits max_hops + 1 search levels.
            for _ in range(self.max_hops + 1):
                next_frontier: dict[
                    tuple[str, timedelta],
                    RouteState,
                ] = {}
                destination_candidates = []
                expansions = executor.map(
                    self._expand_from_stop,
                    frontier,
                )
                for candidate in chain.from_iterable(expansions):
                    state_key = (
                        candidate.stop_id,
                        candidate.available_time,
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
                    earliest = min(
                        destination_candidates,
                        key=lambda candidate: candidate.available_time,
                    )
                    return list(earliest.stop_ids)

                if not next_frontier:
                    return None
                seen_states.update(next_frontier)
                frontier = list(next_frontier.values())

        return None

    def _load_graph(self) -> None:
        trips: dict[str, list[StopEvent]] = defaultdict(list)
        stop_times = StopTime.objects.order_by(
            'trip_id',
            'stop_sequence',
        ).values_list(
            'trip_id',
            'stop_id',
            'arrival_time',
            'departure_time',
        )
        for trip_id, stop_id, arrival_time, departure_time in stop_times:
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
            trip_events = self._trip_stops[trip_id]
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
                candidates.append(
                    RouteState(
                        stop_id=destination_event.stop_id,
                        stop_ids=state.stop_ids + segment_stop_ids,
                        available_time=arrival_time,
                        trips_taken=state.trips_taken + 1,
                    ),
                )
        return candidates
