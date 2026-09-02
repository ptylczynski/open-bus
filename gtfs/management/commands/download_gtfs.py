from django.core.management.base import BaseCommand, CommandError

from gtfs.services import GtfsDownloadBatchError, GtfsDownloadService


class Command(BaseCommand):
    help = 'Periodically download the configured GTFS ZIP files'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once',
            action='store_true',
            help='Download all feeds once instead of running continuously',
        )

    def handle(self, *args, **options):
        service = GtfsDownloadService()
        if not service.urls:
            raise CommandError('GTFS_URLS does not contain any feed URLs')

        if not options['once']:
            self.stdout.write(
                f'Downloading {len(service.urls)} GTFS feed(s) every '
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
                self.stdout.write(self.style.SUCCESS(f'Downloaded {path}'))
            details = '; '.join(
                f'{url}: {download_error}'
                for url, download_error in error.errors
            )
            raise CommandError(details) from error

        for path in downloaded:
            self.stdout.write(self.style.SUCCESS(f'Downloaded {path}'))
