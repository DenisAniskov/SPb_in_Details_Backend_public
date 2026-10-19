# Phase C7.3 — API: `POST /api/v1/environment/pollen/analyze`

Отдельный эндпоинт с отдельными схемами (`app/schemas/pollen.py`).
Контракт по стилю следует Shade/Puddle, но **не имеет** полей риска:
`risk_level`, `risk_index`, `low/moderate/high_meters` здесь отсутствуют
намеренно (ТЗ §4: порогов для пыльцы нет).

Эндпоинт **не генерирует и не меняет маршрут**, не ходит в native routing и
не отправляет геометрию пользователя наружу.

## Запрос

```jsonc
{
  "departure_at": "2026-07-17T14:00:00+03:00",   // обязателен TZ
  "legs": [
    {
      "geometry": [{"latitude": 59.94, "longitude": 30.31}, ...],
      "walking_duration_seconds": 1800,
      "visit_duration_seconds_after_leg": 900     // осмотр POI после этапа
    }
  ],
  "include_legs": false        // необязательная по-этапная разбивка
}
```

Лимиты (те же, что у shade/puddle, чтобы клиент переиспользовал валидацию):
1…24 этапа, 2…2000 точек на этап, ≤ 5000 точек, ≤ 25 км, ≤ 6 ч ходьбы на
этап, ≤ 24 ч осмотра, суммарное время ≤ 24 ч, `extra="forbid"`.

## Ответ (сокращённо)

```jsonc
{
  "status": "available",           // available | partial | unavailable | outside_coverage
  "method": "experimental_pollen_exposure",
  "method_version": "c7_3.1",
  "estimate": true,
  "confidence": "low",             // low | unavailable — «высокой» не бывает
  "evaluated_period": {"start": "...", "end": "...", "timezone": "+03:00"},
  "route_length_meters": 1234.5,
  "known_meters": 1234.5,
  "unknown_meters": 0.0,
  "coverage_share": 1.0,
  "walking_duration_seconds": 3240.0,
  "elapsed_duration_seconds": 4140.0,
  "concentration_unit": "grains/m³",
  "species": [
    {
      "code": "birch_pollen", "name": "Берёза", "unit": "grains/m³",
      "status": "available",            // available | partial | unknown
      "known_seconds": 3240.0, "total_seconds": 3240.0, "known_share": 1.0,
      "mean_concentration": 12.6,       // среднее по ИЗВЕСТНОМУ времени
      "peak_concentration": 34.2,
      "peak_at": "2026-07-17T08:00:00+00:00",
      "exposure_grains_hours_per_m3": 11.34
    }
    // ... остальные виды
  ],
  "legs": null,                    // массив при include_legs: true
  "reasons": ["modeled_pollen_concentration", "experimental_uncalibrated"],
  "limitations": ["...", "..."],
  "assumptions": ["..."],
  "source_provenance": {
    "available": true, "provider": "Open-Meteo", "model": "cams_europe",
    "model_name": "CAMS European Air Quality Forecast (CAMS ENSEMBLE)",
    "license": "CC-BY-4.0",
    "attribution": "Pollen data by Open-Meteo.com; CAMS ENSEMBLE (Copernicus Atmosphere Monitoring Service) model",
    "source_url": "https://open-meteo.com/en/docs/air-quality-api",
    "retrieved_at": "...", "valid_until": "...",
    "interval_start": "...", "interval_end": "...",
    "temporal_resolution_seconds": 3600,
    "spatial_resolution_meters": 11000, "grid_step_degrees": 0.1,
    "cell_latitude": 59.9, "cell_longitude": 30.3,
    "cell_bounds": [59.85, 30.25, 59.95, 30.35],
    "contacted_point": [59.9405, 30.3165],
    "unit": "grains/m³", "species": ["alder_pollen", "..."],
    "time_semantics": "instantaneous concentration valid for the indicated hour [t, t+1h); null is a gap, not zero"
  },
  "diagnostics": {"legs": 1, "legs_outside_source_cell": 0,
                  "species_available": 6, "species_partial": 0, "species_unknown": 0,
                  "species_withheld": 0}
}
```

### Статусы

| `status` | Когда | Числа |
|---|---|---|
| `available` | вся прогулка покрыта, все виды известны | да, `unknown_meters == 0` |
| `partial` | часть прогулки без значений (горизонт/вне ячейки) | да, только по известной части |
| `unavailable` | источник выключен/недоступен/устарел, значений нет вообще, **или ни на одном часе прогулки нет полной панели всех видов** | **нет**: `known_meters == 0`, все виды `unknown`, `legs: null` |
| `outside_coverage` | **все** этапы вне ячейки источника | **нет** |

### Инварианты, которые клиент вправе (и обязан) проверять

* `known_meters + unknown_meters == route_length_meters` — **точно** после
  округления до 0,1 м (остаток округления поглощает `unknown_meters`);
* `coverage_share == known_meters / route_length_meters`;
* `concentration_unit == "grains/m³"` — клиент рендерит единицу из ответа и
  отвергает ответ с другой единицей, а не подставляет свою;
* у вида со `status != "unknown"` есть `mean_concentration`,
  `peak_concentration`, `exposure_grains_hours_per_m3` и `peak_at` со
  смещением; у `unknown` — все четыре `null`;
* `mean_concentration <= peak_concentration`;
* `elapsed_duration_seconds >= walking_duration_seconds` (осмотр POI
  увеличивает общее время, но не время накопления);
* при `status == "available"` ни один вид не `partial`/`unknown`;
* при `status == "unavailable"`/`"outside_coverage"` — `known_meters == 0`,
  все виды `unknown` (все четыре числовых поля `null`), `confidence ==
  "unavailable"`, `legs: null`; `known_share` при этом остаётся честным
  покрытием источника — см. следующий раздел;
* **числа приходят только вместе с атрибуцией** (`source_provenance.available
  == true` и непустые `attribution`/`license`) и с непустыми
  `assumptions`/`limitations`: ответ с числами без источника и без оговорок
  клиент считает недействительным, а не «почти полным».

Клиентская модель `PollenAnalysisModel.tryFromJson` проверяет всё
перечисленное и возвращает `null` при любом нарушении (см.
`frontend/test/pollen_analysis_model_test.dart`).

### Удержанные числа: `pollen_no_complete_panel` (C7.3.1)

Покрытие маршрута считается по **полной панели** шести видов (MODEL §4).
Если ни на одном часе прогулки нет всех шести значений сразу, вердикт
маршрута — `unavailable`, а на `unavailable` по контракту не публикуется ни
одно число. Тогда:

* каждый вид публикуется как `unknown` со всеми четырьмя `null` — включая
  виды, известные на всей прогулке;
* `known_seconds`/`known_share` **не обнуляются**: это покрытие самого
  источника, и оно остаётся правдой — «источник значения имел, числа
  удержаны». Обнулить их значило бы выдать известный факт за ноль;
* причина — `pollen_no_complete_panel` (вместе с `pollen_incomplete`, если
  какой-то вид покрыт частично). `pollen_no_data` остаётся за случаём «в
  источнике нет значений вообще»: там у всех видов `known_share == 0`;
* `diagnostics.species_withheld` — сколько видов имели значения, чьи числа
  не опубликованы (во всех остальных случаях `0`); остальные счётчики
  `diagnostics` описывают то, что движок нашёл в источнике, а не то, что
  ответ публикует;
* `legs` — `null`: по-этапные числа удержаны вместе с маршрутными.

Клиент отличает это состояние по `status == "unavailable"` **и** ненулевому
`known_share` хотя бы у одного вида; Flutter-модель отдаёт его как
`hasWithheldNumbers` и называет виды вместо фразы «значений нет». Любой
другой ответ `unavailable` (источник выключен, недоступен, устарел, прогулка
вне ячейки или горизонта) приходит с нулевым `known_share` у всех видов — это
проверяется и на backend, и на клиенте.

Обязательный сценарий — двухчасовая прогулка, где в первом часе известна
ольха и нет берёзы, а во втором наоборот, остальные четыре вида известны в
обоих часах: `test_no_complete_panel_over_http_publishes_no_species_numbers`.
Тот же ответ принимается Flutter-моделью как дословный payload в
`frontend/test/fixtures/c7_3_1_withheld_panel_response.json`
(`the withheld-panel answer the backend really sends is accepted`), а попытка
прислать вид с числом под `unavailable` по-прежнему отвергается
(`an unavailable answer that still carries a species number`).

### `include_legs`

По умолчанию `false` и `legs: null`. При `true` возвращается по-этапная
разбивка: `starts_at_seconds` / `ends_at_seconds` (окна модельного времени,
включая сдвиг от осмотров POI), `known_meters`, `unknown_meters`,
`inside_source_cell` и те же поля по видам. По-этапные значения — это
**временной** профиль одной и той же ячейки: смена часа меняет значение,
смена места внутри ячейки — нет.

## Ошибки

| Код | Когда | Тело |
|---|---|---|
| 413 | тело > `REQUEST_MAX_BYTES` | middleware, `{"detail": "Тело запроса слишком большое."}` |
| 422 | запрос/маршрут не проходит валидацию, маршрут вне зоны СПб, вырожденная геометрия, длиннее лимита | русское объяснение, без координат в логах |
| 503 | анализ превысил `POLLEN_ANALYSIS_TIMEOUT_SECONDS` | «Расчёт пыльцевой нагрузки занял слишком много времени и был остановлен.» |

Ошибка backend — **не** ошибка маршрута: клиент показывает «Обычная
навигация продолжает работать» и не трогает прогулку. Недоступность
источника — это не ошибка: это `200` с `status: "unavailable"`.

## Приватность и нагрузка

* Маршрут и GPS-координаты **не отправляются** ни одному внешнему сервису:
  вверх уходит один фиксированный центроид центра СПб.
* Экземпляр провайдера один на процесс (`lru_cache` в `wiring`), кэш 1 час,
  single-flight: «один внешний запрос на окно» действует и **между
  HTTP-запросами и пользователями**. Проверено на уровне HTTP:
  `test_one_source_fetch_is_shared_by_consecutive_requests` (3 запроса → 1
  обращение; четвёртый после окна → ровно 2-е). Отдельный тест
  `test_the_production_request_never_carries_user_geometry` проверяет, что
  координаты маршрута не встречаются в URL запроса.
* Анализ выполняет **0 native routing-запросов** и не создаёт нового бюджета.
* Результат не содержит идентификаторов устройства и не логируется с
  координатами.

## Конфигурация

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `POLLEN_SOURCE_ENABLED` | `false` | явный opt-in внешнего источника |
| `POLLEN_SOURCE_TIMEOUT_SECONDS` | `10` | внешний бюджет шире внутреннего (8 с), чтобы отказ приходил от провайдера |
| `POLLEN_SOURCE_LATITUDE` / `_LONGITUDE` | `59.9405` / `30.3165` | фиксированный центроид |
| `POLLEN_MAX_CONCURRENT_ANALYSES` | `2` | семафор CPU-нагрузки |
| `POLLEN_ANALYSIS_TIMEOUT_SECONDS` | `10` | серверный бюджет анализа |

## Flutter-интеграция

* `BackendPollenAnalysisService` (`lib/services/pollen_analysis_service.dart`)
  — общий `ApiClient` (никакого второго Dio), receive-timeout 15 с, тот же
  проверенный контракт геометрии/времени, что у shade, но с
  `include_legs` вместо `include_segments`.
* `PollenAssessmentProvider` — необязательная read-only оценка; во время
  активной прогулки использует `NavigationProvider.buildRemainingRoute()` из
  C7.1.2, не снимает planning lock и не меняет геометрию.
* `PollenAssessmentSection` — карточка «Пыльцевая нагрузка»: время оценки,
  виды с концентрациями и единицами, покрытие, уверенность, причины,
  источник с лицензией CC-BY-4.0 и ссылкой, шаг по времени и
  пространственное разрешение, ячейка, допущения и ограничения.
* В опциональном симуляторе (C7.1) выбор времени в карточке — это смена
  **общего виртуального времени** (как у тени и мокрых участков).
