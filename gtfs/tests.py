import io
import zipfile
from contextlib import ExitStack, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Self
from unittest.mock import Mock, call, patch

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
