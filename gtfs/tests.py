import io
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from gtfs.services import GtfsDownloadBatchError, GtfsDownloadService


def make_zip():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zip_file:
        zip_file.writestr('agency.txt', 'agency_id,agency_name\n1,Example')
    return archive.getvalue()


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@override_settings(
    GTFS_URLS=['https://example.com/transit.zip'],
    GTFS_DOWNLOAD_INTERVAL_SECONDS=300,
    GTFS_DOWNLOAD_TIMEOUT_SECONDS=10,
)
class GtfsDownloadServiceTests(SimpleTestCase):
    def test_downloads_valid_zip_to_configured_directory(self):
        with TemporaryDirectory() as directory:
            with patch(
                'gtfs.services.urllib.request.urlopen',
                return_value=Response(make_zip()),
            ) as urlopen:
                service = GtfsDownloadService(destination=directory)
                downloaded = service.download_all()

            self.assertEqual(downloaded, [Path(directory) / 'transit.zip'])
            self.assertTrue(zipfile.is_zipfile(downloaded[0]))
            self.assertEqual(urlopen.call_args.kwargs['timeout'], 10)

    def test_invalid_zip_is_removed_and_reported(self):
        with TemporaryDirectory() as directory:
            with patch(
                'gtfs.services.urllib.request.urlopen',
                return_value=Response(b'not a zip'),
            ):
                service = GtfsDownloadService(destination=directory)

                with self.assertRaises(GtfsDownloadBatchError):
                    service.download_all()

            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_duplicate_filenames_are_made_unique(self):
        urls = [
            'https://first.example/gtfs.zip',
            'https://second.example/gtfs.zip',
        ]
        with TemporaryDirectory() as directory:
            with patch(
                'gtfs.services.urllib.request.urlopen',
                side_effect=[Response(make_zip()), Response(make_zip())],
            ):
                downloaded = GtfsDownloadService(
                    urls=urls,
                    destination=directory,
                ).download_all()

            self.assertEqual(len({path.name for path in downloaded}), 2)

    def test_failure_does_not_prevent_later_downloads(self):
        urls = [
            'https://first.example/broken.zip',
            'https://second.example/working.zip',
        ]
        with TemporaryDirectory() as directory:
            with patch(
                'gtfs.services.urllib.request.urlopen',
                side_effect=[Response(b'not a zip'), Response(make_zip())],
            ):
                service = GtfsDownloadService(urls=urls, destination=directory)

                with self.assertRaises(GtfsDownloadBatchError) as context:
                    service.download_all()

            self.assertEqual(
                context.exception.downloaded,
                [Path(directory) / 'working.zip'],
            )
