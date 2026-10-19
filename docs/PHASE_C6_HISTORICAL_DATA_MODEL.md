# Phase C6 — Модель исторических данных

## Файл набора

`app/db/place_history.json` — версионированный редакторский набор:

```json
{
  "meta": {
    "version": "1.0.0",
    "locale": "ru",
    "generatedOn": "2026-10-11",
    "license": "…",
    "attribution": "…"
  },
  "entries": [ { … HistoricalPlaceContent … } ]
}
```

Индексация — по стабильному `catalog_key` стартового каталога
(`app/db/seed_places.json`). Числовой `Place.id` сознательно не
используется: он зависит от установки БД.

## HistoricalPlaceContent (§3.1)

| Поле | Тип | Правила |
|---|---|---|
| `catalogKey` | str ≤80 | `[a-z][a-z0-9-]*`, обязан существовать в каталоге |
| `locale` | str | BCP-47-подобный, по умолчанию `ru` |
| `shortHistoricalSummary` | str ≤1200 | 2–4 предложения (§6.1) |
| `detailedHistoricalText` | str ≤6000 | ориентир 150–250 слов (§6.2) |
| `period` | str? ≤120 | свободная редакторская метка («1754–1762») |
| `timeline` | list | см. HistoricalTimelineEvent |
| `people` | list | см. HistoricalPerson |
| `facts` | list | см. HistoricalFact |
| `sources` | list ≥1 | см. HistoricalSource |
| `editorialStatus` | enum | `verified` / `needs_review` / `draft` |
| `lastVerifiedOn` | ISO-дата | не в будущем (жёсткая проверка схемы: отклоняется при загрузке) |
| `contentVersion` | int ≥1 | версия карточки |

Каждое поле карточки НЕ обязано быть заполнено (кроме отмеченного):
у малого объекта могут отсутствовать timeline/people — UI скрывает пустые
разделы, ничего не выдумывая.

## HistoricalTimelineEvent (§3.3)

- `dateLabel` — отображается как написано: «середина XVIII века» остаётся
  периодом, ложная точность запрещена.
- `datePrecision` ∈ `exact | year | period | circa` — различает точную дату,
  год, период и приблизительную датировку.
- `sortKey` — необязательная строка сортировки («1754», «1754-05-27»).
- `sourceIds` — ссылки на источники ЭТОЙ карточки.

## HistoricalPerson (§3.4)

`role` ∈ `architect | engineer | sculptor | founder | patron |
historical_figure | other`. Авторство не приписывается без подтверждения:
например, у Каменноостровского дворца роль архитектора честно помечена
как неизвестная.

## HistoricalFact (§3.2)

`category` ∈ `architecture | creation_history | historical_events |
notable_people | cultural_significance | interesting_details |
reconstructions`. Лимита на число фактов нет, но валидатор предупреждает,
если список превращается в однотипный filler (>7).

## HistoricalSource (§3.5)

- `url` — только абсолютные http(s)-ссылки (схемы `javascript:`, `data:`,
  `file:` отвергаются схемой).
- `sourceType` ∈ `official_museum | government_heritage | academic |
  official_reference | encyclopedia | other`.
- `accessedOn` — ISO-дата сверки; не в будущем (жёсткая проверка схемы
  вместе с `lastVerifiedOn` и `meta.generatedOn`: будущая дата — ошибка
  редакторского ввода и отклоняется при загрузке набора, а не только
  предупреждением в скрипте покрытия).
- Ссылка на главную страницу музея не считается доказательством произвольного
  утверждения — конкретные факты карточек опираются на конкретные статьи,
  указанные с датой доступа.

## Целостность и валидация

Проверяются при каждой загрузке (и в CI через `validate_dataset()`):

1. нет дубликатов `catalogKey`, ключ существует в каталоге POI;
2. каждый факт/событие/персона ссылается на источники **своей** карточки,
   и ни один источник не остаётся «висячим»;
3. id фактов и событий уникальны внутри карточки;
4. даты — ISO, не из будущего (отклонение при загрузке, тесты
   `test_future_*_is_rejected_at_load`); URL — http(s);
5. статусы, роли, категории и точности дат — из разрешённых множеств.

Дальнейшие правила объёма (150–250 слов и т.п.) — предупреждения, а не ошибки:
малый объект не растягивается искусственно.

## Flutter-модель

`frontend/lib/data/models/historical_content_model.dart` — иммутабельные
классы с защитным `fromJson`: повреждённые значения (не-строки, не-списки,
элементы без обязательных полей) отбрасываются, карточка деградирует до
пустых списков, но не роняет экран. Поддержаны и camelCase (API), и
snake_case ключи.

## Подготовка к C13 (Audio Guides) и C14 (AI Assistant)

- устойчивые `id` фактов и событий — якоря для будущего озвучивания и
  цитирования;
- факты отделены от туристических советов (`visit_note` остаётся в каталоге);
- русский исходный текст, `editorialStatus` и `lastVerifiedOn` — признак
  проверенности; сомнительные сведения не попадают в набор;
- C13/C14 должны потреблять структуру, а не генерировать факты без источников.
