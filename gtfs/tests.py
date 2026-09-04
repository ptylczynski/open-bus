import io
import json
import urllib.error
import zipfile
from contextlib import ExitStack, nullcontext
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Self
from unittest.mock import Mock, call, patch
from urllib.parse import parse_qs, urlparse

from django.urls import reverse
from django.test import SimpleTestCase, TestCase, override_settings

from gtfs.models import (
    Agency,
    Calendar,
    CalendarDate,
    FeedInfo,
    GtfsDatasetState,
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
    WalkLeg,
)
from gtfs.services.routing_data import (
    clear_routing_snapshot_cache,
    great_circle_distance,
    get_routing_snapshot,
)
from gtfs.serializers import (
    ROUTE_FROM_COORDINATE_STOP_ID,
    ROUTE_TO_COORDINATE_STOP_ID,
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
            '_advance_revision',
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
        self.assertEqual(GtfsDatasetState.objects.get().revision, 1)

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
        self.assertFalse(GtfsDatasetState.objects.exists())


class StopListViewTests(TestCase):
    def test_allows_cross_origin_api_requests_by_default(self) -> None:
        response = self.client.get(
            reverse('stop-list'),
            headers={'origin': 'https://frontend.example.com'},
        )

        self.assertEqual(response['Access-Control-Allow-Origin'], '*')

    def test_handles_cross_origin_preflight_requests(self) -> None:
        response = self.client.options(
            reverse('route-create'),
            headers={
                'origin': 'https://frontend.example.com',
                'access-control-request-method': 'POST',
                'access-control-request-headers': 'content-type',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Access-Control-Allow-Origin'], '*')
        self.assertIn('POST', response['Access-Control-Allow-Methods'])
        self.assertIn(
            'content-type',
            response['Access-Control-Allow-Headers'],
        )

    @override_settings(
        CORS_ALLOW_ALL_ORIGINS=False,
        CORS_ALLOWED_ORIGINS=['https://frontend.example.com'],
    )
    def test_allows_a_configured_origin(self) -> None:
        response = self.client.get(
            reverse('stop-list'),
            headers={'origin': 'https://frontend.example.com'},
        )

        self.assertEqual(
            response['Access-Control-Allow-Origin'],
            'https://frontend.example.com',
        )

    @override_settings(
        CORS_ALLOW_ALL_ORIGINS=False,
        CORS_ALLOWED_ORIGINS=['https://frontend.example.com'],
    )
    def test_rejects_an_origin_that_is_not_configured(self) -> None:
        response = self.client.get(
            reverse('stop-list'),
            headers={'origin': 'https://untrusted.example.com'},
        )

        self.assertNotIn('Access-Control-Allow-Origin', response)

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

    def test_lists_one_stop_for_each_stop_name(self) -> None:
        Stop.objects.create(
            stop_id='poznan-platform-2',
            stop_code='02',
            stop_name='Poznań Główny',
            stop_lat='52.400000000000',
            stop_lon='16.900000000000',
        )
        Stop.objects.create(
            stop_id='poznan-platform-1',
            stop_code='01',
            stop_name='Poznań Główny',
            stop_lat='52.400000000000',
            stop_lon='16.900000000000',
        )

        response = self.client.get(reverse('stop-list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [stop['stop_id'] for stop in response.json()],
            ['poznan-platform-1'],
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

    def test_suggests_one_stop_for_each_stop_name(self) -> None:
        Stop.objects.create(
            stop_id='swiety-platform-2',
            stop_code='zzzz',
            stop_name='Święty Marcin',
            stop_lat='52.400000000000',
            stop_lon='16.900000000000',
        )

        response = self.client.get(
            reverse('stop-suggest'),
            {'name': 'SWI'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [stop['stop_id'] for stop in response.json()],
            ['swietokrzyska', 'swiety'],
        )

    def test_requires_at_least_three_characters(self) -> None:
        response = self.client.get(
            reverse('stop-suggest'),
            {'name': 'sw'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('name', response.json())


@override_settings(
    HERE_API_KEY='here-secret',
    HERE_AUTOSUGGEST_LIMIT=2,
    HERE_AUTOSUGGEST_BOUNDING_BOX='bbox:16.7,52.2,17.2,52.6',
    HERE_AUTOSUGGEST_TIMEOUT_SECONDS=7,
)
class GeocodeViewTests(SimpleTestCase):
    @patch('gtfs.services.geocoder.urllib.request.urlopen')
    def test_returns_names_and_coordinates_from_here(
        self,
        urlopen: Mock,
    ) -> None:
        urlopen.return_value = Response(
            json.dumps(
                {
                    'items': [
                        {
                            'title': 'Poznań',
                            'position': {'lat': 52.408, 'lng': 16.934},
                        },
                        {
                            'title': 'Poznań restaurants',
                            'resultType': 'categoryQuery',
                        },
                        {
                            'title': 'Poznań Główny',
                            'position': {'lat': 52.402, 'lng': 16.911},
                        },
                    ],
                },
            ).encode(),
        )

        response = self.client.get(reverse('geocode'), {'text': 'Poznań'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    'name': 'Poznań',
                    'coordinates': {'lat': 52.408, 'lng': 16.934},
                },
                {
                    'name': 'Poznań Główny',
                    'coordinates': {'lat': 52.402, 'lng': 16.911},
                },
            ],
        )
        request = urlopen.call_args.args[0]
        query = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(query['apiKey'], ['here-secret'])
        self.assertEqual(query['q'], ['Poznań'])
        self.assertEqual(query['limit'], ['2'])
        self.assertEqual(query['termsLimit'], ['0'])
        self.assertEqual(query['in'], ['bbox:16.7,52.2,17.2,52.6'])
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 7)

    @patch('gtfs.services.geocoder.urllib.request.urlopen')
    def test_requires_text_longer_than_three_characters(
        self,
        urlopen: Mock,
    ) -> None:
        response = self.client.get(reverse('geocode'), {'text': 'abc'})

        self.assertEqual(response.status_code, 400)
        self.assertIn('text', response.json())
        urlopen.assert_not_called()

    @override_settings(HERE_API_KEY='')
    @patch('gtfs.services.geocoder.urllib.request.urlopen')
    def test_reports_missing_here_api_key(self, urlopen: Mock) -> None:
        response = self.client.get(reverse('geocode'), {'text': 'Poznań'})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {'detail': 'HERE API key is not configured.'},
        )
        urlopen.assert_not_called()

    @patch(
        'gtfs.services.geocoder.urllib.request.urlopen',
        side_effect=urllib.error.URLError('unavailable'),
    )
    def test_reports_here_api_failure(self, urlopen: Mock) -> None:
        response = self.client.get(reverse('geocode'), {'text': 'Poznań'})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {'detail': 'HERE autosuggest request failed.'},
        )
        urlopen.assert_called_once()


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
                'service_date': '2026-09-04',
                'min_exchange_time': '00:02:00',
                'max_exchange_time': '00:20:00',
            },
        )

        self.assertEqual(response.status_code, 200)
        routes = response.json()['routes']
        self.assertEqual(response.json()['service_date'], '2026-09-04')
        self.assertEqual(response.json()['from_stop'], routes[0]['stops'][0])
        self.assertEqual(response.json()['to_stop'], routes[0]['stops'][-1])
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
                'from_stop': routes[1]['stops'][1],
                'to_stop': routes[1]['stops'][1],
                'arrival_time': '08:10:00',
                'departure_time': '08:15:00',
                'wait_time': '00:05:00',
                'walk_time': '00:00:00',
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
        self.assertEqual(
            [segment['mode'] for segment in routes[1]['segments']],
            ['transit', 'transit'],
        )
        self.assertEqual(routes[1]['arrival_time'], '08:30:00')
        self.assertEqual(routes[1]['walking_time'], '00:00:00')
        find_routes.assert_called_once_with(
            'stop-0',
            'stop-1',
            departure_time=timedelta(hours=8),
            min_exchange_time=timedelta(minutes=2),
            max_exchange_time=timedelta(minutes=20),
            service_date=date(2026, 9, 4),
        )

    @patch('gtfs.views.RouteSelectionService.find_routes', return_value=[])
    def test_passes_coordinate_pseudo_stops_to_router(
        self,
        find_routes: Mock,
    ) -> None:
        response = self.client.post(
            reverse('route-create'),
            {
                'from_lat': '0.001',
                'from_lon': '0.001',
                'to_lat': '10.001',
                'to_lon': '10.001',
                'departure_time': '08:00:00',
                'service_date': '2026-09-04',
            },
        )

        self.assertEqual(response.status_code, 404)
        find_routes.assert_called_once_with(
            ROUTE_FROM_COORDINATE_STOP_ID,
            ROUTE_TO_COORDINATE_STOP_ID,
            departure_time=timedelta(hours=8),
            min_exchange_time=timedelta(0),
            max_exchange_time=timedelta(hours=1),
            service_date=date(2026, 9, 4),
            from_coordinates=(0.001, 0.001),
            to_coordinates=(10.001, 10.001),
        )

    @patch('gtfs.views.RouteSelectionService.find_routes')
    def test_returns_coordinate_inputs_as_pseudo_stops(
        self,
        find_routes: Mock,
    ) -> None:
        find_routes.return_value = [
            RouteOption(
                stop_ids=(
                    ROUTE_FROM_COORDINATE_STOP_ID,
                    'stop-0',
                    'stop-1',
                    ROUTE_TO_COORDINATE_STOP_ID,
                ),
                hops=0,
                departure_time=timedelta(hours=7, minutes=58),
                arrival_time=timedelta(hours=8, minutes=22),
                walking_time=timedelta(minutes=4),
                legs=(
                    RouteLeg(
                        trip_id='trip',
                        route_id='route',
                        line_number='1',
                        line_name='Line',
                        direction='Centre',
                        direction_id=0,
                        from_stop_id='stop-0',
                        to_stop_id='stop-1',
                        stop_ids=('stop-0', 'stop-1'),
                        departure_time=timedelta(hours=8),
                        arrival_time=timedelta(hours=8, minutes=20),
                    ),
                ),
                segments=(
                    WalkLeg(
                        from_stop_id=ROUTE_FROM_COORDINATE_STOP_ID,
                        to_stop_id='stop-0',
                        departure_time=timedelta(hours=7, minutes=58),
                        arrival_time=timedelta(hours=8),
                        distance_meters=168,
                    ),
                    RouteLeg(
                        trip_id='trip',
                        route_id='route',
                        line_number='1',
                        line_name='Line',
                        direction='Centre',
                        direction_id=0,
                        from_stop_id='stop-0',
                        to_stop_id='stop-1',
                        stop_ids=('stop-0', 'stop-1'),
                        departure_time=timedelta(hours=8),
                        arrival_time=timedelta(hours=8, minutes=20),
                    ),
                    WalkLeg(
                        from_stop_id='stop-1',
                        to_stop_id=ROUTE_TO_COORDINATE_STOP_ID,
                        departure_time=timedelta(hours=8, minutes=20),
                        arrival_time=timedelta(hours=8, minutes=22),
                        distance_meters=168,
                    ),
                ),
            ),
        ]

        response = self.client.post(
            reverse('route-create'),
            {
                'from_lat': '52.399',
                'from_lon': '16.899',
                'to_lat': '52.401',
                'to_lon': '16.901',
                'departure_time': '07:58:00',
                'service_date': '2026-09-04',
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['from_stop']['stop_name'], 'Start')
        self.assertEqual(
            payload['from_stop']['stop_id'],
            ROUTE_FROM_COORDINATE_STOP_ID,
        )
        self.assertEqual(payload['from_stop']['stop_lat'], '52.399000000000')
        self.assertEqual(payload['to_stop']['stop_name'], 'Finish')
        self.assertEqual(
            payload['to_stop']['stop_id'],
            ROUTE_TO_COORDINATE_STOP_ID,
        )
        self.assertEqual(payload['to_stop']['stop_lon'], '16.901000000000')
        self.assertEqual(
            payload['routes'][0]['stops'][0],
            payload['from_stop'],
        )
        self.assertEqual(
            payload['routes'][0]['stops'][-1],
            payload['to_stop'],
        )
        self.assertEqual(
            [segment['mode'] for segment in payload['routes'][0]['segments']],
            ['walk', 'transit', 'walk'],
        )
        self.assertFalse(
            Stop.objects.filter(
                stop_id__in=(
                    ROUTE_FROM_COORDINATE_STOP_ID,
                    ROUTE_TO_COORDINATE_STOP_ID,
                ),
            ).exists(),
        )

    def test_requires_complete_coordinates(self) -> None:
        response = self.client.post(
            reverse('route-create'),
            {
                'from_lat': '52.4',
                'to_stop_id': 'stop-1',
                'departure_time': '08:00:00',
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('from_lon', response.json())

    def test_rejects_stop_id_combined_with_coordinates(self) -> None:
        response = self.client.post(
            reverse('route-create'),
            {
                'from_stop_id': 'stop-0',
                'from_lat': '52.4',
                'from_lon': '16.9',
                'to_stop_id': 'stop-1',
                'departure_time': '08:00:00',
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('from_stop_id', response.json())

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
                'service_date': '2026-09-04',
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
            service_date=date(2026, 9, 4),
        )

    @patch('gtfs.views.RouteSelectionService')
    def test_uses_requested_hop_limit(
        self,
        route_service_class: Mock,
    ) -> None:
        route_service = route_service_class.return_value
        route_service.find_routes.return_value = []
        route_service.max_hops = 2

        response = self.client.post(
            reverse('route-create'),
            {
                'from_stop_id': 'stop-0',
                'to_stop_id': 'stop-1',
                'departure_time': '08:00:00',
                'hops': 2,
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn('2 hop(s)', response.json()['detail'])
        route_service_class.assert_called_once_with(max_hops=2)

    def test_rejects_negative_hop_limit(self) -> None:
        response = self.client.post(
            reverse('route-create'),
            {
                'from_stop_id': 'stop-0',
                'to_stop_id': 'stop-1',
                'departure_time': '08:00:00',
                'hops': -1,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('hops', response.json())

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


@override_settings(
    ROUTE_MAX_WALK_DISTANCE_METERS=0,
    ROUTE_MAX_EXTRA_TRAVEL_SECONDS=86_400,
    ROUTE_MAX_EXTRA_TRAVEL_RATIO=100,
)
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

    @override_settings(
        ROUTE_MAX_WALK_DISTANCE_METERS=500,
        ROUTE_MAX_COORDINATE_STOPS=4,
        ROUTE_WALK_SPEED_METERS_PER_SECOND=1.4,
    )
    def test_walks_from_and_to_exact_coordinate_endpoints(self) -> None:
        Stop.objects.filter(stop_id='a').update(stop_lat=52.0, stop_lon=16.0)
        Stop.objects.filter(stop_id='b').update(stop_lat=52.02, stop_lon=16.0)
        Stop.objects.filter(stop_id__in=('c', 'd', 'e')).update(
            stop_lat=53.0,
            stop_lon=16.0,
        )
        self._create_trip(
            'too-early',
            ('a', 'b'),
            timedelta(hours=7, minutes=56),
        )
        self._create_trip('direct', ('a', 'b'), timedelta(hours=8))
        start = (51.999, 16.0)
        finish = (52.021, 16.0)

        routes = RouteSelectionService(max_hops=0).find_routes(
            ROUTE_FROM_COORDINATE_STOP_ID,
            ROUTE_TO_COORDINATE_STOP_ID,
            departure_time=timedelta(hours=7, minutes=55),
            service_date=date(2026, 9, 4),
            from_coordinates=start,
            to_coordinates=finish,
        )

        self.assertEqual(len(routes), 1)
        route = routes[0]
        self.assertEqual(
            route.stop_ids,
            (
                ROUTE_FROM_COORDINATE_STOP_ID,
                'a',
                'b',
                ROUTE_TO_COORDINATE_STOP_ID,
            ),
        )
        self.assertEqual(
            [type(segment) for segment in route.segments],
            [WalkLeg, RouteLeg, WalkLeg],
        )
        first_walk = route.segments[0]
        last_walk = route.segments[-1]
        assert isinstance(first_walk, WalkLeg)
        assert isinstance(last_walk, WalkLeg)
        self.assertEqual(
            first_walk.distance_meters,
            round(great_circle_distance(start, (52.0, 16.0))),
        )
        self.assertEqual(
            last_walk.distance_meters,
            round(great_circle_distance((52.02, 16.0), finish)),
        )
        self.assertNotIn(
            ROUTE_FROM_COORDINATE_STOP_ID,
            {leg.from_stop_id for leg in route.legs},
        )
        self.assertNotIn(
            ROUTE_TO_COORDINATE_STOP_ID,
            {leg.from_stop_id for leg in route.legs},
        )
        self.assertEqual(route.legs[0].trip_id, 'direct')
        self.assertEqual(
            route.arrival_time,
            route.legs[-1].arrival_time + last_walk.duration,
        )
        self.assertEqual(
            route.walking_time,
            first_walk.duration + last_walk.duration,
        )

    @override_settings(
        ROUTE_MAX_COORDINATE_STOPS=2,
        ROUTE_WALK_SPEED_METERS_PER_SECOND=1,
    )
    def test_limits_coordinate_connections_to_nearby_stops(self) -> None:
        paths = RouteSelectionService()._endpoint_footpaths(
            (0.0, 0.0),
            {
                'first': (0.001, 0.0),
                'second': (0.002, 0.0),
                'third': (0.003, 0.0),
                'too-far': (0.01, 0.0),
            },
        )

        self.assertEqual(
            [path.to_stop_id for path in paths],
            ['first', 'second'],
        )
        self.assertTrue(
            all(path.duration <= 15 * 60 for path in paths),
        )

    @override_settings(
        ROUTE_MAX_COORDINATE_STOPS=4,
        ROUTE_WALK_SPEED_METERS_PER_SECOND=1.4,
    )
    def test_route_limit_prefers_diverse_coordinate_access_stops(self) -> None:
        Stop.objects.filter(stop_id='a').update(stop_lat=52.0005, stop_lon=16)
        Stop.objects.filter(stop_id='c').update(stop_lat=52.001, stop_lon=16)
        Stop.objects.filter(stop_id='b').update(stop_lat=52.0195, stop_lon=16)
        Stop.objects.filter(stop_id='d').update(stop_lat=52.0185, stop_lon=16)
        Stop.objects.filter(stop_id='e').update(stop_lat=52.019, stop_lon=16)
        self._create_trip('best', ('a', 'b'), timedelta(hours=8))
        self._create_trip(
            'same-start',
            ('a', 'd'),
            timedelta(hours=8, minutes=1),
        )
        self._create_trip(
            'diverse',
            ('c', 'e'),
            timedelta(hours=8, minutes=2),
        )

        routes = RouteSelectionService(
            max_hops=0,
            max_alternatives_per_hop=5,
            max_routes=2,
        ).find_routes(
            ROUTE_FROM_COORDINATE_STOP_ID,
            ROUTE_TO_COORDINATE_STOP_ID,
            departure_time=timedelta(hours=7, minutes=55),
            service_date=date(2026, 9, 4),
            from_coordinates=(52.0, 16.0),
            to_coordinates=(52.02, 16.0),
        )

        self.assertEqual(
            [
                (route.legs[0].from_stop_id, route.legs[-1].to_stop_id)
                for route in routes
            ],
            [('a', 'b'), ('c', 'e')],
        )

    @override_settings(
        ROUTE_MAX_COORDINATE_STOPS=4,
        ROUTE_WALK_SPEED_METERS_PER_SECOND=1,
    )
    def test_excludes_stops_over_fifteen_minutes_from_coordinates(self) -> None:
        paths = RouteSelectionService()._endpoint_footpaths(
            (0.0, 0.0),
            {
                'nearby': (0.008, 0.0),
                'too-far': (0.009, 0.0),
            },
        )

        self.assertEqual(
            [path.to_stop_id for path in paths],
            ['nearby'],
        )

    @override_settings(ROUTE_MAX_COORDINATE_STOPS=0)
    def test_rejects_non_positive_coordinate_stop_limit(self) -> None:
        with self.assertRaisesMessage(
            ValueError,
            'ROUTE_MAX_COORDINATE_STOPS must be at least one',
        ):
            RouteSelectionService()

    def test_logs_route_search_progress_and_result(self) -> None:
        self._create_trip(
            'direct',
            ('a', 'c', 'b'),
            timedelta(hours=8),
        )

        with self.assertLogs('gtfs.services.router', level='INFO') as logs:
            RouteSelectionService(max_hops=0).find_routes(
                'a',
                'b',
                departure_time=timedelta(hours=7, minutes=30),
            )

        messages = '\n'.join(logs.output)
        self.assertIn('Starting RAPTOR route search', messages)
        self.assertIn('Searching RAPTOR round 1', messages)
        self.assertIn('RAPTOR route search complete', messages)
        self.assertIn('routes=1', messages)

    def test_uses_all_stops_with_the_selected_start_name(self) -> None:
        Stop.objects.create(
            stop_id='a-platform-2',
            stop_name='Stop A',
            stop_lat='52.400000000000',
            stop_lon='16.900000000000',
        )
        self._create_trip(
            'direct',
            ('a-platform-2', 'c', 'b'),
            timedelta(hours=8),
        )

        stop_ids = RouteSelectionService(max_hops=0).find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(stop_ids, ['a-platform-2', 'c', 'b'])

    def test_uses_all_stops_with_the_selected_destination_name(
        self,
    ) -> None:
        Stop.objects.create(
            stop_id='b-platform-2',
            stop_name='Stop B',
            stop_lat='52.400000000000',
            stop_lon='16.900000000000',
        )
        self._create_trip(
            'second',
            ('a', 'd', 'b-platform-2'),
            timedelta(hours=8),
        )

        routes = RouteSelectionService(
            max_hops=0,
            max_alternatives_per_hop=2,
        ).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(
            [route.stop_ids for route in routes],
            [('a', 'd', 'b-platform-2')],
        )

    def test_shares_per_hop_alternative_limit_between_starting_stops(
        self,
    ) -> None:
        Stop.objects.create(
            stop_id='a-platform-2',
            stop_name='Stop A',
            stop_lat='52.400000000000',
            stop_lon='16.900000000000',
        )
        self._create_trip('first', ('a', 'c', 'b'), timedelta(hours=8))
        self._create_trip(
            'second',
            ('a-platform-2', 'd', 'b'),
            timedelta(hours=9),
        )

        routes = RouteSelectionService(
            max_hops=0,
            max_alternatives_per_hop=1,
        ).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(
            [route.stop_ids for route in routes],
            [('a', 'c', 'b')],
        )

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
    def test_accepts_deprecated_worker_setting(self) -> None:
        self._create_trip('first', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'second',
            ('c', 'd', 'b'),
            timedelta(hours=8, minutes=15),
        )

        service = RouteSelectionService(max_hops=1)
        stop_ids = service.find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(stop_ids, ['a', 'c', 'd', 'b'])
        self.assertEqual(service.max_workers, 2)

    @override_settings(
        ROUTE_MAX_EXTRA_TRAVEL_SECONDS=1800,
        ROUTE_MAX_EXTRA_TRAVEL_RATIO=1.5,
    )
    def test_prefers_staying_on_route_over_a_faster_change(self) -> None:
        Stop.objects.create(
            stop_id='f',
            stop_name='Stop F',
            stop_lat='52.400000000000',
            stop_lon='16.900000000000',
        )
        self._create_trip(
            'stay-on',
            ('a', 'c', 'd', 'e', 'f', 'b'),
            timedelta(hours=8),
        )
        self._create_trip(
            'change',
            ('c', 'b'),
            timedelta(hours=8, minutes=15),
        )

        routes = RouteSelectionService(
            max_hops=1,
            max_routes=1,
        ).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=55),
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].stop_ids, ('a', 'c', 'd', 'e', 'f', 'b'))
        self.assertEqual([leg.trip_id for leg in routes[0].legs], ['stay-on'])

    @override_settings(ROUTE_MAX_WALK_DISTANCE_METERS=500)
    def test_prefers_no_walking_over_an_earlier_arrival(self) -> None:
        Stop.objects.filter(stop_id='a').update(stop_lat=52.0, stop_lon=16.0)
        Stop.objects.filter(stop_id='b').update(stop_lat=52.02, stop_lon=16.0)
        Stop.objects.filter(stop_id='c').update(stop_lat=52.019, stop_lon=16.0)
        Stop.objects.filter(stop_id__in=('d', 'e')).update(
            stop_lat=53.0,
            stop_lon=16.0,
        )
        self._create_trip(
            'walk-earlier',
            ('a', 'c'),
            timedelta(hours=7, minutes=55),
        )
        self._create_trip(
            'no-walk-later',
            ('a', 'b'),
            timedelta(hours=8),
        )

        routes = RouteSelectionService(
            max_hops=0,
            max_alternatives_per_hop=2,
            max_routes=1,
        ).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=50),
            service_date=date(2026, 9, 4),
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].legs[0].trip_id, 'no-walk-later')
        self.assertEqual(routes[0].walking_time, timedelta(0))

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

    def test_selects_the_earliest_route_without_geographic_bias(
        self,
    ) -> None:
        Stop.objects.filter(stop_id='a').update(stop_lat=0, stop_lon=0)
        Stop.objects.filter(stop_id='b').update(stop_lat=0, stop_lon=10)
        Stop.objects.filter(stop_id='c').update(stop_lat=0, stop_lon=9)
        Stop.objects.filter(stop_id='d').update(stop_lat=0, stop_lon=-5)
        Stop.objects.filter(stop_id='e').update(stop_lat=0, stop_lon='9.5')
        self._create_trip('toward-1', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'toward-2',
            ('c', 'e'),
            timedelta(hours=8, minutes=15),
        )
        self._create_trip(
            'toward-3',
            ('e', 'b'),
            timedelta(hours=8, minutes=30),
        )
        self._create_trip('away-1', ('a', 'd'), timedelta(hours=8))
        self._create_trip(
            'away-2',
            ('d', 'b'),
            timedelta(hours=8, minutes=15),
        )

        routes = RouteSelectionService(
            max_hops=2,
            max_workers=1,
            max_routes=1,
        ).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual([route.hops for route in routes], [1])
        self.assertEqual(routes[0].stop_ids, ('a', 'd', 'b'))

    def test_allows_a_route_that_temporarily_moves_away(self) -> None:
        Stop.objects.filter(stop_id='a').update(stop_lat=0, stop_lon=0)
        Stop.objects.filter(stop_id='b').update(stop_lat=0, stop_lon=10)
        Stop.objects.filter(stop_id='c').update(stop_lat=0, stop_lon=-5)
        self._create_trip('away', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'back',
            ('c', 'b'),
            timedelta(hours=8, minutes=15),
        )

        routes = RouteSelectionService(max_hops=1).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual([route.stop_ids for route in routes], [('a', 'c', 'b')])

    def test_treats_a_same_name_stop_as_the_destination(self) -> None:
        Stop.objects.filter(stop_id='a').update(stop_lat=0, stop_lon=0)
        Stop.objects.filter(stop_id='b').update(stop_lat=0, stop_lon=10)
        Stop.objects.filter(stop_id='c').update(stop_lat=0, stop_lon='-9.5')
        Stop.objects.filter(stop_id='d').update(stop_lat=0, stop_lon=9)
        Stop.objects.create(
            stop_id='b-platform-2',
            stop_name='Stop B',
            stop_lat=0,
            stop_lon=-10,
        )
        self._create_trip('west-1', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'west-2',
            ('c', 'b-platform-2'),
            timedelta(hours=8, minutes=15),
        )
        self._create_trip('east-1', ('a', 'd'), timedelta(hours=8))
        self._create_trip(
            'east-2',
            ('d', 'b'),
            timedelta(hours=8, minutes=15),
        )

        routes = RouteSelectionService(
            max_hops=1,
            max_workers=1,
            max_routes=1,
        ).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(
            [route.stop_ids for route in routes],
            [('a', 'c', 'b-platform-2')],
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

        self.assertEqual([route.hops for route in routes], [0, 1, 1])
        self.assertEqual(
            [route.stop_ids for route in routes],
            [
                ('a', 'c', 'b'),
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

    @override_settings(ROUTE_MAX_ROUTES=1)
    def test_uses_configured_global_route_limit(self) -> None:
        self._create_trip('direct-1', ('a', 'c', 'b'), timedelta(hours=8))
        self._create_trip('direct-2', ('a', 'd', 'b'), timedelta(hours=9))

        routes = RouteSelectionService(
            max_alternatives_per_hop=2,
        ).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(
            [route.stop_ids for route in routes],
            [('a', 'c', 'b')],
        )

    def test_global_route_limit_applies_across_hop_counts(self) -> None:
        self._create_trip('direct', ('a', 'd', 'b'), timedelta(hours=9))
        self._create_trip('first', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'second',
            ('c', 'e', 'b'),
            timedelta(hours=8, minutes=15),
        )

        routes = RouteSelectionService(
            max_hops=1,
            max_routes=1,
        ).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
        )

        self.assertEqual(len(routes), 1)

    def test_rejects_non_positive_alternative_limit(self) -> None:
        with self.assertRaisesMessage(
            ValueError,
            'ROUTE_MAX_ALTERNATIVES_PER_HOP must be at least one',
        ):
            RouteSelectionService(max_alternatives_per_hop=0)

    def test_rejects_non_positive_global_route_limit(self) -> None:
        with self.assertRaisesMessage(
            ValueError,
            'ROUTE_MAX_ROUTES must be at least one',
        ):
            RouteSelectionService(max_routes=0)

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

    def test_keeps_later_arrival_needed_by_maximum_exchange_time(self) -> None:
        self._create_trip('early', ('a', 'c'), timedelta(hours=8))
        self._create_trip(
            'later',
            ('a', 'c'),
            timedelta(hours=8, minutes=35),
        )
        self._create_trip(
            'connection',
            ('c', 'b'),
            timedelta(hours=9),
        )

        routes = RouteSelectionService(max_hops=1).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=30),
            max_exchange_time=timedelta(minutes=30),
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(
            [leg.trip_id for leg in routes[0].legs],
            ['later', 'connection'],
        )

    def test_filters_trips_by_service_date_and_exception(self) -> None:
        Calendar.objects.filter(pk=self.calendar.pk).update(
            monday=True,
            tuesday=False,
            wednesday=False,
            thursday=False,
            friday=False,
            saturday=False,
            sunday=False,
        )
        self._create_trip('monday', ('a', 'b'), timedelta(hours=8))

        monday = RouteSelectionService(max_hops=0).find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7),
            service_date=date(2026, 9, 7),
        )
        tuesday = RouteSelectionService(max_hops=0).find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7),
            service_date=date(2026, 9, 8),
        )
        CalendarDate.objects.create(
            service=self.calendar,
            date=date(2026, 9, 8),
            exception_type=1,
        )
        added_tuesday = RouteSelectionService(max_hops=0).find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7),
            service_date=date(2026, 9, 8),
        )

        self.assertEqual(monday, ['a', 'b'])
        self.assertIsNone(tuesday)
        self.assertEqual(added_tuesday, ['a', 'b'])

    def test_respects_pickup_and_drop_off_restrictions(self) -> None:
        self._create_trip('restricted', ('a', 'c', 'b'), timedelta(hours=8))
        StopTime.objects.filter(trip_id='restricted', stop_id='a').update(
            pickup_type=1,
        )

        route = RouteSelectionService(max_hops=0).find_route(
            'a',
            'b',
            departure_time=timedelta(hours=7),
        )

        self.assertIsNone(route)


@override_settings(
    ROUTE_MAX_WALK_DISTANCE_METERS=500,
    ROUTE_WALK_SPEED_METERS_PER_SECOND=1.4,
)
class RoutingWalkingTests(TestCase):
    def setUp(self) -> None:
        agency = Agency.objects.create(
            agency_id='agency',
            agency_name='Transit',
            agency_url='https://example.com',
            agency_timezone='Europe/Warsaw',
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
        route = Route.objects.create(
            route_id='route',
            agency=agency,
            route_short_name='1',
            route_type=3,
        )
        for stop_id, latitude, longitude in (
            ('a', 52.0, 16.0),
            ('c', 52.01, 16.0),
            ('d', 52.0127, 16.0),
            ('b', 52.03, 16.0),
        ):
            Stop.objects.create(
                stop_id=stop_id,
                stop_name=stop_id.upper(),
                stop_lat=latitude,
                stop_lon=longitude,
            )
        first = Trip.objects.create(
            trip_id='first',
            route=route,
            service=calendar,
        )
        second = Trip.objects.create(
            trip_id='second',
            route=route,
            service=calendar,
        )
        StopTime.objects.bulk_create(
            [
                StopTime(
                    trip=first,
                    stop_id='a',
                    stop_sequence=0,
                    arrival_time=timedelta(hours=8),
                    departure_time=timedelta(hours=8),
                ),
                StopTime(
                    trip=first,
                    stop_id='c',
                    stop_sequence=1,
                    arrival_time=timedelta(hours=8, minutes=10),
                    departure_time=timedelta(hours=8, minutes=10),
                ),
                StopTime(
                    trip=second,
                    stop_id='d',
                    stop_sequence=0,
                    arrival_time=timedelta(hours=8, minutes=20),
                    departure_time=timedelta(hours=8, minutes=20),
                ),
                StopTime(
                    trip=second,
                    stop_id='b',
                    stop_sequence=1,
                    arrival_time=timedelta(hours=8, minutes=35),
                    departure_time=timedelta(hours=8, minutes=35),
                ),
            ]
        )

    def test_walks_between_nearby_transfer_stops(self) -> None:
        routes = RouteSelectionService(max_hops=1).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=50),
            service_date=date(2026, 9, 4),
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].stop_ids, ('a', 'c', 'd', 'b'))
        self.assertEqual(routes[0].hops, 1)
        self.assertEqual(
            [type(segment) for segment in routes[0].segments],
            [RouteLeg, WalkLeg, RouteLeg],
        )
        self.assertGreater(routes[0].walking_time, timedelta(0))

    def test_does_not_walk_beyond_the_configured_radius(self) -> None:
        routes = RouteSelectionService(max_hops=0).find_routes(
            'a',
            'c',
            departure_time=timedelta(hours=7),
            service_date=date(2026, 9, 4),
        )

        self.assertEqual(routes[0].stop_ids, ('a', 'c'))
        self.assertEqual(
            [type(segment) for segment in routes[0].segments],
            [RouteLeg],
        )


@override_settings(
    ROUTE_MAX_WALK_DISTANCE_METERS=500,
    ROUTE_WALK_SPEED_METERS_PER_SECOND=1.4,
    ROUTE_MAX_EXTRA_TRAVEL_SECONDS=86_400,
    ROUTE_MAX_EXTRA_TRAVEL_RATIO=100,
)
class TerminalWalkCollapseTests(TestCase):
    def setUp(self) -> None:
        agency = Agency.objects.create(
            agency_id='agency',
            agency_name='Transit',
            agency_url='https://example.com',
            agency_timezone='Europe/Warsaw',
        )
        self.calendar = Calendar.objects.create(
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
            route_type=3,
        )
        for stop_id, latitude in (
            ('a', 52.0000),
            ('s1', 52.0100),
            ('s2', 52.0200),
            ('s3', 52.0300),
            ('s4', 52.0400),
            ('s5', 52.0410),
            ('b', 52.0411),
        ):
            Stop.objects.create(
                stop_id=stop_id,
                stop_name=stop_id.upper(),
                stop_lat=latitude,
                stop_lon=16.0,
            )

    def _create_trip(self, stop_ids: tuple[str, ...]) -> None:
        trip = Trip.objects.create(
            trip_id='bus-a',
            route=self.route,
            service=self.calendar,
        )
        StopTime.objects.bulk_create(
            [
                StopTime(
                    trip=trip,
                    stop_id=stop_id,
                    stop_sequence=index,
                    arrival_time=timedelta(hours=8, minutes=index * 10),
                    departure_time=timedelta(hours=8, minutes=index * 10),
                )
                for index, stop_id in enumerate(stop_ids)
            ]
        )

    def test_keeps_later_alighting_stop_with_shorter_final_walk(self) -> None:
        self._create_trip(('a', 's1', 's2', 's3', 's4', 's5'))

        routes = RouteSelectionService(
            max_hops=0,
            max_alternatives_per_hop=5,
        ).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=50),
            service_date=date(2026, 9, 4),
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(
            routes[0].stop_ids,
            ('a', 's1', 's2', 's3', 's4', 's5', 'b'),
        )
        self.assertEqual(routes[0].legs[-1].to_stop_id, 's5')
        self.assertEqual(
            [type(segment) for segment in routes[0].segments],
            [RouteLeg, WalkLeg],
        )

    def test_keeps_bus_ride_to_destination_instead_of_early_walk(self) -> None:
        self._create_trip(('a', 's1', 's2', 's3', 's4', 'b'))

        routes = RouteSelectionService(
            max_hops=0,
            max_alternatives_per_hop=5,
        ).find_routes(
            'a',
            'b',
            departure_time=timedelta(hours=7, minutes=50),
            service_date=date(2026, 9, 4),
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(
            routes[0].stop_ids,
            ('a', 's1', 's2', 's3', 's4', 'b'),
        )
        self.assertEqual(
            [type(segment) for segment in routes[0].segments],
            [RouteLeg],
        )
        self.assertEqual(routes[0].walking_time, timedelta(0))


class RoutingSnapshotCacheTests(TestCase):
    def tearDown(self) -> None:
        clear_routing_snapshot_cache()

    def test_reuses_snapshot_for_a_committed_revision(self) -> None:
        GtfsDatasetState.objects.create(singleton_id=1, revision=1)

        with patch(
            'gtfs.services.routing_data._build_snapshot',
            wraps=get_routing_snapshot.__globals__['_build_snapshot'],
        ) as build_snapshot:
            first = get_routing_snapshot(date(2026, 9, 4))
            second = get_routing_snapshot(date(2026, 9, 4))

        self.assertIs(first, second)
        self.assertEqual(build_snapshot.call_count, 1)
