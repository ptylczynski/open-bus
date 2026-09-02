import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Self
from unittest.mock import Mock, call, patch

from django.urls import reverse
from django.test import SimpleTestCase, TestCase, override_settings

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

from gtfs.services import (
    GtfsDownloadBatchError,
    GtfsDownloadService,
    GtfsImportService,
    RouteLeg,
    RouteOption,
    RouteSelectionService,
)


def make_zip(marker: str = '') -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zip_file:
        zip_file.writestr(
            'agency.txt',
            'agency_id,agency_name,agency_url,agency_timezone\n'
            f'1,Example{marker},https://example.com,UTC',
        )
    return archive.getvalue()


def make_complete_gtfs_zip() -> bytes:
    files = {
        'agency.txt': (
            'agency_id,agency_name,agency_url,agency_timezone\n'
            'new,New Transit,https://example.com,Europe/Warsaw\n'
        ),
        'calendar.txt': (
            'service_id,monday,tuesday,wednesday,thursday,friday,saturday,'
            'sunday,start_date,end_date\n'
            'weekday,1,1,1,1,1,0,0,20260901,20260930\n'
        ),
        'calendar_dates.txt': (
            'service_id,date,exception_type\n'
            'weekday,20260905,1\n'
        ),
        'feed_info.txt': (
            'feed_publisher_name,feed_publisher_url,feed_lang,'
            'feed_start_date,feed_end_date\n'
            'New Transit,https://example.com,en,20260901,20260930\n'
        ),
        'routes.txt': (
            'route_id,agency_id,route_short_name,route_long_name,route_type\n'
            'route,new,10,Central Station,3\n'
        ),
        'shapes.txt': (
            'shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n'
            'shape,52.4,16.9,0\n'
        ),
        'stops.txt': (
            'stop_id,stop_code,stop_name,stop_lat,stop_lon,zone_id\n'
            'stop,STOP01,Main Street,52.4,16.9,A\n'
        ),
        'trips.txt': (
            'route_id,service_id,trip_id,trip_headsign,direction_id,shape_id,'
            'wheelchair_accessible,brigade\n'
            'route,weekday,trip,Downtown,0,shape,1,12\n'
        ),
        'stop_times.txt': (
            'trip_id,arrival_time,departure_time,stop_id,stop_sequence,'
            'stop_headsign,pickup_type,drop_off_type\n'
            'trip,25:10:05,25:10:30,stop,0,Downtown,0,0\n'
        ),
    }
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zip_file:
        for filename, contents in files.items():
            zip_file.writestr(filename, contents)
    return archive.getvalue()


class Response(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


@override_settings(
    GTFS_URLS=['https://example.com/transit.zip'],
    GTFS_DOWNLOAD_INTERVAL_SECONDS=300,
    GTFS_DOWNLOAD_TIMEOUT_SECONDS=10,
)
class GtfsDownloadServiceTests(SimpleTestCase):
    def test_downloads_valid_zip_to_configured_directory(self) -> None:
        with TemporaryDirectory() as directory:
            importer = Mock()
            with patch(
                'gtfs.services.downloader.urllib.request.urlopen',
                return_value=Response(make_zip()),
            ) as urlopen:
                service = GtfsDownloadService(
                    destination=directory,
                    importer=importer,
                )
                downloaded = service.download_all()

            self.assertEqual(downloaded, [Path(directory) / 'transit.zip'])
            self.assertTrue(zipfile.is_zipfile(downloaded[0]))
            self.assertEqual(urlopen.call_args.kwargs['timeout'], 10)
            importer.load.assert_called_once()

    def test_logs_download_and_import_progress(self) -> None:
        with TemporaryDirectory() as directory:
            with patch(
                'gtfs.services.downloader.urllib.request.urlopen',
                return_value=Response(make_zip()),
            ):
                with self.assertLogs(
                    'gtfs.services.downloader',
                    level='INFO',
                ) as logs:
                    GtfsDownloadService(
                        destination=directory,
                        importer=Mock(),
                    ).download_all()

            output = '\n'.join(logs.output)
            self.assertIn('Starting GTFS batch with 1 feed(s)', output)
            self.assertIn('Processing GTFS feed 1/1', output)
            self.assertIn('Downloaded and imported GTFS feed', output)
            self.assertIn(
                'GTFS batch complete: 1 imported, 0 unchanged, '
                '0 old archive(s) removed',
                output,
            )

    def test_invalid_zip_is_removed_and_reported(self) -> None:
        with TemporaryDirectory() as directory:
            importer = Mock()
            with patch(
                'gtfs.services.downloader.urllib.request.urlopen',
                return_value=Response(b'not a zip'),
            ):
                service = GtfsDownloadService(
                    destination=directory,
                    importer=importer,
                )

                with self.assertRaises(GtfsDownloadBatchError):
                    service.download_all()

            self.assertEqual(list(Path(directory).iterdir()), [])
            importer.load.assert_not_called()

    def test_duplicate_filenames_are_made_unique(self) -> None:
        urls = [
            'https://first.example/gtfs.zip',
            'https://second.example/gtfs.zip',
        ]
        with TemporaryDirectory() as directory:
            importer = Mock()
            with patch(
                'gtfs.services.downloader.urllib.request.urlopen',
                side_effect=[Response(make_zip()), Response(make_zip())],
            ):
                downloaded = GtfsDownloadService(
                    urls=urls,
                    destination=directory,
                    importer=importer,
                ).download_all()

            self.assertEqual(len({path.name for path in downloaded}), 2)
            self.assertEqual(importer.load.call_count, 2)

    def test_failure_does_not_prevent_later_downloads(self) -> None:
        urls = [
            'https://first.example/broken.zip',
            'https://second.example/working.zip',
        ]
        with TemporaryDirectory() as directory:
            importer = Mock()
            with patch(
                'gtfs.services.downloader.urllib.request.urlopen',
                side_effect=[Response(b'not a zip'), Response(make_zip())],
            ):
                service = GtfsDownloadService(
                    urls=urls,
                    destination=directory,
                    importer=importer,
                )

                with self.assertRaises(GtfsDownloadBatchError) as context:
                    service.download_all()

            self.assertEqual(
                context.exception.downloaded,
                [Path(directory) / 'working.zip'],
            )

    def test_unchanged_archive_is_not_imported(self) -> None:
        archive = make_zip()
        with TemporaryDirectory() as directory:
            destination = Path(directory) / 'transit.zip'
            destination.write_bytes(archive)
            importer = Mock()
            with patch(
                'gtfs.services.downloader.urllib.request.urlopen',
                return_value=Response(archive),
            ):
                downloaded = GtfsDownloadService(
                    destination=directory,
                    importer=importer,
                ).download_all()

            self.assertEqual(downloaded, [])
            self.assertEqual(destination.read_bytes(), archive)
            importer.load.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_changed_archive_is_imported_and_replaces_previous_file(self) -> None:
        old_archive = make_zip(' old')
        new_archive = make_zip(' new')
        with TemporaryDirectory() as directory:
            destination = Path(directory) / 'transit.zip'
            destination.write_bytes(old_archive)
            importer = Mock()
            with patch(
                'gtfs.services.downloader.urllib.request.urlopen',
                return_value=Response(new_archive),
            ):
                downloaded = GtfsDownloadService(
                    destination=directory,
                    importer=importer,
                ).download_all()

            self.assertEqual(downloaded, [destination])
            self.assertEqual(destination.read_bytes(), new_archive)
            importer.load.assert_called_once()

    def test_successful_batch_removes_old_archives(self) -> None:
        with TemporaryDirectory() as directory:
            old_archive = Path(directory) / 'old.zip'
            old_archive.write_bytes(make_zip(' old'))
            unrelated_file = Path(directory) / 'notes.txt'
            unrelated_file.write_text('keep me')
            importer = Mock()
            with patch(
                'gtfs.services.downloader.urllib.request.urlopen',
                return_value=Response(make_zip(' new')),
            ):
                downloaded = GtfsDownloadService(
                    urls=['https://example.com/new.zip'],
                    destination=directory,
                    importer=importer,
                ).download_all()

            self.assertEqual(downloaded, [Path(directory) / 'new.zip'])
            self.assertFalse(old_archive.exists())
            self.assertTrue(unrelated_file.exists())

    def test_failed_batch_keeps_old_archives(self) -> None:
        urls = [
            'https://example.com/new.zip',
            'https://example.com/broken.zip',
        ]
        with TemporaryDirectory() as directory:
            old_archive = Path(directory) / 'old.zip'
            old_archive.write_bytes(make_zip(' old'))
            importer = Mock()
            with patch(
                'gtfs.services.downloader.urllib.request.urlopen',
                side_effect=[Response(make_zip(' new')), Response(b'not a zip')],
            ):
                with self.assertRaises(GtfsDownloadBatchError):
                    GtfsDownloadService(
                        urls=urls,
                        destination=directory,
                        importer=importer,
                    ).download_all()

            self.assertTrue(old_archive.exists())
            self.assertTrue((Path(directory) / 'new.zip').exists())

    def test_failed_import_keeps_previous_archive(self) -> None:
        old_archive = make_zip(' old')
        new_archive = make_zip(' new')
        with TemporaryDirectory() as directory:
            destination = Path(directory) / 'transit.zip'
            destination.write_bytes(old_archive)
            importer = Mock()
            importer.load.side_effect = ValueError('broken feed')
            with patch(
                'gtfs.services.downloader.urllib.request.urlopen',
                return_value=Response(new_archive),
            ):
                with self.assertRaises(GtfsDownloadBatchError):
                    GtfsDownloadService(
                        destination=directory,
                        importer=importer,
                    ).download_all()

            self.assertEqual(destination.read_bytes(), old_archive)
            self.assertEqual(list(Path(directory).iterdir()), [destination])


class GtfsImportServiceTests(SimpleTestCase):
    def test_purges_before_importing_in_dependency_order(self) -> None:
        service = GtfsImportService()
        methods = [
            'purge',
            '_import_agencies',
            '_import_calendars',
            '_import_feed_info',
            '_import_routes',
            '_import_shapes',
            '_import_stops',
            '_import_trips',
            '_import_calendar_dates',
            '_import_stop_times',
        ]
        events = Mock()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w'):
            pass
        archive.seek(0)

        patches = [
            patch.object(
                service,
                method,
                side_effect=lambda *args, name=method: events(name) or 0,
            )
            for method in methods
        ]
        with patch(
            'gtfs.services.importer.transaction.atomic',
            return_value=nullcontext(),
        ):
            with ExitStack() as stack:
                for method_patch in patches:
                    stack.enter_context(method_patch)
                service.load(archive)

        self.assertEqual(
            events.call_args_list,
            [call(method) for method in methods],
        )

    def test_gtfs_times_can_extend_past_midnight(self) -> None:
        value = GtfsImportService._duration(
            {'arrival_time': '25:10:05'},
            'arrival_time',
        )

        self.assertEqual(value.total_seconds(), 25 * 3600 + 10 * 60 + 5)


class GtfsImportDatabaseTests(TestCase):
    def test_replaces_existing_data_with_archive_contents(self) -> None:
        Agency.objects.create(
            agency_id='old',
            agency_name='Old Transit',
            agency_url='https://old.example.com',
            agency_timezone='UTC',
        )
        with TemporaryDirectory() as directory:
            archive = Path(directory) / 'gtfs.zip'
            archive.write_bytes(make_complete_gtfs_zip())

            counts = GtfsImportService().load(archive)

        self.assertEqual(
            counts,
            {
                'agencies': 1,
                'calendars': 1,
                'feed_info': 1,
                'routes': 1,
                'shapes': 1,
                'stops': 1,
                'trips': 1,
                'calendar_dates': 1,
                'stop_times': 1,
            },
        )
        self.assertFalse(Agency.objects.filter(agency_id='old').exists())
        self.assertTrue(Agency.objects.filter(agency_id='new').exists())
        self.assertEqual(Calendar.objects.count(), 1)
        self.assertEqual(CalendarDate.objects.count(), 1)
        self.assertEqual(FeedInfo.objects.count(), 1)
        self.assertEqual(Route.objects.count(), 1)
        self.assertEqual(Shape.objects.count(), 1)
        self.assertEqual(Stop.objects.count(), 1)
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(
            StopTime.objects.get().arrival_time.total_seconds(),
            25 * 3600 + 10 * 60 + 5,
        )

    def test_invalid_archive_rolls_back_purge_and_partial_import(self) -> None:
        Agency.objects.create(
            agency_id='old',
            agency_name='Old Transit',
            agency_url='https://old.example.com',
            agency_timezone='UTC',
        )
        with TemporaryDirectory() as directory:
            archive = Path(directory) / 'gtfs.zip'
            archive.write_bytes(make_zip())

            with self.assertRaisesMessage(
                ValueError,
                'GTFS archive is missing calendar.txt',
            ):
                GtfsImportService().load(archive)

        self.assertTrue(Agency.objects.filter(agency_id='old').exists())
        self.assertFalse(Agency.objects.filter(agency_id='1').exists())


class StopListViewTests(TestCase):
    def test_lists_all_stops_in_model_order(self) -> None:
        Stop.objects.create(
            stop_id='second',
            stop_code='02',
            stop_name='Second Stop',
            stop_lat='52.400000000000',
            stop_lon='16.900000000000',
            zone_id='B',
        )
        Stop.objects.create(
            stop_id='first',
            stop_code='01',
            stop_name='First Stop',
            stop_lat='51.100000000000',
            stop_lon='17.000000000000',
            zone_id='A',
        )

        response = self.client.get(reverse('stop-list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    'stop_id': 'first',
                    'stop_code': '01',
                    'stop_name': 'First Stop',
                    'stop_lat': '51.100000000000',
                    'stop_lon': '17.000000000000',
                    'zone_id': 'A',
                },
                {
                    'stop_id': 'second',
                    'stop_code': '02',
                    'stop_name': 'Second Stop',
                    'stop_lat': '52.400000000000',
                    'stop_lon': '16.900000000000',
                    'zone_id': 'B',
                },
            ],
        )

    def test_returns_empty_list_when_no_stops_exist(self) -> None:
        response = self.client.get(reverse('stop-list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


class StopSuggestionViewTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        for stop_id, stop_name in (
            ('swiety', 'Święty Marcin'),
            ('swietokrzyska', 'Świętokrzyska'),
            ('lukasz', 'Łukaszewicza'),
            ('other', 'Długa'),
        ):
            Stop.objects.create(
                stop_id=stop_id,
                stop_code=stop_id,
                stop_name=stop_name,
                stop_lat='52.400000000000',
                stop_lon='16.900000000000',
            )

    def test_suggests_stops_by_case_and_diacritic_insensitive_prefix(
        self,
    ) -> None:
        response = self.client.get(
            reverse('stop-suggest'),
            {'name': 'SWI'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [stop['stop_id'] for stop in response.json()],
            ['swietokrzyska', 'swiety'],
        )

    def test_ignores_polish_l_stroke(self) -> None:
        response = self.client.get(
            reverse('stop-suggest'),
            {'name': 'LUK'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [stop['stop_id'] for stop in response.json()],
            ['lukasz'],
        )

    def test_requires_at_least_three_characters(self) -> None:
        response = self.client.get(
            reverse('stop-suggest'),
            {'name': 'sw'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('name', response.json())


class RouteCreateViewTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        for index in range(12):
            Stop.objects.create(
                stop_id=f'stop-{index}',
                stop_code=f'{index:02}',
                stop_name=f'Stop {index}',
                stop_lat='52.400000000000',
                stop_lon='16.900000000000',
            )

    @patch(
        'gtfs.views.RouteSelectionService.find_routes',
        return_value=[
            RouteOption(
                stop_ids=('stop-0', 'stop-5', 'stop-1'),
                hops=0,
                arrival_time=timedelta(hours=8, minutes=20),
                legs=(
                    RouteLeg(
                        trip_id='trip-10',
                        route_id='route-10',
                        line_number='10',
                        line_name='City Centre',
                        direction='Central Station',
                        direction_id=0,
                        from_stop_id='stop-0',
                        to_stop_id='stop-1',
                        stop_ids=('stop-0', 'stop-5', 'stop-1'),
                        departure_time=timedelta(hours=8),
                        arrival_time=timedelta(hours=8, minutes=20),
                    ),
                ),
            ),
            RouteOption(
                stop_ids=('stop-0', 'stop-6', 'stop-7', 'stop-1'),
                hops=1,
                arrival_time=timedelta(hours=8, minutes=30),
                legs=(
                    RouteLeg(
                        trip_id='trip-20',
                        route_id='route-20',
                        line_number='20',
                        line_name='Cross-town',
                        direction='Market Square',
                        direction_id=0,
                        from_stop_id='stop-0',
                        to_stop_id='stop-6',
                        stop_ids=('stop-0', 'stop-6'),
                        departure_time=timedelta(hours=8),
                        arrival_time=timedelta(hours=8, minutes=10),
                    ),
                    RouteLeg(
                        trip_id='trip-30',
                        route_id='route-30',
                        line_number='30',
                        line_name='Station link',
                        direction='Central Station',
                        direction_id=1,
                        from_stop_id='stop-6',
                        to_stop_id='stop-1',
                        stop_ids=('stop-6', 'stop-7', 'stop-1'),
                        departure_time=timedelta(hours=8, minutes=15),
                        arrival_time=timedelta(hours=8, minutes=30),
                    ),
                ),
            ),
        ],
    )
    def test_returns_ordered_stops_for_each_route_alternative(
        self,
        find_routes: Mock,
    ) -> None:
        response = self.client.post(
            reverse('route-create'),
            {
                'from_stop_id': 'stop-0',
                'to_stop_id': 'stop-1',
                'departure_time': '08:00:00',
                'min_exchange_time': '00:02:00',
                'max_exchange_time': '00:20:00',
            },
        )

        self.assertEqual(response.status_code, 200)
        routes = response.json()['routes']
        self.assertEqual(
            [route['hops'] for route in routes],
            [0, 1],
        )
        self.assertEqual(
            [
                [stop['stop_id'] for stop in route['stops']]
                for route in routes
            ],
            [
                ['stop-0', 'stop-5', 'stop-1'],
                ['stop-0', 'stop-6', 'stop-7', 'stop-1'],
            ],
        )
        self.assertEqual(
            [
                transfer['stop']['stop_id']
                for transfer in routes[1]['transfers']
            ],
            ['stop-6'],
        )
        self.assertEqual(
            routes[1]['transfers'][0],
            {
                'stop': routes[1]['stops'][1],
                'arrival_time': '08:10:00',
                'departure_time': '08:15:00',
                'wait_time': '00:05:00',
                'from_route_id': 'route-20',
                'from_line_number': '20',
                'to_route_id': 'route-30',
                'to_line_number': '30',
            },
        )
        self.assertEqual(
            routes[1]['legs'][1],
            {
                'trip_id': 'trip-30',
                'route_id': 'route-30',
                'line_number': '30',
                'line_name': 'Station link',
                'direction': 'Central Station',
                'direction_id': 1,
                'from_stop': routes[1]['stops'][1],
                'to_stop': routes[1]['stops'][3],
                'departure_time': '08:15:00',
                'arrival_time': '08:30:00',
                'stops': routes[1]['stops'][1:],
            },
        )
        find_routes.assert_called_once_with(
            'stop-0',
            'stop-1',
            departure_time=timedelta(hours=8),
            min_exchange_time=timedelta(minutes=2),
            max_exchange_time=timedelta(minutes=20),
        )

    @override_settings(
        ROUTE_MIN_EXCHANGE_TIME_SECONDS=180,
        ROUTE_MAX_EXCHANGE_TIME_SECONDS=900,
    )
    @patch('gtfs.views.RouteSelectionService.find_routes', return_value=[])
    def test_uses_default_exchange_times_when_request_omits_them(
        self,
        find_routes: Mock,
    ) -> None:
        response = self.client.post(
            reverse('route-create'),
            {
                'from_stop_id': 'stop-0',
                'to_stop_id': 'stop-1',
                'departure_time': '08:00:00',
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn('No route found', response.json()['detail'])
        find_routes.assert_called_once_with(
            'stop-0',
            'stop-1',
            departure_time=timedelta(hours=8),
            min_exchange_time=timedelta(minutes=3),
            max_exchange_time=timedelta(minutes=15),
        )

    def test_rejects_unknown_from_stop_id(self) -> None:
        response = self.client.post(
            reverse('route-create'),
            {
                'from_stop_id': 'unknown',
                'to_stop_id': 'stop-1',
                'departure_time': '08:00:00',
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('from_stop_id', response.json())

    def test_rejects_unknown_to_stop_id(self) -> None:
        response = self.client.post(
            reverse('route-create'),
            {
                'from_stop_id': 'stop-0',
                'to_stop_id': 'unknown',
                'departure_time': '08:00:00',
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('to_stop_id', response.json())

    def test_requires_departure_time(self) -> None:
        response = self.client.post(
            reverse('route-create'),
            {'from_stop_id': 'stop-0', 'to_stop_id': 'stop-1'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('departure_time', response.json())

    def test_rejects_exchange_range_with_maximum_below_minimum(self) -> None:
        response = self.client.post(
            reverse('route-create'),
            {
                'from_stop_id': 'stop-0',
                'to_stop_id': 'stop-1',
                'departure_time': '08:00:00',
                'min_exchange_time': '00:20:00',
                'max_exchange_time': '00:10:00',
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('max_exchange_time', response.json())


class RouteSelectionServiceTests(TestCase):
    def setUp(self) -> None:
        agency = Agency.objects.create(
            agency_id='agency',
            agency_name='Transit',
            agency_url='https://example.com',
            agency_timezone='UTC',
        )
        calendar = Calendar.objects.create(
            service_id='service',
            monday=True,
            tuesday=True,
            wednesday=True,
            thursday=True,
            friday=True,
            saturday=True,
            sunday=True,
            start_date='2026-01-01',
            end_date='2026-12-31',
        )
        self.route = Route.objects.create(
            route_id='route',
            agency=agency,
            route_short_name='1',
            route_long_name='Main line',
            route_type=3,
        )
        self.calendar = calendar
        for stop_id in ('a', 'b', 'c', 'd', 'e'):
            Stop.objects.create(
                stop_id=stop_id,
                stop_name=f'Stop {stop_id.upper()}',
                stop_lat='52.400000000000',
                stop_lon='16.900000000000',
            )

    def _create_trip(
        self,
        trip_id: str,
        stop_ids: tuple[str, ...],
        start_time: timedelta,
        direction: str = 'Central Station',
        direction_id: int | None = 0,
    ) -> None:
        trip = Trip.objects.create(
            trip_id=trip_id,
            route=self.route,
            service=self.calendar,
            trip_headsign=direction,
            direction_id=direction_id,
        )
        StopTime.objects.bulk_create(
            [
                StopTime(
                    trip=trip,
                    stop_id=stop_id,
                    stop_sequence=index,
                    arrival_time=start_time + timedelta(minutes=index * 10),
                    departure_time=start_time + timedelta(minutes=index * 10),
                )
                for index, stop_id in enumerate(stop_ids)
            ],
        )

    def test_finds_direct_connection_with_intermediate_stops(self) -> None:
        self._create_trip(
            'direct',
            ('a', 'c', 'd', 'b'),
            timedelta(hours=8),
        )

        stop_ids = RouteSelectionService(max_hops=0).find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(stop_ids, ['a', 'c', 'd', 'b'])

    def test_returns_line_direction_and_timing_for_each_leg(self) -> None:
        self._create_trip(
            'direct',
            ('a', 'c', 'b'),
            timedelta(hours=8),
            direction='Railway Station',
            direction_id=1,
        )

        routes = RouteSelectionService(max_hops=0).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(
            routes[0].legs,
            (
                RouteLeg(
                    trip_id='direct',
                    route_id='route',
                    line_number='1',
                    line_name='Main line',
                    direction='Railway Station',
                    direction_id=1,
                    from_stop_id='a',
                    to_stop_id='b',
                    stop_ids=('a', 'c', 'b'),
                    departure_time=timedelta(hours=8),
                    arrival_time=timedelta(hours=8, minutes=20),
                ),
            ),
        )

    @override_settings(ROUTE_CALCULATION_WORKERS=2)
    def test_finds_connection_with_one_transfer_in_parallel(self) -> None:
        self._create_trip('first', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'second',
            ('c', 'd', 'b'),
            timedelta(hours=8, minutes=15),
        )

        original_map = ThreadPoolExecutor.map
        with patch.object(
            ThreadPoolExecutor,
            'map',
            autospec=True,
            side_effect=original_map,
        ) as executor_map:
            with patch(
                'gtfs.services.router.ThreadPoolExecutor',
                side_effect=ThreadPoolExecutor,
            ) as executor_class:
                stop_ids = RouteSelectionService(max_hops=1).find_route(
                    'a',
                    'b',
                    departure_time=timedelta(hours=7, minutes=30),
                )

        self.assertEqual(stop_ids, ['a', 'c', 'd', 'b'])
        executor_class.assert_called_once_with(max_workers=2)
        self.assertEqual(executor_map.call_count, 2)

    def test_prefers_direct_connection_over_transfer(self) -> None:
        self._create_trip('first', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'second',
            ('c', 'b'),
            timedelta(hours=8, minutes=15),
        )
        self._create_trip('third', ('a', 'd', 'b'), timedelta(hours=9))

        stop_ids = RouteSelectionService(max_hops=1).find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(stop_ids, ['a', 'd', 'b'])

    def test_returns_alternatives_for_every_hop_count(self) -> None:
        self._create_trip('direct', ('a', 'd', 'b'), timedelta(hours=9))
        self._create_trip('first', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'second',
            ('c', 'e', 'b'),
            timedelta(hours=8, minutes=15),
        )

        routes = RouteSelectionService(max_hops=1).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual([route.hops for route in routes], [0, 1])
        self.assertEqual(
            [route.stop_ids for route in routes],
            [('a', 'd', 'b'), ('a', 'c', 'e', 'b')],
        )

    def test_limits_alternatives_independently_for_each_hop_count(self) -> None:
        self._create_trip('direct-1', ('a', 'c', 'b'), timedelta(hours=8))
        self._create_trip('direct-2', ('a', 'd', 'b'), timedelta(hours=8))
        self._create_trip('direct-3', ('a', 'e', 'b'), timedelta(hours=8))
        self._create_trip('first-1', ('a', 'c'), timedelta(hours=9))
        self._create_trip(
            'second-1',
            ('c', 'd', 'b'),
            timedelta(hours=9, minutes=15),
        )
        self._create_trip('first-2', ('a', 'd'), timedelta(hours=10))
        self._create_trip(
            'second-2',
            ('d', 'e', 'b'),
            timedelta(hours=10, minutes=15),
        )

        routes = RouteSelectionService(
            max_hops=1,
            max_alternatives_per_hop=2,
        ).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual([route.hops for route in routes], [0, 0, 1, 1])
        self.assertEqual(
            [route.stop_ids for route in routes],
            [
                ('a', 'c', 'b'),
                ('a', 'd', 'b'),
                ('a', 'c', 'd', 'b'),
                ('a', 'd', 'e', 'b'),
            ],
        )

    @override_settings(ROUTE_MAX_ALTERNATIVES_PER_HOP=1)
    def test_uses_configured_alternative_limit(self) -> None:
        self._create_trip('direct-1', ('a', 'c', 'b'), timedelta(hours=8))
        self._create_trip('direct-2', ('a', 'd', 'b'), timedelta(hours=9))

        routes = RouteSelectionService(max_hops=0).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(
            [route.stop_ids for route in routes],
            [('a', 'c', 'b')],
        )

    def test_rejects_non_positive_alternative_limit(self) -> None:
        with self.assertRaisesMessage(
            ValueError,
            'ROUTE_MAX_ALTERNATIVES_PER_HOP must be at least one',
        ):
            RouteSelectionService(max_alternatives_per_hop=0)

    def test_respects_trip_direction(self) -> None:
        self._create_trip(
            'reverse',
            ('b', 'c', 'a'),
            timedelta(hours=8),
        )

        stop_ids = RouteSelectionService(max_hops=2).find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertIsNone(stop_ids)

    def test_stops_search_at_configured_hop_limit(self) -> None:
        self._create_trip('first', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'second',
            ('c', 'd'),
            timedelta(hours=8, minutes=15),
        )
        self._create_trip(
            'third',
            ('d', 'b'),
            timedelta(hours=8, minutes=30),
        )

        with override_settings(ROUTE_MAX_HOPS=1):
            too_short = RouteSelectionService().find_route(
                'a',
                'b',
                departure_time=timedelta(hours=7, minutes=30),
            )
        with override_settings(ROUTE_MAX_HOPS=2):
            complete = RouteSelectionService().find_route(
                'a',
                'b',
                departure_time=timedelta(hours=7, minutes=30),
            )

        self.assertIsNone(too_short)
        self.assertEqual(complete, ['a', 'c', 'd', 'b'])

    def test_ignores_trips_departing_before_requested_time(self) -> None:
        self._create_trip('early', ('a', 'c', 'b'), timedelta(hours=7))
        self._create_trip('later', ('a', 'd', 'b'), timedelta(hours=8))

        stop_ids = RouteSelectionService(max_hops=0).find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(stop_ids, ['a', 'd', 'b'])

    def test_applies_minimum_and_maximum_exchange_times(self) -> None:
        self._create_trip('first', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'too-soon',
            ('c', 'e', 'b'),
            timedelta(hours=8, minutes=11),
        )
        self._create_trip(
            'valid',
            ('c', 'd', 'b'),
            timedelta(hours=8, minutes=15),
        )

        stop_ids = RouteSelectionService(max_hops=1).find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
            min_exchange_time=timedelta(minutes=2),
            max_exchange_time=timedelta(minutes=10),
        )

        self.assertEqual(stop_ids, ['a', 'c', 'd', 'b'])

    def test_rejects_transfer_after_maximum_exchange_time(self) -> None:
        self._create_trip('first', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'too-late',
            ('c', 'b'),
            timedelta(hours=8, minutes=30),
        )

        stop_ids = RouteSelectionService(max_hops=1).find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
            min_exchange_time=timedelta(0),
            max_exchange_time=timedelta(minutes=10),
        )

        self.assertIsNone(stop_ids)
