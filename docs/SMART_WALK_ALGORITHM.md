# Smart Walk: локальный подбор v2 и проверка MapKit

Актуализировано 2026-10-10 для Phase C2. Подбор детерминированный, по локальному проверенному каталогу. Геометрическое предложение backend и реальный пешеходный маршрут MapKit остаются разными стадиями. Точные правила, ограничения и additive API: [PHASE_C2_LOCAL_ROUTING_ALGORITHM.md](PHASE_C2_LOCAL_ROUTING_ALGORITHM.md); воспроизводимое географическое сравнение: [PHASE_C2_GEOGRAPHIC_EVALUATION.md](PHASE_C2_GEOGRAPHIC_EVALUATION.md).

## Каталог и совместимость

Канонический каталог хранится в `app/db/seed_places.json`; startup и Smart Walk не обращаются к Overpass. Проверенные source snapshots, stable `catalog_key`, исходные OSM-координаты и редакторские оценки сохраняются отдельно от пользовательских записей. Расширение не требует удаления SQLite, пересоздания ID или сброса saved walks. Key-based startup остаётся идемпотентным; legacy migration и ремонт непригодных значений проверяются существующими тестами. Количество и ограничения проверенного расширения указаны в машинно-читаемых манифестах каталога (`docs/phase_c2_catalog_manifest.json`, `docs/phase_c3_catalog_manifest.json`, `docs/phase_c6_1_catalog_manifest.json`).

`recommended_visit_minutes` — приблизительное редакторское время SmartNavigator, integer 5..180; старое отсутствие поля/NULL соответствует 30 минутам. Это не официальные часы музея, не очередь и не длительность Яндекса. Исходные времена не сокращаются для улучшения метрик. Display/source coordinates не становятся “проверенным входом” только потому, что на них строится маршрут.

## Backend proposal v2

`POST /api/v1/routes/propose-walk` принимает numeric finite WGS84 start, integer 30..360 минут, существующие категории, boolean `prefer_unusual` и максимум 1..6 POI. Строковые/boolean координаты и нецелые/строковые бюджеты отклоняются. UI сохраняет ровно три режима: «Быстрая прогулка» — 60, «Обычная прогулка» — 120, «Погулять подольше» — 240 минут. Бюджет включает ходьбу и осмотр; режим никогда не меняется автоматически.

Категории — строгий фильтр. Пустой список допускает все шесть enum. Нет fallback на чужие интересы и обязательного дополнительного POI. Unusual — мягкий бонус **4** балла вместо C1 25; обычные места остаются пригодными. UI включает предпочтение по умолчанию без отдельного переключателя; явное API `false` сохраняется.

Каждый stop получает 50/30 interest points, 0.2×min(visit,45) sightseeing points, до40 proximity points (`40×max(0,1−d/3500)`), unusual4 и diversity12 за первую категорию, если разрешены все или несколько категорий. Route utility — сумма stop scores минус `120×total_walk/budget` и `80×first_walk/budget`. Так длительный первый переход сильнее влияет на быстрый режим, а extended допускает более широкий связный маршрут. Все расстояния здесь **Haversine**, все walking estimates — геометрическая эвристика **75 м/мин**, без дорог, мостов и ограждений.

Сервис проверяет координаты, ID, enum, осмотр и возможность `PlaceRead`-сериализации до ranking. Повторные IDs/catalog keys и одинаковые нормализованные названия в25м не создают лишние stops; конфликтующие копии одного ID исключаются. POI менее25м от старта исключаются, чтобы не производить нулевые прогулки и не вытеснять пригодную цель из ограниченного пула.

Пул максимум24: представители интересов, ближайшие16 допустимых целей, затем singleton utility. Beam максимум32 states на depth, максимум6 depths; геометрическая matrix24×24. Каждое сохранённое промежуточное решение, включая один POI, может стать итогом. Конкурируют разные наборы и порядки, поэтому длинный greedy-first осмотр не обязан исключать комбинацию коротких nearby stops. Tie-break: route utility, distance, visit duration, ID sequence. Старый 2-opt helper сохранён, но v2 production выбирает порядок внутри beam. Глобальный оптимум и native feasibility не гарантируются.

Проверка строгая: `ceil(Haversine_distance/75×60) + visit_minutes×60 <= budget×60`. Reported total округляется вверх до целых минут. Никаких скрытых+15минут или tolerance. Положительная полезность stops может уменьшать предварительный запас времени по сравнению с C1; это риск для native refinement, отражённый в сравнении, а не доказательство лучшего реального маршрута.

Все прежние поля ответа сохранены. Additive `alternatives` содержит максимум2 предварительно допустимых объекта с теми же totals, budget, items и sequence. Наборы POI различаются; простая перестановка того же набора не считается альтернативой. По возможности меняется первая цель и появляется заменяющий POI. Старый ответ без alternatives остаётся совместимым. Reasons пересчитываются в окончательном порядке и называют близость предварительной геометрической оценкой.

Пустой ответ содержит нулевые totals, пустые items/sequence/alternatives и `reason_code`: `no_eligible_places`, `category_filter_empty`, `preliminary_budget_infeasible` или `no_meaningful_route`. Это описание конкретной стадии. Нет утверждения, что в районе вообще отсутствуют достопримечательности. Полностью пустая DB сохраняет прежний404 endpoint. Backend не классифицирует native/network отказ как нехватку времени.

## Flutter: владение состоянием и native acceptance

Wizard владеет параметрами и ещё не принятым proposal. Revision инвалидирует старые результаты при изменении старта, preset, categories, закрытии/открытии и навигационной блокировке. Бюджет ответа должен совпадать с выбранным preset. Preview честно обозначает геометрическую оценку и редакторское время. Принятие передаёт параметры, primary и bounded alternatives в **RoutingProvider** — единственного владельца accepted stops, start, native route и totals. PlacesProvider не хранит копию маршрута.

Альтернативы проходят проверки budget, выбранных categories, canonical visit durations, уникальности IDs и отличающегося POI set. Add/remove/reorder и смена старта инвалидируют прежний маршрут/альтернативы; stale await не может опубликовать отменённый результат. Ручное редактирование сохраняет preset budget и очищает причины прежнего порядка. Одна цель допустима, но zero native distance не считается успешной прогулкой.

Официальный MapKit PedestrianRouter строит Start→POI1→POI2→… отдельными двухточечными запросами. MapKitService суммирует настоящие leg duration/distance и geometry; fractional seconds округляются вверх. Отказ leg не публикует частичный путь. Haversine никогда не подменяет pedestrian polyline.

Принятие требует **MapKit walking seconds + canonical editorial visit minutes×60 <= preset budget×60**. Primary, до2 alternatives и последующее удаление stops делят общий предел: **до6 service builds и до12 native point-to-point requests на одно действие**. Requests резервируются до build; фактический лимит также проверяется непосредственно перед native request. Слишком дорогая альтернатива может быть пропущена ради меньшего fallback. Single-stop replacement может проверяться перед удалением из предыдущего списка. Нет рекурсивного retry.

При no pedestrian path возможна другая кандидатная последовательность. Network/service/timeout/unknown error немедленно прекращает попытку, даже если предыдущая последовательность превышала время. Mixed no-path и over-budget показывается отдельно. Если ни одна полноценная native последовательность не подходит, новый route не публикуется; пользователь получает причину и действие: другой старт, более долгий preset, другие интересы или повтор после восстановления сети. Последний backend proposal не превращается в доказательство отсутствия объектов.

Все await защищены request revision; cancel/смена accepted walk не меняются устаревшим результатом. Существующие GPS navigation, native guidance, off-route checks, manual rerouting, arrival safeguards, next-stop confirmation, pause/background safeguards и attribution остаются в Phase C1 архитектуре. Автоматическое rerouting, background GPS, voice guidance и новые permissions не добавлены.

## Проверки и ограничения

56 новых backend regressions проверяют locality, greedy trap, unusual soft preference, single stop, alternative replacement, strict categories, fractional budget boundary, duplicate/corrupt rows, deterministic results, bounded geometric work, pole/dateline guards и additive compatibility.

Автоматические Python/Flutter fixtures не доказывают настоящее время ходьбы. Без отдельно захваченных физических Android результатов native runtime status — **NOT_TESTED**. Synthetic evaluation points не являются точными координатами пользователя или посещёнными входами. Opt-in debug diagnostics используют только явно заданный synthetic test-point ID и числовые результаты, без координат, GPS history, названий и ключей API.

Остаются ограничения каталога, непроверенные entrance/access details, расписания и очереди, неполный semantic duplicate detection и approximate bounded search. OSM/ODbL и Yandex notices сохраняются. Количественные географические улучшения относятся только к опубликованной synthetic матрице и предварительным оценкам, пока нет captured native evidence.
