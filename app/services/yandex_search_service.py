import asyncio
import logging
import re
from typing import Any, Optional
import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from app.core.config import settings
from app.core.upstream_controls import UpstreamControls, upstream_controls
from app.schemas.search import GeocodeResultResponse, ReverseGeocodeResponse, SuggestItemResponse

class _RedactApiKey(logging.Filter):
    @staticmethod
    def _redact(message):
        # Query inputs include reverse-geocoded GPS coordinates and credentials.
        return re.sub(r'([?&][^=&\s"\']+=)[^&\s"\']+', r'\1***', message)

    def filter(self, record):
        if record.name == 'uvicorn.access' and isinstance(record.args, tuple) and len(record.args) == 5:
            # Uvicorn's AccessFormatter expects this five-item tuple; preserve it.
            args = list(record.args)
            args[2] = self._redact(str(args[2]))
            record.args = tuple(args)
        else:
            record.msg = self._redact(record.getMessage())
            record.args = ()
        return True

# Keep endpoint/status diagnostics without retaining query secrets or GPS inputs.
logging.getLogger('httpx').addFilter(_RedactApiKey())
logging.getLogger('uvicorn.access').addFilter(_RedactApiKey())
logging.getLogger('httpcore').setLevel(logging.WARNING)

def _object(value):
    if not isinstance(value, dict): raise ValueError('Expected object')
    return value

def _array(value):
    if not isinstance(value, list): raise ValueError('Expected array')
    return value

def _text(value, default=None):
    if value is None: return default
    if not isinstance(value, str): raise ValueError('Expected text')
    return value or default

class YandexSearchService:
    """One lifecycle HTTP client; deduplicate concurrent requests, never cache results."""
    def __init__(self, controls: UpstreamControls | None = None):
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._waiters: dict[tuple, int] = {}
        self._closing = False
        self._controls = controls or upstream_controls

    @staticmethod
    def _shutting_down() -> HTTPException:
        return HTTPException(503, 'Сервис поиска останавливается.')

    async def get_client(self):
        if self._closing:
            # Shutdown has begun: never hand out the client again, so close()
            # cannot race a fresh client into existence behind its back.
            raise self._shutting_down()
        if self._client is None or self._client.is_closed:
            async with self._client_lock:
                if self._closing:
                    raise self._shutting_down()
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(timeout=httpx.Timeout(
                        settings.YANDEX_TIMEOUT_SECONDS,
                        connect=settings.YANDEX_CONNECT_TIMEOUT_SECONDS),
                        trust_env=False, follow_redirects=False, headers={
                        'Accept': 'application/json', 'Referer': settings.YANDEX_HTTP_REFERER,
                    })
        return self._client

    async def close(self):
        self._closing = True
        # Registration (_request) and teardown are mutually exclusive: anything
        # registered before this critical section is in the cancellation
        # snapshot, anything arriving after it observes `_closing`. Tasks stuck
        # waiting for this lock were cancelled below before the gather, so the
        # await cannot deadlock.
        async with self._client_lock:
            tasks = list(self._inflight.values())
            for task in tasks: task.cancel()
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)
            self._inflight.clear()
            self._waiters.clear()
            client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _request(self, url, params, product):
        async with self._client_lock:
            if self._closing:
                raise self._shutting_down()
            key = (url, tuple(sorted((k, str(v)) for k, v in params.items() if k != 'apikey')))
            task = self._inflight.get(key)
            if task is None:
                admission = self._controls.acquire()
                try:
                    task = asyncio.create_task(self._perform_request(url, params, product))
                except BaseException:
                    admission.release()
                    raise
                task.add_done_callback(lambda completed: admission.release())
                self._inflight[key] = task
            self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            return await asyncio.shield(task)
        finally:
            remaining = self._waiters.get(key, 1) - 1
            if remaining:
                self._waiters[key] = remaining
            else:
                self._waiters.pop(key, None)
                if self._inflight.get(key) is task: self._inflight.pop(key, None)
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def _perform_request(self, url, params, product):
        client = await self.get_client()
        await self._controls.reserve(product)
        try:
            response = await client.get(url, params=params)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise HTTPException(504, 'Время ожидания ответа сервиса поиска истекло.') from None
        except httpx.RequestError:
            raise HTTPException(502, 'Не удалось связаться с сервисом поиска.') from None
        if response.status_code in (401, 403):
            raise HTTPException(503, 'Сервис поиска временно недоступен из-за ошибки авторизации.')
        if response.status_code == 429:
            raise HTTPException(429, 'Превышен лимит запросов сервиса поиска. Повторите попытку позже.',
                                headers={'Retry-After': str(settings.YANDEX_RATE_WINDOW_SECONDS)})
        if not 200 <= response.status_code < 300:
            raise HTTPException(502, 'Внешний сервис поиска временно вернул ошибку.')
        try:
            return _object(response.json())
        except (ValueError, TypeError):
            raise self._malformed() from None

    @staticmethod
    def _malformed():
        return HTTPException(502, 'Некорректный формат ответа сервиса поиска.')

    @staticmethod
    def _key(name):
        key = getattr(settings, name)
        if not key or not key.strip():
            raise HTTPException(503, 'Сервис поиска временно недоступен (не настроен ключ API).')
        return key

    async def get_suggestions(self, query: str, results_limit: int = 7):
        query = query.strip()
        if not query: return []
        data = await self._request(settings.YANDEX_SUGGEST_URL, {
            'apikey': self._key('SUGGEST_API_KEY'), 'text': query,
            'lang': 'ru', 'results': min(max(results_limit, 1), 10),
            'attrs': 'uri', 'print_address': 1,
            # No strict_bounds: the documented default window is advisory.
            'll': f'{settings.SPB_CENTER_LON},{settings.SPB_CENTER_LAT}',
            'spn': f'{settings.SPB_SPN_LON},{settings.SPB_SPN_LAT}',
        }, 'suggest')
        try:
            items = []
            for raw in _array(data['results']):
                raw = _object(raw)
                title = _text(_object(raw['title'])['text'])
                if not title: continue
                subtitle = _text(_object(raw.get('subtitle') or {}).get('text'))
                distance = _text(_object(raw.get('distance') or {}).get('text'))
                tags = _array(raw.get('tags', []))
                if any(not isinstance(tag, str) for tag in tags): raise ValueError('Invalid tags')
                items.append(SuggestItemResponse(title=title, subtitle=subtitle, tags=tags,
                    uri=_text(raw.get('uri')), distance_text=distance))
            return items
        except (KeyError, ValueError, TypeError, ValidationError):
            raise self._malformed() from None

    def _geo_object(self, data):
        try:
            collection = _object(_object(data['response'])['GeoObjectCollection'])
            members = _array(collection['featureMember'])
            if not members: raise HTTPException(404, 'Координаты или адрес указанного объекта не найдены.')
            return _object(_object(members[0])['GeoObject'])
        except (KeyError, ValueError, TypeError):
            raise self._malformed() from None

    async def geocode_object(self, text=None, uri=None):
        text = text.strip() if text else None
        uri = uri.strip() if uri else None
        if not text and not uri: raise HTTPException(400, 'Необходимо указать поисковый текст или URI объекта.')
        params: dict[str, Any] = {'apikey': self._key('GEOCODER_API_KEY'), 'format': 'json', 'lang': 'ru_RU', 'results': 1}
        if uri: params['uri'] = uri
        else:
            params.update(geocode=text, ll=f'{settings.SPB_CENTER_LON},{settings.SPB_CENTER_LAT}',
                spn=f'{settings.SPB_SPN_LON},{settings.SPB_SPN_LAT}', rspn=0)
        geo = self._geo_object(await self._request(settings.YANDEX_GEOCODER_URL, params, 'geocoder'))
        try:
            parts = _text(_object(geo['Point'])['pos']).split()
            if len(parts) != 2: raise ValueError('Invalid position')
            lon, lat = map(float, parts)
            meta = _object(_object(geo.get('metaDataProperty', {})).get('GeocoderMetaData', {}))
            return GeocodeResultResponse(title=_text(geo.get('name'), text or 'Выбранный объект'),
                subtitle=_text(geo.get('description')), address=_text(meta.get('text')),
                latitude=lat, longitude=lon, uri=uri, kind=_text(meta.get('kind')))
        except (KeyError, ValueError, TypeError, AttributeError, ValidationError):
            raise self._malformed() from None

    async def reverse_geocode(self, latitude, longitude):
        geo = self._geo_object(await self._request(settings.YANDEX_GEOCODER_URL, {
            'apikey': self._key('GEOCODER_API_KEY'), 'format': 'json', 'lang': 'ru_RU',
            'geocode': f'{longitude},{latitude}', 'results': 1,
        }, 'geocoder'))
        try:
            name = _text(geo.get('name'), 'Точка на карте')
            meta = _object(_object(geo.get('metaDataProperty', {})).get('GeocoderMetaData', {}))
            return ReverseGeocodeResponse(title=name, address=_text(meta.get('text'), name),
                subtitle=_text(geo.get('description')), latitude=latitude, longitude=longitude)
        except (ValueError, TypeError, ValidationError):
            raise self._malformed() from None

yandex_search_service = YandexSearchService()
