import hashlib
import logging
import os
import shutil
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.conf import settings


logger = logging.getLogger(__name__)


class GtfsDownloadBatchError(Exception):
    def __init__(self, errors, downloaded):
        self.errors = errors
        self.downloaded = downloaded
        failed_urls = ', '.join(url for url, _error in errors)
        super().__init__(f'Failed to download GTFS feeds: {failed_urls}')


class GtfsDownloadService:
    def __init__(
        self,
        urls=None,
        destination=None,
        interval_seconds=None,
        timeout_seconds=None,
    ):
        self.urls = list(settings.GTFS_URLS if urls is None else urls)
        self.destination = Path(
            settings.GTFS_TEMP_DIR if destination is None else destination
        )
        self.interval_seconds = (
            settings.GTFS_DOWNLOAD_INTERVAL_SECONDS
            if interval_seconds is None
            else interval_seconds
        )
        self.timeout_seconds = (
            settings.GTFS_DOWNLOAD_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )

        if self.interval_seconds <= 0:
            raise ValueError('GTFS_DOWNLOAD_INTERVAL_SECONDS must be greater than 0')
        if self.timeout_seconds <= 0:
            raise ValueError('GTFS_DOWNLOAD_TIMEOUT_SECONDS must be greater than 0')

    def download_all(self):
        self.destination.mkdir(parents=True, exist_ok=True)
        downloaded = []
        errors = []
        used_names = set()

        for url in self.urls:
            try:
                path = self._download(url, used_names)
            except Exception as error:
                logger.exception('Failed to download GTFS feed from %s', url)
                errors.append((url, error))
            else:
                logger.info('Downloaded GTFS feed from %s to %s', url, path)
                downloaded.append(path)

        if errors:
            raise GtfsDownloadBatchError(errors, downloaded)

        return downloaded

    def run_forever(self):
        while True:
            try:
                self.download_all()
            except GtfsDownloadBatchError:
                logger.exception('One or more GTFS downloads failed')
            time.sleep(self.interval_seconds)

    def _download(self, url, used_names):
        filename = self._filename_for(url, used_names)
        destination = self.destination / filename
        request = urllib.request.Request(
            url,
            headers={'User-Agent': 'open-bus GTFS downloader'},
        )
        temporary_path = None

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                with tempfile.NamedTemporaryFile(
                    dir=self.destination,
                    prefix=f'.{filename}.',
                    suffix='.part',
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    shutil.copyfileobj(response, temporary_file)

            if not zipfile.is_zipfile(temporary_path):
                raise ValueError(f'{url} did not return a valid ZIP file')

            os.replace(temporary_path, destination)
            return destination
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _filename_for(url, used_names):
        filename = Path(unquote(urlparse(url).path)).name
        if not filename.lower().endswith('.zip'):
            filename = 'gtfs.zip'

        if filename in used_names:
            digest = hashlib.sha256(url.encode()).hexdigest()[:8]
            path = Path(filename)
            filename = f'{path.stem}-{digest}{path.suffix}'
            counter = 2
            while filename in used_names:
                filename = f'{path.stem}-{digest}-{counter}{path.suffix}'
                counter += 1

        used_names.add(filename)
        return filename
