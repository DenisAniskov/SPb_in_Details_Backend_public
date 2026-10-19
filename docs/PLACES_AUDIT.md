# Проверка каталога мест — 3 октября 2026

Это исторический аудит первоначальных 25 записей. Последующие расширения каталога (Phase B, C2, C3, C6.1) сохраняют эти 25 записей полностью; их машинно-читаемые доказательства — в материалах `phase_b_*` и `phase_c*_*` каталога. Приведённые ниже ID — исходные seed ID; миграции сохраняют фактические ID существующей базы.

Исходный backend содержал **25** мест, Flutter — **14**. ID не совпадали: например, ID 5 в backend был Домом Зингера, а во Flutter — Русским музеем. Встроенная копия Flutter удалена. Единственный каталог — `app/db/seed_places.json`.

Названия и географическое положение сопоставлены вручную с объектами исходной базы OpenStreetMap, полученной через Overpass API. Использованы тип объекта, название, адресные теги, тематика и положение. Снимок выбранных объектов: `docs/osm_source_snapshot.json`. Ссылки ниже дают проверяемое происхождение координат, но **не подтверждают действующий вход, проходимость, часы работы или доступность объекта**. Для way/relation `out center` возвращает центр ограничивающего прямоугольника, а не вход и не геометрический центроид. Для node используется точка OSM.

Категории и флаг «необычное место» — редакционная классификация проекта. Они не являются измерениями тишины, тени или пыльцы. Все 6 категорий сохранены. Для места с несколькими темами выбрана одна категория для MVP.

Рейтинги, внешние стоковые фотографии, часы работы, годы и стили без проверенных источников удалены. Адреса с `osm_tags` взяты из выбранного объекта OSM. Остальные сохранены как предварительные; они не обозначены как независимо подтверждённые. Подробные описания заменены краткими нейтральными текстами.

| ID исходного seed | Место | Прежние координаты | Координаты из источника | Категория | Источник | Значение точки | Статус адреса |
|---|---|---|---|---|---|---|---|
| 1 | Государственный Эрмитаж (Зимний дворец) | 59.939832, 30.314560 | 59.9404659, 30.3137851 | history_architecture | [relation/69424](https://www.openstreetmap.org/relation/69424) | osm_bounds_center | osm_tags |
| 2 | Исаакиевский собор | 59.934093, 30.306079 | 59.9340838, 30.3061563 | history_architecture | [relation/3229838](https://www.openstreetmap.org/relation/3229838) | osm_bounds_center | osm_tags |
| 3 | Спас на Крови (Храм Воскресения Христова) | 59.940076, 30.328905 | 59.9400907, 30.3288445 | history_architecture | [way/28966446](https://www.openstreetmap.org/way/28966446) | osm_bounds_center | osm_tags |
| 4 | Петропавловская крепость | 59.950000, 30.316667 | 59.9500638, 30.3164295 | history_architecture | [relation/227969](https://www.openstreetmap.org/relation/227969) | osm_bounds_center | legacy_address_not_independently_verified |
| 5 | Дом компании «Зингер» (Дом книги) | 59.935633, 30.325904 | 59.9358632, 30.3258905 | history_architecture | [way/102305449](https://www.openstreetmap.org/way/102305449) | osm_bounds_center | osm_tags |
| 6 | Государственный Русский музей (Михайловский дворец) | 59.938634, 30.332187 | 59.9385846, 30.3307196 | art | [relation/2614475](https://www.openstreetmap.org/relation/2614475) | osm_bounds_center | legacy_address_not_independently_verified |
| 7 | Музей Фаберже (Дворец Нарышкиных-Шуваловых) | 59.934444, 30.343889 | 59.9347199, 30.3431283 | art | [node/3702115194](https://www.openstreetmap.org/node/3702115194) | osm_node | legacy_address_not_independently_verified |
| 8 | Мариинский театр (Историческая сцена) | 59.925761, 30.296181 | 59.9254301, 30.2959090 | art | [relation/1690283](https://www.openstreetmap.org/relation/1690283) | osm_bounds_center | osm_tags |
| 9 | Мозаичный дворик Малой Академии искусств | 59.945833, 30.341111 | 59.9470834, 30.3391853 | art | [node/7932398588](https://www.openstreetmap.org/node/7932398588) | osm_node | osm_tags |
| 10 | Кунсткамера (Музей антропологии и этнографии им. Петра Великого) | 59.941641, 30.304561 | 59.9415303, 30.3042545 | science_technology | [relation/3296328](https://www.openstreetmap.org/relation/3296328) | osm_bounds_center | osm_tags |
| 11 | Центральный музей связи имени А. С. Попова | 59.932778, 30.302222 | 59.9326534, 30.3022684 | science_technology | [way/61650354](https://www.openstreetmap.org/way/61650354) | osm_bounds_center | osm_tags |
| 12 | Ледокол «Красин» (Музей Мирового океана) | 59.927907, 30.269094 | 59.9278478, 30.2689987 | science_technology | [relation/3283438](https://www.openstreetmap.org/relation/3283438) | osm_bounds_center | legacy_address_not_independently_verified |
| 13 | Планетарий № 1 (в бывшем газгольдере) | 59.907400, 30.321800 | 59.9116843, 30.3310735 | science_technology | [way/153957591](https://www.openstreetmap.org/way/153957591) | osm_bounds_center | osm_tags |
| 14 | Летний сад | 59.945000, 30.335278 | 59.9445853, 30.3350962 | nature | [relation/2653740](https://www.openstreetmap.org/relation/2653740) | osm_bounds_center | legacy_address_not_independently_verified |
| 15 | Ботанический сад Петра Великого | 59.970278, 30.323611 | 59.9700433, 30.3252898 | nature | [relation/2123949](https://www.openstreetmap.org/relation/2123949) | osm_bounds_center | legacy_address_not_independently_verified |
| 16 | Михайловский сад | 59.939722, 30.333056 | 59.9398334, 30.3326266 | nature | [relation/2614476](https://www.openstreetmap.org/relation/2614476) | osm_bounds_center | legacy_address_not_independently_verified |
| 17 | Таврический сад | 59.945556, 30.366667 | 59.9461010, 30.3728761 | nature | [relation/1582734](https://www.openstreetmap.org/relation/1582734) | osm_bounds_center | legacy_address_not_independently_verified |
| 18 | Остров «Новая Голландия» | 59.929722, 30.289167 | 59.9296195, 30.2897366 | modern_spaces | [relation/1576204](https://www.openstreetmap.org/relation/1576204) | osm_bounds_center | legacy_address_not_independently_verified |
| 19 | Севкабель Порт | 59.924444, 30.241111 | 59.9237294, 30.2431486 | modern_spaces | [relation/1411921](https://www.openstreetmap.org/relation/1411921) | osm_bounds_center | legacy_address_not_independently_verified |
| 20 | Бертгольд Центр | 59.927222, 30.312778 | 59.9283229, 30.3122764 | modern_spaces | [node/6848299386](https://www.openstreetmap.org/node/6848299386) | osm_node | legacy_address_not_independently_verified |
| 21 | Ленинградский зоопарк | 59.953611, 30.308333 | 59.9517488, 30.3087747 | family_leisure | [relation/3296313](https://www.openstreetmap.org/relation/3296313) | osm_bounds_center | legacy_address_not_independently_verified |
| 22 | Гранд Макет Россия | 59.887222, 30.317500 | 59.8880456, 30.3308909 | family_leisure | [relation/2662030](https://www.openstreetmap.org/relation/2662030) | osm_bounds_center | osm_tags |
| 23 | Санкт-Петербургский Океанариум («Планета Нептун») | 59.919444, 30.338889 | 59.9190485, 30.3382664 | family_leisure | [node/986234938](https://www.openstreetmap.org/node/986234938) | osm_node | osm_tags |
| 24 | Ротонда на Гороховой | 59.926111, 30.324722 | 59.9256318, 30.3257431 | history_architecture | [node/2400928307](https://www.openstreetmap.org/node/2400928307) | osm_node | legacy_address_not_independently_verified |
| 25 | Аптека доктора Пеля и сыновей | 59.938889, 30.283889 | 59.9382943, 30.2839421 | history_architecture | [node/951709409](https://www.openstreetmap.org/node/951709409) | osm_node | legacy_address_not_independently_verified |

Особенно существенны поправки Планетария № 1, Гранд Макета, Мозаичного дворика и аптеки Пеля. В исходной версии координаты не имели источников, поэтому называть их «верифицированными» было неправомерно. Название аптеки уточнено по музейному POI; координата музея не выдается за отдельную координату Башни грифонов.

Стартовые пресеты Flutter — приблизительные точки района/площади, унаследованные из первой версии. Их точность до входа метро не подтверждена. Для точного старта используется долгий тап на настоящей карте Яндекса; приложение передаёт выбранную пользователем координату в SDK.

© OpenStreetMap contributors. Данные доступны по [ODbL](https://www.openstreetmap.org/copyright). Каталог и снимок сохраняют атрибуцию; геометрия рек и улиц не создаётся проектом, базовую карту предоставляет Яндекс.
