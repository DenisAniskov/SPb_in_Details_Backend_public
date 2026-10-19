# Конфигурация

Все параметры читаются из переменных окружения; переменная окружения имеет
приоритет над файлом `.env` в рабочем каталоге. Шаблон без ключей —
[`../.env.example`](../.env.example). Неизвестные переменные игнорируются,
некорректные значения останавливают старт с понятной ошибкой, а секреты
никогда не попадают в `repr` настроек и в текст ошибки валидации.

## Привязка и режим

| Переменная | По умолчанию | Значение |
| --- | --- | --- |
| `BACKEND_ENV` | `development` | `development` / `staging` / `production` |
| `HOST`, `PORT` | `0.0.0.0`, `8000` | адрес и порт процесса |
| `WORKERS` | `1` | допустимо только `1`: каталог и квоты — один писатель |
| `RELOAD` | `false` | автоперезапуск только в `development` |
| `LOG_LEVEL` | `info` | `critical`…`debug` |
| `ALLOWED_ORIGINS` | `[]` | точные HTTP(S)-источники для браузерных клиентов |
| `TRUSTED_PROXY_IPS` | пусто | явные IP или ограниченные CIDR обратного прокси; `0.0.0.0/0` и `::/0` запрещены |
| `DATABASE_URL` | `sqlite+aiosqlite:///./smart_navigator_spb.db` | только файл SQLite через `aiosqlite`; в `staging`/`production` `:memory:` запрещён |

## Защита запросов

| Переменная | По умолчанию | Значение |
| --- | --- | --- |
| `REQUEST_MAX_BYTES` | `16384` | максимальный размер тела (1 КиБ … 1 МиБ) |
| `REQUEST_BODY_TIMEOUT_SECONDS` | `10` | таймаут чтения тела |
| `QUERY_MAX_BYTES` | `2048` | максимальная строка параметров |
| `SERVER_CONCURRENCY_LIMIT` | `100` | лимит одновременных соединений uvicorn |
| `SEARCH_CLIENT_RATE_LIMIT` | `30` | анонимный лимит поисковых запросов на адрес |
| `SEARCH_CLIENT_WINDOW_SECONDS` | `60` | окно лимита |
| `SEARCH_CLIENT_MAX_ENTRIES` | `4096` | максимум отслеживаемых адресов |
| `READINESS_TIMEOUT_SECONDS` | `2` | таймаут проверки готовности в `/ready` |

## Внешний поиск (Яндекс)

| Переменная | По умолчанию | Значение |
| --- | --- | --- |
| `SUGGEST_API_KEY`, `GEOCODER_API_KEY` | пусто | серверные ключи; пустое значение оставляет каталог и Smart Walk полностью рабочими |
| `YANDEX_SUGGEST_URL`, `YANDEX_GEOCODER_URL` | официальные HTTPS endpoint'ы | домен и путь проверяются, подмена хоста невозможна |
| `YANDEX_HTTP_REFERER` | `http://ru.smartnavigator.spb.ymapapp` | значение заголовка `Referer` для серверных запросов |
| `YANDEX_TIMEOUT_SECONDS`, `YANDEX_CONNECT_TIMEOUT_SECONDS` | `5`, `3` | таймауты |
| `YANDEX_MAX_CONCURRENT` | `4` | одновременные исходящие вызовы в процессе |
| `YANDEX_SERVER_RATE_LIMIT`, `YANDEX_RATE_WINDOW_SECONDS` | `60`, `60` | серверный лимит попыток |
| `YANDEX_SUGGEST_DAILY_BUDGET`, `YANDEX_GEOCODER_DAILY_BUDGET` | `800`, `800` | скользящие 24 часа; `0` отключает продукт |

Ключи `MAPKIT_API_KEY` (устаревший, не используется) и любые значения вида
`YOUR_*`, `REPLACE_*`, `PLACEHOLDER*` трактуются как «не настроено».

## Модельные движки и датасеты

| Переменная | По умолчанию | Значение |
| --- | --- | --- |
| `SHADE_BUILDING_DATASET` | `data/shade_buildings_spb_pilot.sqlite` | датасет зданий; пусто — движок честно недоступен |
| `SHADE_MAX_CONCURRENT_ANALYSES`, `SHADE_ANALYSIS_TIMEOUT_SECONDS` | `2`, `10` | семафор и таймаут GEOS-работы |
| `PUDDLE_WAYS_DATASET` | `data/puddle_ways_spb_pilot.json` | снимок дорог для оценки мокрых участков |
| `PUDDLE_MAX_CONCURRENT_ANALYSES`, `PUDDLE_ANALYSIS_TIMEOUT_SECONDS` | `2`, `10` | семафор и таймаут |
| `PUDDLE_WEATHER_ENABLED` | `false` | явный opt-in модельной погоды Open-Meteo |
| `PUDDLE_WEATHER_TIMEOUT_SECONDS` | `6` | таймаут запроса погоды |
| `PUDDLE_WEATHER_LATITUDE`, `PUDDLE_WEATHER_LONGITUDE` | `59.9405`, `30.3165` | фиксированный центроид: геометрия пользователя вверх не уходит |
| `POLLEN_SOURCE_ENABLED` | `false` | явный opt-in источника CAMS через Open-Meteo Air Quality |
| `POLLEN_SOURCE_TIMEOUT_SECONDS` | `10` | таймаут источника |
| `POLLEN_SOURCE_LATITUDE`, `POLLEN_SOURCE_LONGITUDE` | `59.9405`, `30.3165` | ячейка 0.1° вокруг фиксированной точки |
| `POLLEN_MAX_CONCURRENT_ANALYSES`, `POLLEN_ANALYSIS_TIMEOUT_SECONDS` | `2`, `10` | семафор и таймаут |
| `QUIET_ENGINE_ENABLED` | `false` | включает Quiet Engine только вместе с проверенным gzip-датасетом |
| `QUIET_DATASET`, `QUIET_DATASET_MANIFEST` | `data/quiet_spb_pilot.json.gz`, `data/quiet_spb_pilot.manifest.json` | снимок дорог и его SHA-256-манифест |
| `QUIET_MAX_CONCURRENT_ANALYSES`, `QUIET_ANALYSIS_TIMEOUT_SECONDS` | `2`, `10` | семафор и таймаут |
| `SPB_CENTER_LON`, `SPB_CENTER_LAT`, `SPB_SPN_LON`, `SPB_SPN_LAT` | `30.3146`, `59.9398`, `0.9`, `0.7` | центр (`ll`) и охват (`spn`) области поиска по умолчанию |

Источники `PUDDLE_WEATHER_ENABLED`, `POLLEN_SOURCE_ENABLED` и `QUIET_ENGINE_ENABLED`
выключены по умолчанию, потому что условия поставщиков (некоммерческий
бесплатный тариф, обязательная атрибуция) требуют осознанного решения
оператора. См. [DATA_AND_ATTRIBUTIONS.md](DATA_AND_ATTRIBUTIONS.md).

## ИИ-помощник (C13)

| Переменная | По умолчанию | Значение |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | пусто | серверный ключ; без него помощник выключен |
| `AI_ASSISTANT_ENABLED` | `false` | включает помощника |
| `AI_ASSISTANT_ACCESS_MODE` | `private_test` | `private_test` / `guest_limited` / `disabled` |
| `AI_ASSISTANT_ACCESS_CODE_HASH` | пусто | SHA-256 операторского кода; нужен всегда, пока помощник включён |
| `AI_ASSISTANT_SOURCE_SALT` | пусто | соль для псевдонимов источников; обязательна в `guest_limited` |
| `AI_ASSISTANT_FREE_ONLY` | `true` | **нельзя** отключить: платный роутинг запрещён конфигурацией |
| `AI_ASSISTANT_DATA_COLLECTION` | `deny` | `allow` требует явного `AI_ASSISTANT_DATA_COLLECTION_ACK=true` |
| `AI_ASSISTANT_MODEL_PRIMARY`, `AI_ASSISTANT_MODEL_RESERVE` | free-модели OpenRouter | идентификаторы обязаны заканчиваться на `:free` |
| `AI_ASSISTANT_MAX_*` | сообщение 800, история 8 сообщений / 4000 символов, ответ 8000 символов | границы входа и выхода |
| `AI_ASSISTANT_STREAMING_ENABLED` | `true` | SSE-поток; выключение не ломает обычный `/chat` |
| `AI_ASSISTANT_GLOBAL_PER_MINUTE`, `AI_ASSISTANT_GLOBAL_PER_DAY` | `8`, `100` | глобальные квоты |
| `AI_ASSISTANT_SOURCE_PER_MINUTE`, `AI_ASSISTANT_SOURCE_PER_DAY` | `3`, `20` | квоты на источник |
| `AI_ASSISTANT_MAX_CONCURRENT` | `2` | одновременные обращения к модели |
| `AI_ASSISTANT_TRUSTED_PROXY_HOPS` | `2` | сколько хопов `X-Forwarded-For` считать доверенными |
| `AI_ASSISTANT_BACKUP_ON_START` | `false` | онлайн-копия базы до миграции схемы |
| `AI_ASSISTANT_EXPORT_ENABLED`, `AI_ASSISTANT_EXPORT_TOKEN_HASH` | `false`, пусто | разовая выгрузка проверенной копии |

Значения `AI_ASSISTANT_ACCESS_CODE_HASH`, `AI_ASSISTANT_SOURCE_SALT` и
`AI_ASSISTANT_EXPORT_TOKEN_HASH` принимаются только как lowercase SHA-256: сам
секрет в конфигурации не хранится.
