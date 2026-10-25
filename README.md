# Петербург в деталях — Backend

FastAPI-бэкенд пешеходного навигатора для знакомства с Санкт-Петербургом.

**Автор: Денис Аниськов.**

Клиентское приложение (Flutter, Yandex MapKit) публикуется отдельно:
[github.com/DenisAniskov/SPb_in_Details_public](https://github.com/DenisAniskov/SPb_in_Details_public).

> **Опубликованная ревизия и рабочее развёртывание.** Это публичный исходный
> релиз проверенного снимка кода. Он **не гарантирует идентичности** текущему
> рабочему развёртыванию проекта: рабочая служба развивается отдельно и может
> обслуживать другую ревизию. Настройки, ключи и данные рабочего развёртывания
> здесь не публикуются, и клонирование этого репозитория не даёт доступа к
> рабочей базе данных или административным операциям проекта.

## Что реализовано

| Компонент | Что делает | Endpoint |
| --- | --- | --- |
| Каталог мест | 180 проверенных записей Санкт-Петербурга с фильтрами и категориями | `GET /api/v1/places` |
| Исторические справки | 35 проверенных исторических карточек | `GET /api/v1/places/{id}/history` |
| Smart Walk | детерминированный локальный подбор прогулки по каталогу | `POST /api/v1/routes/propose-walk` |
| Shade Engine (C7.1) | геометрическая оценка тени на существующем маршруте с учётом времени | `POST /api/v1/environment/shade/analyze` |
| Puddle Engine (C7.2) | оценка риска мокрых участков по закреплённому OSM-снимку | `POST /api/v1/environment/puddle/analyze` |
| Pollen Engine (C7.3) | концентрации пыльцы CAMS (внешний источник — явный opt-in) | `POST /api/v1/environment/pollen/analyze` |
| Quiet Engine (C11) | относительный прокси дорожного шума (opt-in, read-only снимок) | `POST /api/v1/environment/quiet/analyze` |
| Внешний поиск | подсказки адресов и прямое/обратное геокодирование Яндекса при наличии ключей | `GET /api/v1/search/*` |
| ИИ-помощник (C13) | текстовый помощник строго на `:free`-моделях OpenRouter (нулевые цены проверяются, платных fallback нет), SSE-поток, durable-квоты и защитная блокировка при признаках платного использования; защищённый операторский сброс блокировки с обязательным подтверждением платных инцидентов; ограниченный переход на резервную бесплатную модель до первого SSE-события при явном включении | `POST /api/v1/assistant/chat`, `POST /api/v1/assistant/chat/stream`, операторский `POST /api/v1/assistant/kill-switch/reset` |

Все оценочные движки работают **по уже существующему маршруту**: сервис не
генерирует пешеходные маршруты, не заменяет MapKit и не выдаёт модельные
величины за измерения. Подробности — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
и [docs/API.md](docs/API.md).

## Требования

* Python 3.14.x (проверено на CPython 3.14.7; runtime-замыкание закреплено в
  `requirements.runtime.lock`).
* Компилятор не нужен: Linux/amd64 и Windows-колёса ставятся из wheel.
* SQLite доступен через `aiosqlite`; отдельный сервер БД не требуется.
* Один worker: каталог синхронизируется однократно при старте, а квоты
  учитываются в общей SQLite-базе.

## Локальный запуск

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt        # Windows: .venv\Scripts\python.exe
cp .env.example .env                                       # не перезаписывайте существующий .env
.venv/bin/python run.py
```

По умолчанию сервис слушает `0.0.0.0:8000` (локально доступен по `http://127.0.0.1:8000`), API — `/api/v1`,
интерактивная схема — `/docs` (публикуется только в `BACKEND_ENV=development`).
`/health` сообщает о работоспособности процесса, `/ready` — о готовности
локальных зависимостей и базы.

## Тесты

```bash
python -m pytest -q -ra
```

Полный набор backend-тестов выполняется без внешней сети: HTTP-вызовы к
Яндексу, OpenRouter и Open-Meteo подменяются тестовыми транспортами, а SQLite
работает в памяти или во временном каталоге. Репозиторий публикует зафиксированные данные вместе с их происхождением, поэтому часть тестов дополнительно пересчитывает датасеты и сверяет контрольные суммы. На ревизии этого релиза набор даёт **934 пройденных теста и 1 условный пропуск** (причина пропуска описана в [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md)).

Проверки, воспроизводимые локально (их же выполняет CI):

```bash
python -m compileall -q app scripts tools tests container_entrypoint.py run.py
python tools/verify_extraction.py             # происхождение импортированных файлов
python tools/scan_credentials.py              # скан секретов по worktree, индексу и истории
python -m scripts.validate_phase_c6_1_catalog
python -m app.db.validate_place_history
python -m scripts.acquire_phase_c7_1_osm --verify
python -m scripts.build_phase_c7_1_buildings --check-reproducible --out /tmp/rebuilt.sqlite --audit /tmp/rebuilt-audit.json
python tools/verify_osm_equivalence.py --original data/shade_buildings_spb_pilot.sqlite --rebuilt /tmp/rebuilt.sqlite
python docs/phase_c5_acquisition/build_scenic_dataset.py --check
```

## Docker

```bash
docker build --platform linux/amd64 -t smartnavigator-backend:local .
docker compose up -d --build
python scripts/container_smoke.py --image smartnavigator-backend:local
```

Образ собирается по hash-pinned `requirements.runtime.lock`, запускается от
UID/GID 10001, монтирует `/data` под записываемую SQLite-базу и объявляет
`HEALTHCHECK` на `/ready`. Контейнерный smoke-тест проверяет привилегии,
персистентность каталога и квот, онлайн-резервную копию, изолированное
восстановление и корректное завершение. Подробности — [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## API

| Метод | Путь | Назначение |
| --- | --- | --- |
| GET | `/health`, `/ready`, `/` | состояние процесса, готовность, корень API |
| GET | `/api/v1/places`, `/places/categories`, `/places/{id}`, `/places/{id}/history` | каталог и исторические справки |
| POST | `/api/v1/routes/propose-walk` | подбор прогулки по каталогу |
| POST | `/api/v1/routes/evaluate` | честный «не реализовано»: комплексная оценка маршрута ещё не построена |
| POST | `/api/v1/environment/{shade,puddle,pollen,quiet}/analyze` | оценка существующего маршрута |
| GET | `/api/v1/search/{suggest,geocode,reverse-geocode}` | внешний поиск Яндекса (нужны ключи) |
| POST | `/api/v1/assistant/chat`, `/assistant/chat/stream` | ИИ-помощник, обычный ответ и SSE |
| GET | `/api/v1/assistant/capabilities`, `/assistant/status` | публичная доступность и операторский статус |

Каждый endpoint ограничен по размеру входа, времени выполнения и параллелизму;
внешние источники (`puddle`-погода, `pollen`, `quiet`, `assistant`) по умолчанию
выключены и включаются только оператором. Полные контракты, коды ошибок и
примеры — [docs/API.md](docs/API.md).

## Переменные окружения

Все параметры читаются из окружения (переменные ОС важнее файла `.env`).
Безопасный шаблон без ключей — [.env.example](.env.example); полный разбор с
значениями по умолчанию и границами — [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

Ключевые группы: привязка (`HOST`, `PORT`, `WORKERS`, `DATABASE_URL`),
защита (`REQUEST_MAX_BYTES`, `QUERY_MAX_BYTES`, `SERVER_CONCURRENCY_LIMIT`,
`SEARCH_CLIENT_RATE_LIMIT`), внешний поиск (`GEOCODER_API_KEY`,
`SUGGEST_API_KEY`, суточные бюджеты), модели окружения (`SHADE_*`, `PUDDLE_*`,
`POLLEN_*`, `QUIET_*`) и ИИ-помощник (`AI_ASSISTANT_*`, `OPENROUTER_API_KEY`).

## Данные и лицензии

* Каталог, исторические справки и все геопространственные снимки происходят из
  [OpenStreetMap](https://www.openstreetmap.org/copyright): **© OpenStreetMap
  contributors**, лицензия **ODbL 1.0**. Уведомление и разрешение на
  переиспользование редакционных текстов — [app/db/README.md](app/db/README.md).
* Закреплённые снимки и их контрольные суммы опубликованы вместе с исходным
  кодом: датасет тени (1409 зданий в шести зонах), снимок дорог для оценки
  мокрых участков (1094 way), снимок дорог для Quiet Engine (3638 way),
  scenic-набор (187 записей).
* Внешние службы: подсказки и геокодирование Яндекса, Open-Meteo (погода и
  CAMS-пыльца), OpenRouter (free-модели). Условия и атрибуция —
  [docs/DATA_AND_ATTRIBUTIONS.md](docs/DATA_AND_ATTRIBUTIONS.md).

## Известные ограничения

Модельные движки — не измерительные приборы: они дают относительную оценку по
закреплённому снимку OSM, а не измерение реальных условий; Quiet Engine — прокси
дорожной близости, а не акустическое измерение в дБ. Пилотное покрытие ограничено
несколькими зонами Санкт-Петербурга; часть высот зданий оценена по этажности.
Рабочее развёртывание проекта здесь не описывается. Полный список —
[docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

## Безопасность

Сервис не хранит пользовательские данные и не пишет в логи координаты или
запросы. Он ограничивает размер тела и параметров, время чтения, параллелизм и
внешние вызовы, ведёт durable-учёт попыток обращения к внешним API и не отдаёт
ключи клиенту в ответах, ошибках и SSE. Перед публикацией репозиторий проходит
собственный скан секретов (`tools/scan_credentials.py`) по worktree, индексу и
всей истории. Детали и модель угроз — [docs/SECURITY.md](docs/SECURITY.md).

## Структура репозитория

```text
app/
  api/v1/endpoints/   HTTP-контракты (places, routes, search, environment, assistant)
  core/               настройки, ограничения запросов, защита внешних вызовов, логи
  db/                 SQLAlchemy-сессии, seed-каталог, исторические карточки
  models/ schemas/    ORM-модели и строгие Pydantic-контракты
  services/           Smart Walk, Shade, Puddle, Pollen, Quiet, поиск, ИИ-помощник
data/                 закреплённые ODbL-снимки (SQLite/JSON/gzip) и их манифесты
docs/                 архитектура, API, конфигурация, развёртывание, данные, юридика
scripts/              acquisition, сборка датасетов, валидаторы, backup, smoke-тесты
tests/                полный набор backend-тестов и фикстуры
tools/                происхождение, сканер секретов, генератор runtime-lock
Dockerfile compose.yaml container_entrypoint.py run.py
```

## Документация

* [Архитектура](docs/ARCHITECTURE.md) · [API](docs/API.md) · [Конфигурация](docs/CONFIGURATION.md)
* [Развёртывание](docs/DEPLOYMENT.md) · [Безопасность](docs/SECURITY.md)
* [Данные и атрибуция](docs/DATA_AND_ATTRIBUTIONS.md) · [Ограничения](docs/KNOWN_LIMITATIONS.md)
* [Лицензии и авторские права](docs/LICENSING.md) · [COPYRIGHT.md](COPYRIGHT.md)
* Модели и контракты по фазам: `docs/PHASE_C7_1_*`, `docs/PHASE_C7_2_*`,
  `docs/PHASE_C7_3_*`, `docs/PHASE_C11_*`, `docs/SMART_WALK_ALGORITHM.md`.

## Авторские права и лицензии

### Лицензия

Оригинальный исходный код backend'а распространяется по **MIT License**,
**Copyright (c) 2026 Denis Aniskov** — полный текст в [LICENSE](LICENSE),
границы действия и разбор правовых режимов — в [COPYRIGHT.md](COPYRIGHT.md) и
[docs/LICENSING.md](docs/LICENSING.md).

MIT касается только собственного кода. Геоданные OpenStreetMap остаются под
**ODbL 1.0 (© OpenStreetMap contributors)**; редакционные материалы каталога,
сторонние зависимости и внешние службы (Яндекс, Open-Meteo/CAMS, OpenRouter)
сохраняют свои условия в полном объёме; товарные знаки и фирменные элементы
по MIT не предоставляются.

## Исторические ссылки (архив)

Происхождение снимка зафиксировано в `docs/A5_EXTRACTION_MANIFEST.json`: код
выделен из монорепозитория `DenisAniskov/SmartNavigator_Spb` на ревизии
`f09219ae`. Монорепозиторий впоследствии разделён на публичный клиент
([SPb_in_Details_public](https://github.com/DenisAniskov/SPb_in_Details_public))
и этот backend; Git-история разработки в публичный репозиторий не переносилась.
Ссылки на исторические имена репозиториев и внутренние отчёты в сохранённых
provenance-файлах — архивные, а не действующие адреса.
