# Внешний поиск Яндекса: реализация и ограничения

Проверено по официальной документации 2026-10-03.

## HTTP контракты

- Suggest: `https://suggest-maps.yandex.ru/v1/suggest`; параметры `apikey`, `text`, `lang=ru`, `results=7`, `attrs=uri`, `print_address=1`, `ll=lon,lat`, `spn=width,height`. Документация задаёт язык ISO 639-1. Прежнее `ru_RU` заменено на `ru`.
- `strict_bounds=1` документирован для строгого окна. Без него окно рекомендательное. Код параметр не передаёт; утверждение, что он отправляет `strict_bounds=0`, удалено. Это не гарантия нахождения любого объекта за пределами города.
- `print_address=1` добавляет отдельное поле `address`, а не превращает `subtitle` в структурированный адрес. Наш контракт использует `title.text`, `subtitle.text`, `tags`, `uri`, `distance.text`; дополнительный `address` не выдаётся. Расстояние без `ull` относится к центру окна, не к текущему старту прогулки.
- Geocoder: `https://geocode-maps.yandex.ru/v1/`, `format=json`, `lang=ru_RU`, `results=1`. `uri` из Suggest передаётся вместо `geocode`. Если URI пуст, после выбора применяется текст с subtitle; совпадение в этом режиме может быть неоднозначным.
- Прямой поиск передаёт `geocode=text`, рекомендательное окно СПб и `rspn=0`. Reverse передаёт `geocode=lon,lat`. В `Point.pos` порядок — `lon lat`; свои модели используют явные latitude/longitude и проверяют конечность/диапазоны. Reverse сохраняет исходные координаты тапа, а не переносит старт к найденному дому.

Источники: [Suggest request](https://yandex.ru/maps-api/docs/suggest-api/request.html), [Suggest response](https://yandex.ru/maps-api/docs/suggest-api/response.html), [Geocoder request](https://yandex.ru/maps-api/docs/geocoder-api/request.html).

## Backend

Три endpoint: `/api/v1/search/suggest`, `/geocode`, `/reverse-geocode`. Параметры ограничены по длине, числу результатов и диапазонам координат. Пустой Suggest не расходует квоту; геокодирование пустых text/uri отклоняется. Собственные response models не выдают сырые upstream bodies.

Один AsyncClient на lifecycle процесса; timeout 5 с, connect 3 с. Клиент закрывается при shutdown, включая ошибку освобождения DB. Auth 401/403 → 503; 429 → 429; upstream/network/malformed JSON → 502; timeout → 504; корректный пустой Geocoder → 404. Заголовок Referer настраивается через YANDEX_HTTP_REFERER; перед публикацией сопоставьте его с реальным идентификатором приложения.

HTTPX INFO-логирование маскирует apikey; HTTP core debug выключен. Ключи берутся только из backend environment/.env. Нельзя включать wire logging или публиковать локальные .env и build define files.

## Кэш, тариф и лицензия

П. 5.1.5 [условий](https://yandex.ru/legal/maps_api/ru/) допускает временный кэш до 30 дней только для предусмотренных целей и использования. Это не безусловное разрешение любой интеграции. П. 6.6.1 ограничивает бесплатный HTTP Geocoder JavaScript API; разрешение его сочетания с MapKit здесь не подтверждено. Нужно письменное согласование или применимые отдельные условия. TTL-кэш результатов отключён; одинаковые запросы объединяются лишь пока выполняются, результаты после завершения не сохраняются. Лимиты: Suggest 1000/сутки, Geocoder 1000/сутки. Нет гарантии соблюдения общей квоты несколькими клиентами.

## Flutter и маршрутизация

Один search field отправляет локальный запрос и Suggest отдельно. Минимум Suggest — 2 символа, debounce 350 мс. Повтор того же текста не создаёт новый вызов. Stale Suggest/geocode результаты игнорируются; очистка, локальный выбор и dispose инвалидируют внешние запросы. Ошибка внешнего поиска не удаляет каталог SmartNavigator. Результаты имеют отдельную атрибуцию Яндекса; доступны ссылки на условия и «Открыть в Картах».

ExternalMapPoint не превращается в PlaceModel и не содержит туристических полей. Координаты выбранной точки переходят в RoutePointModel и далее в реальные RequestPoint SDK. Поддерживаются локальные/внешние старты и назначения. Geocode выполняется при выборе, reverse — только при тапе для выбора старта. Координаты устанавливаются сразу; revision старта защищает от позднего переименования другой точки.

[PedestrianRouter](https://yandex.ru/maps-api/docs/mapkit/com/yandex/mapkit/transport/masstransit/PedestrianRouter.html) документированно поддерживает две точки в requestRoutes; это также указано в исходнике Flutter 4.45.0. Рабочая прогулка сохраняет последовательные SDK legs; TSP и автоматический подбор не добавляются.

Модульные тесты используют тестовые HTTP/роутер adapters и не доказывают работу native MapKit. Фактические команды и живой smoke в этот публичный исходный релиз не входят.
