# Phase C6 — API-интеграция исторического контента

## Endpoint

```
GET /api/v1/places/{place_id}/history
```

Ответ `200 OK`:

```json
{
  "placeId": 1,
  "catalogKey": "hermitage-winter-palace",
  "history": {
    "catalogKey": "hermitage-winter-palace",
    "locale": "ru",
    "shortHistoricalSummary": "…",
    "detailedHistoricalText": "…",
    "period": "1754–1762 (постройка современного здания)",
    "timeline": [ { "id", "dateLabel", "datePrecision", "sortKey", "title", "description", "sourceIds" } ],
    "people":   [ { "name", "role", "description", "sourceIds" } ],
    "facts":    [ { "id", "title", "text", "category", "sourceIds" } ],
    "sources":  [ { "id", "title", "url", "publisher", "sourceType", "accessedOn", "license", "notes" } ],
    "editorialStatus": "verified",
    "lastVerifiedOn": "2026-10-11",
    "contentVersion": 1
  }
}
```

- `history: null` — у места нет проверенной карточки (нормальный ответ).
- `404` — место не существует (как у `GET /places/{id}`).

## Обратная совместимость

- Схемы `/places` и `/places/{id}` не изменились: список не содержит
  исторических полей, payload листовых ответов не растёт (тесты
  `test_places_pagination_is_untouched`,
  `test_place_detail_response_shape_is_backward_compatible`).
- Сетевые запросы к историческим сайтам при получении карточки отсутствуют
  (тест перехватывает `socket.connect`).
- Набор читается из кеша один раз за процесс (`lru_cache`); повторный вызов
  загрузчика не обращается к диску (тест `test_loader_is_cached_and_reads_file_once`).

## Клиентский слой (Flutter)

- `PlacesRepository.getPlaceHistory(placeId)` — возвращает
  `HistoricalContent?` или бросает исключение (транспорт/форма), чтобы
  провайдер отличал «нет истории» от «история недоступна».
- `HistoricalContentProvider` — сессионный кеш по `place.id`:
  - одна карточка = один запрос; подтверждённое отсутствие кешируется
    (negative cache);
  - у каждого запроса per-place токен: поздний ответ не перезаписывает более
    новый (тест stale-response guard);
  - сбой — мягкий флаг `hasFailed(placeId)` с явной кнопкой «Повторить»;
    перестройка экрана не превращает один сбой в цикл запросов
    (регрессия, пойманная тестом).
- Провайдер регистрируется в `main.dart` после `PlacesProvider`.
- Все четыре точки UI (`PlaceDetailScreen`, `RouteDetailsSheet`,
  `SmartWalkWizardSheet`, `LiveNavigationSheet`) читают провайдер как
  **опциональный** (`watch<HistoricalContentProvider?>`): исторический слой
  не является жёсткой зависимостью экранов и при его отсутствии интерфейс
  полностью совпадает с прежним.

## Безопасность

- URLs источников открываются только внешним браузером
  (`launchUrl(externalApplication)`) — тот же механизм, что и для
  `official_website`; схема ограничена http(s) ещё на бэкенде.
- Никаких произвольных загрузок по URL из набора клиент не выполняет.
