import logging
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from gtfs.services import GtfsDownloadBatchError, GtfsDownloadService


class Command(BaseCommand):
    help = 'Periodically download and import the configured GTFS ZIP files'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            '--once',
            action='store_true',
            help='Download and import all feeds once instead of running continuously',
        )

    def handle(self, *args: Any, **options: Any) -> None:
        service = GtfsDownloadService()
        if not service.urls:
            raise CommandError('GTFS_URLS does not contain any feed URLs')

        if not options['once']:
            self.stdout.write(
                f'Downloading and importing {len(service.urls)} GTFS feed(s) every '
                f'{service.interval_seconds} seconds'
            )
            try:
                service.run_forever()
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING('GTFS downloader stopped'))
            return

        try:
            downloaded = service.download_all()
        except GtfsDownloadBatchError as error:
            for path in error.downloaded:
                self.stdout.write(self.style.SUCCESS(f'Imported {path}'))
            details = '; '.join(
                f'{url}: {download_error}'
                for url, download_error in error.errors
            )
            raise CommandError(details) from error

        for path in downloaded:
            self.stdout.write(self.style.SUCCESS(f'Imported {path}'))
