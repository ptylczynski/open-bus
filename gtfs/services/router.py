from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

from gtfs.models import StopTime


StopPath = tuple[str, ...]
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

        self._trip_stops: dict[str, StopPath] = {}
        self._departures: dict[str, tuple[TripDeparture, ...]] = {}

    def find_route(
        self,
        from_stop_id: str,
        to_stop_id: str,
    ) -> list[str] | None:
        if from_stop_id == to_stop_id:
            return [from_stop_id]

        self._load_graph()
        frontier: dict[str, StopPath] = {
            from_stop_id: (from_stop_id,),
        }
        visited = {from_stop_id}

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # One trip is a direct connection. Every additional trip adds one
            # transfer, so max_hops permits max_hops + 1 search levels.
            for _ in range(self.max_hops + 1):
                next_frontier: dict[str, StopPath] = {}
                expansions = executor.map(
                    self._expand_from_stop,
                    frontier.items(),
                )
                for candidates in expansions:
                    for stop_id, path in candidates:
                        if stop_id in visited or stop_id in next_frontier:
                            continue
                        if stop_id == to_stop_id:
                            return list(path)
                        next_frontier[stop_id] = path

                if not next_frontier:
                    return None
                visited.update(next_frontier)
                frontier = next_frontier

        return None

    def _load_graph(self) -> None:
        trips: dict[str, list[str]] = defaultdict(list)
        stop_times = StopTime.objects.order_by(
            'trip_id',
            'stop_sequence',
        ).values_list('trip_id', 'stop_id')
        for trip_id, stop_id in stop_times:
            trips[trip_id].append(stop_id)

        departures: dict[str, list[TripDeparture]] = defaultdict(list)
        for trip_id, stop_ids in trips.items():
            for index, stop_id in enumerate(stop_ids[:-1]):
                departures[stop_id].append((trip_id, index))

        self._trip_stops = {
            trip_id: tuple(stop_ids)
            for trip_id, stop_ids in trips.items()
        }
        self._departures = {
            stop_id: tuple(stop_departures)
            for stop_id, stop_departures in departures.items()
        }

    def _expand_from_stop(
        self,
        frontier_item: tuple[str, StopPath],
    ) -> list[tuple[str, StopPath]]:
        stop_id, path = frontier_item
        candidates = []
        for trip_id, stop_index in self._departures.get(stop_id, ()):
            trip_stops = self._trip_stops[trip_id]
            for destination_index in range(stop_index + 1, len(trip_stops)):
                segment = trip_stops[stop_index + 1:destination_index + 1]
                candidates.append((trip_stops[destination_index], path + segment))
        return candidates
