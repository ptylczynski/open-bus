import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from django.conf import settings


HERE_AUTOSUGGEST_URL = (
    'https://autosuggest.search.hereapi.com/v1/autosuggest'
)


class HereAutosuggestError(Exception):
    """Raised when HERE cannot provide autosuggestions."""


class HereAutosuggestConfigurationError(HereAutosuggestError):
    """Raised when HERE autosuggest is not configured."""


class HereAutosuggestService:
    def __init__(
        self,
        api_key: str | None = None,
        limit: int | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.api_key = settings.HERE_API_KEY if api_key is None else api_key
        self.limit = (
            settings.HERE_AUTOSUGGEST_LIMIT if limit is None else limit
        )
        self.timeout_seconds = (
            settings.HERE_AUTOSUGGEST_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )

    def suggest(self, text: str) -> list[dict[str, object]]:
        if not self.api_key:
            raise HereAutosuggestConfigurationError(
                'HERE API key is not configured.',
            )

        query = urlencode(
            {
                'apiKey': self.api_key,
                'in': settings.HERE_AUTOSUGGEST_BOUNDING_BOX,
                'limit': self.limit,
                'q': text,
                'termsLimit': 0,
            },
        )
        request = urllib.request.Request(
            f'{HERE_AUTOSUGGEST_URL}?{query}',
            headers={
                'Accept': 'application/json',
                'User-Agent': 'open-bus geocoder',
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                payload = json.load(response)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as error:
            raise HereAutosuggestError(
                'HERE autosuggest request failed.',
            ) from error

        return self._suggestions_from(payload)

    def _suggestions_from(
        self,
        payload: Any,
    ) -> list[dict[str, object]]:
        if not isinstance(payload, dict) or not isinstance(
            payload.get('items'),
            list,
        ):
            raise HereAutosuggestError(
                'HERE autosuggest returned an invalid response.',
            )

        suggestions = []
        for item in payload['items']:
            suggestion = self._suggestion_from(item)
            if suggestion is not None:
                suggestions.append(suggestion)
            if len(suggestions) == self.limit:
                break
        return suggestions

    @staticmethod
    def _suggestion_from(item: object) -> dict[str, object] | None:
        if not isinstance(item, dict):
            return None
        name = item.get('title')
        position = item.get('position')
        if not isinstance(name, str) or not isinstance(position, dict):
            return None
        latitude = position.get('lat')
        longitude = position.get('lng')
        if (
            not isinstance(latitude, (int, float))
            or isinstance(latitude, bool)
            or not isinstance(longitude, (int, float))
            or isinstance(longitude, bool)
        ):
            return None
        return {
            'name': name,
            'coordinates': {
                'lat': latitude,
                'lng': longitude,
            },
        }
