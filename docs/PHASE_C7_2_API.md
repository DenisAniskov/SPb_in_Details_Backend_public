# C7.2 — API оценки риска мокрых участков

Отдельный endpoint не меняет контракты C7.1 Shade: другой путь, другая схема,
тот же стиль честности.

```
POST /api/v1/environment/puddle/analyze
```

## Запрос

```json
{
  "departure_at": "2026-10-16T15:30:00+03:00",
  "include_segments": false,
  "legs": [
    {
      "geometry": [{"latitude": 59.9401, "longitude": 30.3152}, {"latitude": 59.9409, "longitude": 30.3165}],
      "walking_duration_seconds": 600.0,
      "visit_duration_seconds_after_leg": 900.0
    }
  ]
}
```

| Поле | Ограничение | Поведение при нарушении |
|---|---|---|
| `departure_at` | ISO-8601 **с часовым поясом**, год 2000–2100 | 422 |
| `legs` | 1–24 этапа | 422 |
| `legs[].geometry` | 2–2000 точек, координаты в допустимых диапазонах | 422 |
| Σ точек | ≤5000 | 422 |
| `walking_duration_seconds` | >0, ≤6 ч на этап | 422 |
| `visit_duration_seconds_after_leg` | ≥0, ≤24 ч; суммарное время прогулки ≤24 ч | 422 |
| длина маршрута | ≤25 000 м, >0 | 422 |
| регион | Санкт-Петербург и окрестности (59.45–60.45 N, 29.05–31.15 E) | 422 |
| всё тело | ≤`REQUEST_MAX_BYTES` (по умолчанию 16 384 байта) | 413 (middleware) |
| `extra` поля | запрещены (`extra="forbid"`) | 422 |

Клиент обязан отправлять геометрию **как есть**: тот же контракт длины/точек,
что у Shade (клиентская проверка A6.3), без обрезки полилинии.

## Ответ

200 возвращается и для «без чисел»: статус в теле говорит правду.

```json
{
  "status": "partial",
  "method": "experimental_precipitation_surface_index",
  "method_version": "c7_2.4",
  "estimate": true,
  "risk_level": "moderate",
  "risk_index": 0.42,
  "route_length_meters": 1000.0,
  "known_meters": 600.0,
  "unknown_meters": 400.0,
  "low_meters": 0.0,
  "moderate_meters": 600.0,
  "high_meters": 0.0,
  "coverage_share": 0.6,
  "confidence": "low",
  "evaluated_period": {"start": "...", "end": "...", "timezone": "MSK"},
  "dataset_version": "c7_2_palace_pilot_v1",
  "dataset_provenance": {"source": "OpenStreetMap", "license": "ODbL-1.0", "...": "..."},
  "weather_provenance": {"available": true, "provider": "Open-Meteo", "model": "icon_global", "kind": "modeled_...", "license": "CC-BY-4.0", "attribution": "Weather data by Open-Meteo.com; DWD ICON model", "source_url": "https://open-meteo.com/en/docs"},
  "reasons": ["unmatched_osm_way", "model_precipitation_history", "experimental_uncalibrated"],
  "limitations": ["Оценка относительная: ..."],
  "classified_segments": null,
  "sensitivity": {"baseline": "moderate", "drying_faster_0_5x": "low", "...": "..."}
}
```

| Статус | risk_level | known_meters | Смысл |
|---|---|---|---|
| `available` | low/moderate/high | = всей длине | всё подтверждено |
| `partial` | low/moderate/high | >0 | часть пути неизвестна, числа только по подтверждённому |
| `unavailable` | `unknown` | 0 | нет модели/погоды/совпадений — чисел нет |
| `outside_coverage` | `unknown` | 0 | маршрут вне пилотной зоны OSM |

Инварианты, которые клиент вправе проверять (и проверяет):

* `known_meters + unknown_meters == route_length_meters`;
* `low_meters + moderate_meters + high_meters == known_meters`;
* `coverage_share == known_meters / route_length_meters`;
* `risk_index` отсутствует ровно тогда, когда `risk_level == "unknown"`;
* длины округлены до 0.1 м **согласованно** (остаток округления поглощает
  последняя корзина), поэтому суммы сходятся после сериализации;
* прозрачность входит в контракт: при наличии чисел `sensitivity` непуст и
  его `baseline` равен `risk_level`; при `risk_level == "unknown"` блок пуст
  (`{}`). Клиентская модель отклоняет ответ, нарушающий любое из этих
  условий, — «полу-посчитанная» прозрачность не показывается.
* сегмент с подтверждённым покрытием, но **непокрытым окном истории осадков**
  (в `[момент − 12 ч, момент]` нет непрерывной цепочки часовых бинов, в том
  числе отсутствует текущий час или есть внутренний пропуск) не получает
  числа: `state = "unknown"`, причина `weather_incomplete`. Это не сбой, а
  граница данных; остальные сегменты оцениваются как обычно.
* с `c7_2.4` в цепочку истории входят только бины, начавшиеся раньше момента
  оценки: разрыв в будущем прогнозе после прогулки не скрывает полную прошлую
  историю. Проверка снега/мороза учитывает любой бин, перекрывающий окно
  оценки (12 ч истории + прогулка), включая бин, заканчивающийся после её
  последнего момента. Это исправления финального аудита C7.2; коэффициенты и
  пороги не менялись.

`sensitivity` — это уровень при `τ×0.5`, `τ×2.0` и `saturation×0.75` плюс
примечание бэкенда; UI показывает варианты и примечание как есть. Модель
времени и допущения об осадках описаны в `PHASE_C7_2_MODEL.md` §1, §7, §10.

`confidence` для v1 всегда `low` (модель некалиброванная, погода модельная,
покрытие пилотное) или `unavailable` — «высокой уверенности» у этой версии
не бывает.

## Ошибки

| Код | Когда | Тело |
|---|---|---|
| 413 | тело > лимита | middleware, `{"detail": "Тело запроса слишком большое."}` |
| 422 | запрос/маршрут не проходит валидацию | русское объяснение, без координат в логах |
| 503 | анализ превысил `PUDDLE_ANALYSIS_TIMEOUT_SECONDS` | «Расчёт мокрых участков занял слишком много времени…» |

Ошибка backend — это **не** ошибка маршрута: клиент показывает «Обычная
навигация продолжает работать» и не трогает прогулку.

## Конфигурация

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `PUDDLE_WAYS_DATASET` | `data/puddle_ways_spb_pilot.json` | пилотный снимок OSM (пусто = датасет отключён → `unavailable`) |
| `PUDDLE_MAX_CONCURRENT_ANALYSES` | 2 | семафор CPU-нагрузки |
| `PUDDLE_ANALYSIS_TIMEOUT_SECONDS` | 10 | тайм-аут расчёта (503) |
| `PUDDLE_WEATHER_ENABLED` | `false` | **явный** opt-in модельного провайдера |
| `PUDDLE_WEATHER_TIMEOUT_SECONDS` | 6 | тайм-аут запроса погоды (деградация в `unavailable`) |
| `PUDDLE_WEATHER_LATITUDE` / `LONGITUDE` | 59.9405 / 30.3165 | единственная точка запроса погоды (маршрут никогда не уходит наружу) |

По умолчанию погода **выключена**: endpoint отвечает `unavailable` с
`weather_not_configured`. Это ожидаемое состояние до явного решения
оператора (условия free-tier Open-Meteo, см. `PHASE_C7_2_DATA_PROVENANCE.md`).

## Приватность и нагрузка

* Маршрут и GPS-координаты **не отправляются** ни одному внешнему сервису.
* Weather-провайдер запрашивает один фиксированный центроид пилота и
  кэширует серию на 15 минут. Экземпляр провайдера один на процесс (кэш
  композиционного корня), поэтому «один внешний запрос на окно, single-flight»
  действует и **между HTTP-запросами и пользователями**, а не только внутри
  одного запроса (проверено на уровне HTTP:
  `test_one_weather_fetch_is_shared_by_consecutive_requests`).
* Анализ выполняет **0 native routing-запросов** и не создаёт нового бюджета
  экологической функции.
* Результат оценки не включает идентификаторов устройства и не пишется в
  логи с координатами.

## Flutter-интеграция

* `BackendPuddleAnalysisService` (`lib/services/puddle_analysis_service.dart`)
  — общий `ApiClient` (никакого второго Dio), отдельный receive-timeout 15 с.
* `PuddleAssessmentProvider` — необязательная read-only оценка; использует
  `NavigationProvider.buildRemainingRoute()` из C7.1.2 во время активной
  прогулки, не снимает planning lock и не меняет геометрию.
* `PuddleAssessmentSection` — карточка «Мокрые участки после дождя» с
  временем, покрытием, уверенностью, причинами, ограничениями, блоком
  `sensitivity` и полной атрибуцией погодного источника (Open-Meteo, DWD
  ICON, CC-BY-4.0, ссылка) — лицензия CC-BY-4.0 требует атрибуции в UI.
* В опциональном симуляторе (C7.1) выбор времени в карточке — это смена
  общего виртуального времени: см. `PHASE_C7_2_MODEL.md` §11.

## Ручная проверка контракта

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/environment/puddle/analyze \
  -H 'Content-Type: application/json' \
  -d '{"departure_at":"2026-10-16T15:30:00+03:00","legs":[{"geometry":[{"latitude":59.9401,"longitude":30.3152},{"latitude":59.9409,"longitude":30.3165}],"walking_duration_seconds":600}]}'
```

Без включённого провайдера погоды ответ содержит `"status": "unavailable"` и
`"reasons": ["weather_not_configured"]` — и это корректный ответ, а не сбой.
