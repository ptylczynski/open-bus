import csv
import io
import os
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime, timedelta
from typing import IO, TypeVar

from django.db import transaction
from django.db.models import Model

from gtfs.models import (
    Agency,
    Calendar,
    CalendarDate,
    FeedInfo,
    Route,
    Shape,
    Stop,
    StopTime,
    Trip,
)


ArchivePath = str | os.PathLike[str] | IO[bytes]
GtfsRow = Mapping[str, str]
ModelType = TypeVar('ModelType', bound=Model)


class GtfsImportService:
    batch_size = 5000

    def load(self, archive_path: ArchivePath) -> dict[str, int]:
        """Replace all GTFS data with the contents of one GTFS ZIP archive."""
        with zipfile.ZipFile(archive_path) as archive, transaction.atomic():
            self.purge()
            counts = {
                'agencies': self._import_agencies(archive),
                'calendars': self._import_calendars(archive),
                'feed_info': self._import_feed_info(archive),
                'routes': self._import_routes(archive),
                'shapes': self._import_shapes(archive),
                'stops': self._import_stops(archive),
                'trips': self._import_trips(archive),
                'calendar_dates': self._import_calendar_dates(archive),
                'stop_times': self._import_stop_times(archive),
            }
        return counts

    @staticmethod
    def purge() -> None:
        # Delete dependants explicitly so this remains easy to audit if model
        # relationships change later.
        for model in (
            StopTime,
            CalendarDate,
            Trip,
            Route,
            Shape,
            Stop,
            Calendar,
            Agency,
            FeedInfo,
        ):
            model.objects.all().delete()

    def _import_agencies(self, archive: zipfile.ZipFile) -> int:
        return self._bulk_create(
            Agency,
            (
                Agency(
                    agency_id=row.get('agency_id', ''),
                    agency_name=self._required(row, 'agency_name'),
                    agency_url=self._required(row, 'agency_url'),
                    agency_timezone=self._required(row, 'agency_timezone'),
                    agency_phone=row.get('agency_phone', ''),
                    agency_lang=row.get('agency_lang', ''),
                )
                for row in self._rows(archive, 'agency.txt')
            ),
        )

    def _import_calendars(self, archive: zipfile.ZipFile) -> int:
        return self._bulk_create(
            Calendar,
            (
                Calendar(
                    service_id=self._required(row, 'service_id'),
                    monday=self._boolean(row, 'monday'),
                    tuesday=self._boolean(row, 'tuesday'),
                    wednesday=self._boolean(row, 'wednesday'),
                    thursday=self._boolean(row, 'thursday'),
                    friday=self._boolean(row, 'friday'),
                    saturday=self._boolean(row, 'saturday'),
                    sunday=self._boolean(row, 'sunday'),
                    start_date=self._date(row, 'start_date'),
                    end_date=self._date(row, 'end_date'),
                )
                for row in self._rows(archive, 'calendar.txt')
            ),
        )

    def _import_feed_info(self, archive: zipfile.ZipFile) -> int:
        return self._bulk_create(
            FeedInfo,
            (
                FeedInfo(
                    feed_publisher_name=self._required(
                        row,
                        'feed_publisher_name',
                    ),
                    feed_publisher_url=self._required(
                        row,
                        'feed_publisher_url',
                    ),
                    feed_lang=self._required(row, 'feed_lang'),
                    feed_start_date=self._date(
                        row,
                        'feed_start_date',
                        required=False,
                    ),
                    feed_end_date=self._date(
                        row,
                        'feed_end_date',
                        required=False,
                    ),
                )
                for row in self._rows(
                    archive,
                    'feed_info.txt',
                    required=False,
                )
            ),
        )

    def _import_routes(self, archive: zipfile.ZipFile) -> int:
        agency_ids = list(Agency.objects.values_list('agency_id', flat=True))
        sole_agency_id = agency_ids[0] if len(agency_ids) == 1 else None

        def routes() -> Iterator[Route]:
            for row in self._rows(archive, 'routes.txt'):
                agency_id = row.get('agency_id') or sole_agency_id
                if agency_id is None:
                    raise ValueError(
                        'routes.txt requires agency_id when the feed has '
                        'multiple agencies'
                    )
                yield Route(
                    route_id=self._required(row, 'route_id'),
                    agency_id=agency_id,
                    route_short_name=row.get('route_short_name', ''),
                    route_long_name=row.get('route_long_name', ''),
                    route_desc=row.get('route_desc', ''),
                    route_type=self._integer(row, 'route_type'),
                    route_color=row.get('route_color', ''),
                    route_text_color=row.get('route_text_color', ''),
                )

        return self._bulk_create(Route, routes())

    def _import_shapes(self, archive: zipfile.ZipFile) -> int:
        return self._bulk_create(
            Shape,
            (
                Shape(
                    shape_id=self._required(row, 'shape_id'),
                    shape_pt_lat=self._required(row, 'shape_pt_lat'),
                    shape_pt_lon=self._required(row, 'shape_pt_lon'),
                    shape_pt_sequence=self._integer(row, 'shape_pt_sequence'),
                )
                for row in self._rows(archive, 'shapes.txt', required=False)
            ),
        )

    def _import_stops(self, archive: zipfile.ZipFile) -> int:
        return self._bulk_create(
            Stop,
            (
                Stop(
                    stop_id=self._required(row, 'stop_id'),
                    stop_code=row.get('stop_code', ''),
                    stop_name=self._required(row, 'stop_name'),
                    stop_lat=self._required(row, 'stop_lat'),
                    stop_lon=self._required(row, 'stop_lon'),
                    zone_id=row.get('zone_id', ''),
                )
                for row in self._rows(archive, 'stops.txt')
            ),
        )

    def _import_trips(self, archive: zipfile.ZipFile) -> int:
        return self._bulk_create(
            Trip,
            (
                Trip(
                    route_id=self._required(row, 'route_id'),
                    service_id=self._required(row, 'service_id'),
                    trip_id=self._required(row, 'trip_id'),
                    trip_headsign=row.get('trip_headsign', ''),
                    direction_id=self._integer(
                        row,
                        'direction_id',
                        required=False,
                    ),
                    shape_id=row.get('shape_id', ''),
                    wheelchair_accessible=self._integer(
                        row,
                        'wheelchair_accessible',
                        required=False,
                        default=0,
                    ),
                    brigade=row.get('brigade', ''),
                )
                for row in self._rows(archive, 'trips.txt')
            ),
        )

    def _import_calendar_dates(self, archive: zipfile.ZipFile) -> int:
        return self._bulk_create(
            CalendarDate,
            (
                CalendarDate(
                    service_id=self._required(row, 'service_id'),
                    date=self._date(row, 'date'),
                    exception_type=self._integer(row, 'exception_type'),
                )
                for row in self._rows(
                    archive,
                    'calendar_dates.txt',
                    required=False,
                )
            ),
        )

    def _import_stop_times(self, archive: zipfile.ZipFile) -> int:
        return self._bulk_create(
            StopTime,
            (
                StopTime(
                    trip_id=self._required(row, 'trip_id'),
                    arrival_time=self._duration(row, 'arrival_time'),
                    departure_time=self._duration(row, 'departure_time'),
                    stop_id=self._required(row, 'stop_id'),
                    stop_sequence=self._integer(row, 'stop_sequence'),
                    stop_headsign=row.get('stop_headsign', ''),
                    pickup_type=self._integer(
                        row,
                        'pickup_type',
                        required=False,
                        default=0,
                    ),
                    drop_off_type=self._integer(
                        row,
                        'drop_off_type',
                        required=False,
                        default=0,
                    ),
                )
                for row in self._rows(archive, 'stop_times.txt')
            ),
        )

    @staticmethod
    def _rows(
        archive: zipfile.ZipFile,
        filename: str,
        required: bool = True,
    ) -> Iterator[GtfsRow]:
        try:
            source = archive.open(filename)
        except KeyError:
            if required:
                raise ValueError(f'GTFS archive is missing {filename}') from None
            return

        with source, io.TextIOWrapper(
            source,
            encoding='utf-8-sig',
            newline='',
        ) as text_source:
            reader = csv.DictReader(text_source)
            if reader.fieldnames is None:
                raise ValueError(f'{filename} does not contain a CSV header')
            for row in reader:
                yield row

    def _bulk_create(
        self,
        model: type[ModelType],
        objects: Iterable[ModelType],
    ) -> int:
        count = 0
        batch = []
        for instance in objects:
            batch.append(instance)
            if len(batch) == self.batch_size:
                model.objects.bulk_create(batch, batch_size=self.batch_size)
                count += len(batch)
                batch = []
        if batch:
            model.objects.bulk_create(batch, batch_size=self.batch_size)
            count += len(batch)
        return count

    @staticmethod
    def _required(row: GtfsRow, field: str) -> str:
        value = row.get(field)
        if value is None or value == '':
            raise ValueError(f'Missing required GTFS value: {field}')
        return value

    @classmethod
    def _boolean(cls, row: GtfsRow, field: str) -> bool:
        value = cls._integer(row, field)
        if value not in (0, 1):
            raise ValueError(f'{field} must be 0 or 1')
        return bool(value)

    @staticmethod
    def _integer(
        row: GtfsRow,
        field: str,
        required: bool = True,
        default: int | None = None,
    ) -> int | None:
        value = row.get(field)
        if value is None or value == '':
            if required:
                raise ValueError(f'Missing required GTFS value: {field}')
            return default
        try:
            return int(value)
        except ValueError:
            raise ValueError(f'Invalid integer for {field}: {value}') from None

    @staticmethod
    def _date(
        row: GtfsRow,
        field: str,
        required: bool = True,
    ) -> date | None:
        value = row.get(field)
        if value is None or value == '':
            if required:
                raise ValueError(f'Missing required GTFS value: {field}')
            return None
        try:
            return datetime.strptime(value, '%Y%m%d').date()
        except ValueError:
            raise ValueError(f'Invalid GTFS date for {field}: {value}') from None

    @staticmethod
    def _duration(row: GtfsRow, field: str) -> timedelta | None:
        value = row.get(field)
        if value is None or value == '':
            return None
        try:
            hours, minutes, seconds = (int(part) for part in value.split(':'))
            if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f'Invalid GTFS time for {field}: {value}') from None
        return timedelta(hours=hours, minutes=minutes, seconds=seconds)
