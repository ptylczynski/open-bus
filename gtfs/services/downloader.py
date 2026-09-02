import hashlib
import logging
import os
import shutil
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from django.conf import settings

from gtfs.services.importer import GtfsImportService


logger = logging.getLogger(__name__)


class GtfsImporter(Protocol):
    def load(self, archive_path: Path) -> dict[str, int]: ...


class GtfsDownloadBatchError(Exception):
    def __init__(
        self,
        errors: list[tuple[str, Exception]],
        downloaded: list[Path],
    ) -> None:
        self.errors = errors
        self.downloaded = downloaded
        failed_urls = ', '.join(url for url, _error in errors)
        super().__init__(f'Failed to download GTFS feeds: {failed_urls}')


class GtfsDownloadService:
    def __init__(
        self,
        urls: Iterable[str] | None = None,
        destination: str | os.PathLike[str] | None = None,
        interval_seconds: int | None = None,
        timeout_seconds: int | None = None,
        importer: GtfsImporter | None = None,
    ) -> None:
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
        self.importer = GtfsImportService() if importer is None else importer

        if self.interval_seconds <= 0:
            raise ValueError('GTFS_DOWNLOAD_INTERVAL_SECONDS must be greater than 0')
        if self.timeout_seconds <= 0:
            raise ValueError('GTFS_DOWNLOAD_TIMEOUT_SECONDS must be greater than 0')

    def download_all(self) -> list[Path]:
        self.destination.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        errors: list[tuple[str, Exception]] = []
        used_names: set[str] = set()
        unchanged = 0
        total = len(self.urls)

        logger.info(
            'Starting GTFS batch with %d feed(s); destination=%s',
            total,
            self.destination,
        )

        for index, url in enumerate(self.urls, start=1):
            logger.info('Processing GTFS feed %d/%d: %s', index, total, url)
            try:
                path = self._download(url, used_names)
            except Exception as error:
                logger.exception(
                    'GTFS feed %d/%d failed: %s',
                    index,
                    total,
                    url,
                )
                errors.append((url, error))
            else:
                if path is None:
                    logger.info('GTFS feed from %s is unchanged; skipping', url)
                    unchanged += 1
                    continue
                logger.info(
                    'Downloaded and imported GTFS feed from %s to %s',
                    url,
                    path,
                )
                downloaded.append(path)

        if errors:
            logger.error(
                'GTFS batch failed: %d imported, %d unchanged, %d failed; '
                'old archives retained',
                len(downloaded),
                unchanged,
                len(errors),
            )
            raise GtfsDownloadBatchError(errors, downloaded)

        removed = 0
        if downloaded:
            removed = self._remove_old_archives(used_names)

        logger.info(
            'GTFS batch complete: %d imported, %d unchanged, %d old archive(s) '
            'removed',
            len(downloaded),
            unchanged,
            removed,
        )

        return downloaded

    def run_forever(self) -> None:
        while True:
            try:
                self.download_all()
            except GtfsDownloadBatchError:
                logger.warning('GTFS batch will be retried after the configured delay')
            logger.info(
                'Waiting %d seconds before the next GTFS batch',
                self.interval_seconds,
            )
            time.sleep(self.interval_seconds)

    def _download(self, url: str, used_names: set[str]) -> Path | None:
        filename = self._filename_for(url, used_names)
        destination = self.destination / filename
        request = urllib.request.Request(
            url,
            headers={'User-Agent': 'open-bus GTFS downloader'},
        )
        temporary_path = None

        try:
            logger.info('Downloading GTFS archive from %s', url)
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

            logger.info(
                'Downloaded %d bytes from %s; validating archive',
                temporary_path.stat().st_size,
                url,
            )
            if not zipfile.is_zipfile(temporary_path):
                raise ValueError(f'{url} did not return a valid ZIP file')

            downloaded_hash = self._file_hash(temporary_path)
            if (
                destination.exists()
                and self._file_hash(destination) == downloaded_hash
            ):
                return None

            logger.info('Importing changed GTFS archive from %s', url)
            self.importer.load(temporary_path)
            os.replace(temporary_path, destination)
            return destination
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _file_hash(path: Path) -> bytes:
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.digest()

    def _remove_old_archives(self, current_names: set[str]) -> int:
        removed = 0
        for path in self.destination.iterdir():
            if (
                path.is_file()
                and path.name not in current_names
                and path.suffix.lower() == '.zip'
            ):
                path.unlink()
                removed += 1
                logger.info('Removed old GTFS archive %s', path)
        return removed

    @staticmethod
    def _filename_for(url: str, used_names: set[str]) -> str:
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
