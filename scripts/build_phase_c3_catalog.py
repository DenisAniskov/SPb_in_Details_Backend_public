"""Rebuild the reviewed C3 additions from cached OSM evidence, without network/DB.

The explicit selection below is an editorial review, not an automated accept-all.
The frozen C2 catalog (126 records) is copied unchanged; additions target the
under-represented priority districts. No routing coordinates are filled: the
Phase C3 entrance/gate evidence documents no public visitor entrance for any
addition (docs/PHASE_C3_ENTRANCE_VERIFICATION.md), so the routing schema ships
unfilled rather than inventing entrances.
"""
from collections import Counter
import json
from pathlib import Path

from scripts._phase_c3_candidate_review import get_sources_boundaries, ACQUISITION_QUERIES
from scripts.validate_phase_b_catalog import canonical_identity, nearby_pairs, point_in_boundary

ROOT = Path(__file__).resolve().parents[1]
CHECKED_ON = "2026-10-10"
PRIORITY_DISTRICTS = {"Приморский район", "Кировский район", "Красносельский район",
                      "Калининский район", "Выборгский район"}
RESTRICTED = {"private", "no", "permit", "permit_required", "restricted", "military", "employees", "customers"}

# kind, OSM id, stable key, category, minutes, unusual, editorial reason.
# No component of an accepted park (its own memorials, playground sculptures)
# is separately accepted; appointment-only corporate museums are excluded.
REVIEWED = [
    # --- Кировский район ---
    ("relation", 3365494, "vorontsovsky-garden", "nature", 20, False,
     "Погулять по Воронцовскому скверу рядом с Ульянкой и устроить зелёную паузу между жилыми кварталами юго-запада."),
    ("way", 32702194, "komsomolsky-garden", "nature", 20, False,
     "Выбрать местную зелёную прогулку в сквере на Дачном проспекте вместо поездки к центральным паркам."),
    ("relation", 6125865, "january-ninth-garden", "nature", 25, False,
     "Пройти по саду Девятого Января — историческому зелёному пространству Автово рядом с проспектом Стачек."),
    ("relation", 15532525, "kiryanovo-garden", "nature", 25, False,
     "Зайти в сад Кирьяново: остатки старинной усадьбы с прудами — редкий для Кировского района пейзажный уголок."),
    ("way", 96364629, "molvinsky-garden", "nature", 20, False,
     "Увидеть Молвинский сад — небольшое зелёное пространство на Тракторной улице с долгой заводской историей."),
    ("way", 141623677, "tentelovsky-square", "nature", 20, False,
     "Пройтись по Тентелевскому скверу и посмотреть, как зелень вписана в плотную застройку Нарвской заставы."),
    ("way", 73174258, "narva-triumphal-gate", "history_architecture", 20, False,
     "Осмотреть Нарвские триумфальные ворота — памятник победе над Наполеоном и главные ворота Нарвской заставы."),
    ("node", 1340264515, "tank-pobeditel", "history_architecture", 15, False,
     "Увидеть легендарный танк-победитель на постаменте — один из самых известных военных памятников проспекта Стачек."),
    ("node", 1368607035, "blockade-tram", "history_architecture", 10, False,
     "Остановиться у вагона, посвящённого блокадным трамваям Ленинграда, прямо у трамвайных путей проспекта Стачек."),
    ("node", 6959416215, "viktor-tsoi-monument", "art", 10, True,
     "Прийти к памятнику Виктору Цою — месту притяжения поклонников рок-музыки в Автово."),
    ("relation", 16463657, "peredny-kray-oborony-square", "nature", 20, False,
     "Погулять по скверу, чьё название хранит память о переднем крае обороны Ленинграда в Лигово."),
    # --- Красносельский район ---
    ("relation", 11014441, "sosnovaya-polyana-park", "nature", 30, False,
     "Прогуляться по большому парку Сосновая Поляна с прудами и аллеями — главному зелёному пространству юго-запада."),
    ("relation", 15879393, "vladimir-shalimov-square", "nature", 15, False,
     "Пройтись по скверу Владимира Шалимова — небольшой зелёной остановке среди кварталов Сосновой Поляны."),
    ("relation", 12737077, "raevsky-square", "nature", 20, False,
     "Отдохнуть в Раевском сквере — зелёном центре кварталов Сосновой Поляны."),
    ("way", 154855800, "general-duhanov-square", "nature", 15, False,
     "Заглянуть в сквер Генерала Духанова — зелёную остановку в западной части Горелово."),
    ("way", 692406919, "komissar-grushko-square", "nature", 15, False,
     "Сделать зелёную остановку в сквере Комиссара Грушко среди жилых кварталов Красносельского района."),
    ("way", 1006790808, "mikhail-ashik-square", "nature", 15, False,
     "Погулять по скверу Михаила Ашика — военного инженера, строившего укрепления Лиговского рубежа."),
    ("node", 8702586806, "ligovo-staro-panovo-defenders", "history_architecture", 20, False,
     "Осмотреть мемориал защитникам Лигово и Старо-Паново на Урицком рубеже обороны Ленинграда."),
    ("node", 1519933984, "tree-of-life-sculpture", "art", 10, True,
     "Найти скульптуру «Древо Жизни» — необычный арт-объект среди кварталов Ульянки."),
    # --- Приморский район ---
    ("relation", 20398684, "golovinsky-garden", "nature", 20, False,
     "Погулять по Головинскому саду — одной из старейших зелёных зон Приморского района."),
    ("relation", 2055286, "saltykovsky-garden", "nature", 20, False,
     "Отдохнуть в Салтыковском саду с его прудами — тихом саду среди кварталов Старой Деревни."),
    ("relation", 6282307, "stroganovsky-park", "nature", 30, False,
     "Прогуляться по Строгановскому парку — остаткам пейзажного парка Строгановской дачи XVIII века."),
    ("relation", 9443671, "dolgoe-lake-park", "nature", 30, False,
     "Сделать прогулку вокруг озера Долгое — большого парка среди многоэтажных кварталов на западе района."),
    ("way", 143828654, "matsievich-square", "nature", 15, False,
     "Пройтись по скверу Мациевича, названному в честь лётчика Льва Мациевича, недалеко от Авиагородка."),
    ("node", 14158556601, "yuntolovsky-reserve", "nature", 40, False,
     "Погулять по экологическим тропам заказника Юнтолово — большого лесоболотного резервата на северо-западе города."),
    ("node", 8107491427, "faina-ranevskaya-monument", "art", 10, True,
     "Улыбнуться бронзовой Фаине Раневской — памятнику великой актрисе у Богатырского проспекта."),
    ("way", 143828757, "orlovy-denisovy-estate-park", "nature", 25, False,
     "Пройтись по остаткам парка усадьбы Орловых-Денисовых среди кварталов Коломяг."),
    # --- Калининский район ---
    ("relation", 3793464, "lyubashinsky-garden", "nature", 30, False,
     "Погулять по Любашинскому саду с его прудами — большому саду среди кварталов Полюстрова."),
    ("way", 206639567, "kondratyevsky-garden", "nature", 25, False,
     "Отдохнуть в Кондратьевском саду — историческом зелёном саду в рабочей части района."),
    ("relation", 13417031, "piscaryovsky-park", "nature", 35, False,
     "Пройтись по большому Пискарёвскому парку рядом со знаменитым мемориальным кладбищем."),
    ("way", 261805529, "sverdlovsky-garden", "nature", 20, False,
     "Сделать паузу в Свердловском саду — небольшом саду недалеко от Финляндского вокзала."),
    ("relation", 11711880, "bezborodko-square", "nature", 20, False,
     "Погулять по скверу Безбородко — парку усадьбы Безбородко с прудами и старыми деревьями у Полюстрова."),
    ("relation", 8850275, "lenin-armored-car", "history_architecture", 15, False,
     "Увидеть знаменитый памятник Ленину на броневике у Финляндского вокзала — один из первых монументов города."),
    ("node", 320034011, "chapaev-monument", "history_architecture", 15, False,
     "Рассмотреть динамичный памятник Чапаеву на броневике перед Политехническим институтом."),
    ("node", 4171703961, "children-of-war-memorial", "history_architecture", 15, False,
     "Остановиться у памятника «Дети войны» — скульптурной группе о блокадном детстве."),
    ("way", 245659313, "zhores-alfyorov-garden", "nature", 20, False,
     "Пройтись по саду Жореса Алфёрова — новой зелёной территории среди кварталов северной части района."),
    ("node", 4096416490, "grazhdanka-aerodrome-monument", "history_architecture", 10, True,
     "Найти памятный знак на месте аэродрома Гражданка — необычную стелу на бывшем лётном поле."),
    # --- Выборгский район ---
    ("way", 23772140, "polyustrovsky-garden", "nature", 25, False,
     "Погулять по Полюстровскому саду с прудами на месте знаменитых Полюстровских минеральных вод."),
    ("relation", 20276219, "lanskoy-garden", "nature", 25, False,
     "Пройтись по Ланскому саду — обновлённому зелёному пространству в старой части Лесного."),
    ("way", 769365054, "blockade-survivors-square", "nature", 20, False,
     "Зайти в сквер блокадников — зелёный уголок, посвящённый жителям блокадного Ленинграда."),
    ("relation", 12114536, "serebryany-prud-garden", "nature", 20, False,
     "Отдохнуть у Серебряного пруда — тихого сада среди кварталов Лесного."),
    ("relation", 11711397, "ivan-fomin-garden", "nature", 25, False,
     "Погулять по саду Ивана Фомина с его известным террасным рельефом у Муринского ручья."),
    ("relation", 7499371, "north-valley-alley", "nature", 20, False,
     "Пройтись по аллее Северной долины — длинной зелёной оси в северной части района."),
    ("relation", 5925062, "vyborgsky-garden", "nature", 25, False,
     "Пройтись по Выборгскому саду — историческому саду у Лесного проспекта, разбитому ещё в XIX веке."),
    ("node", 5108364076, "blockade-cats-monument", "art", 10, True,
     "Найти памятник кошкам, спасшим город и Эрмитаж от крыс после блокады, — трогательная история в бронзе."),
    ("node", 448465650, "novosiltsev-chernov-duel-place", "history_architecture", 15, True,
     "Найти стелу на месте дуэли Новосильцева и Чернова 1825 года — печально известного события пушкинской эпохи."),
    ("node", 4874241451, "military-dog-handlers-memorial", "history_architecture", 15, False,
     "Увидеть в Сосновском парке памятник военным дрессировщикам и служебным собакам Ленинградского фронта."),
]

EXCLUDED_NOTABLE = {
    ("node", 9178009893): "Дом с самолётом находится западнее задокументированной городской границы каталога (долгота 30.10); не принимается без пересмотра границы.",
    ("way", 1292316232): "Площадь Солнца находится западнее задокументированной городской границы каталога (долгота 30.10); не принимается без пересмотра границы.",
    ("node", 3514428815): "Корпоративный музей Кировского завода: посещение в рабочие часы предприятия, публичность подхода не подтверждена официальным источником.",
    ("node", 1431722815): "Музей истории Политеха: посещение по записи, туристическая пешая остановка не подтверждена.",
    ("node", 3567008095): "Музей истории пивоварения: экскурсии по предварительной записи на заводе, публичность подхода не подтверждена.",
    ("node", 3567020589): "Музей ГАИ: ведомственный музей с ограниченным расписанием; публичность не подтверждена.",
    ("node", 659405364): "Музей подводных сил РФ: посещение требует согласования; как пешая остановка не подтверждён.",
    ("node", 2008725261): "Левашовская пустошь: действующее мемориальное кладбище; статус отдельных захоронений не позволяет принять объект в этой фазе.",
    ("node", 7820233871): "Мемориальная доска на здании; для пешей прогулки недостаточно самостоятельной ценности.",
    ("way", 1097095771): "Музей-ДОТ: открыт эпизодически и по записи прихода; публичность подхода не подтверждена.",
    ("node", 4556959789): "Стела Партизану Герману расположена внутри принимаемого парка Сосновая Поляна; отдельным пунктом не добавляется.",
    ("node", 5819196743): "Единственный документированный вход Молвинского сада помечен access=private; не является публичным входом.",
    ("relation", 19027233): "Территория Новознаменка уже представлена в каталоге другим каноническим объектом OSM relation/19027234 (запись Phase C2 novoznamenka-park); повторное название парка не добавляется.",
}


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def query_for_identity(sources_files):
    def lookup(identity):
        for query, elements in sources_files.items():
            if identity in elements:
                return query, elements[identity]
        return None, None
    return lookup


def main():
    sources, boundaries = get_sources_boundaries()
    old = json.loads((ROOT / "app/db/seed_places.json").read_text(encoding="utf-8"))
    assert len(old) == 126, len(old)
    # Freeze the C2 catalog for the additive C3 gate (validate_phase_c2_catalog).
    write_json(ROOT / "docs/phase_c2_catalog_baseline.json", old)

    # Map every acquired element to the query that delivered it (for the
    # per-item coordinate snapshot timestamp).
    request_log = json.loads((ROOT / "docs/phase_c3_acquisition/requests.json").read_text(encoding="utf-8"))
    query_files = {}
    for entry in request_log:
        if entry.get("status") != "success":
            continue
        base = entry["query_id"].split("_retry", 1)[0]
        for alias in ("_de", "_mail", "_ch", "_kumi"):
            if base.endswith(alias):
                base = base[: -len(alias)]
                break
        if base in ACQUISITION_QUERIES:
            # Response files are stored under the alias-stable name without
            # the retry-attempt suffix.
            query_files[base] = entry["query_id"].split("_retry", 1)[0] + ".json"
    per_query_elements = {}
    per_query_timestamp = {}
    for query, filename in query_files.items():
        raw = json.loads((ROOT / "docs/phase_c3_acquisition" / filename).read_text(encoding="utf-8"))
        per_query_elements[query] = {(e["type"], e["id"]): e for e in raw["elements"]}
        per_query_timestamp[query] = raw["osm3s"]["timestamp_osm_base"]

    additions, accepted_sources = [], []
    for kind, osm_id, key, category, minutes, unusual, reason in REVIEWED:
        identity = (kind, osm_id)
        query, source = query_for_identity(per_query_elements)(identity)
        if source is None:
            raise ValueError(f"{key}: canonical source absent from C3 acquisition evidence")
        tags = source["tags"]
        point = source if kind == "node" else source["center"]
        inside = [b for b in boundaries["boundaries"] if point_in_boundary(point["lat"], point["lon"], b)]
        assert len(inside) == 1, (key, inside)
        district = inside[0]["district"]
        assert district in PRIORITY_DISTRICTS, (key, district)
        if tags.get("access") in RESTRICTED or tags.get("foot") in RESTRICTED:
            raise ValueError(f"{key}: restricted source accepted")
        title = tags.get("name:ru", tags["name"]).replace("\u00a0", " ")
        is_park = tags.get("leisure") in {"park", "garden"}
        is_gate = key == "narva-triumphal-gate"
        mode = ("park_walk" if is_park else "exterior" if is_gate else
                "outdoor_memorial" if category == "history_architecture" else "outdoor_monument")
        snapshot_ts = per_query_timestamp[query]
        short = (f"{title} — зелёная остановка для местной прогулки ({district})." if is_park
                 else f"{title} — {'архитектурная' if is_gate else 'памятная' if category == 'history_architecture' else 'художественная'} остановка ({district}).")
        caution = "Доступ и пешеходный подход независимо не проверены. "
        if is_park:
            note = caution + f"{minutes} минут — прогулка по части территории, без обязательного обхода всего парка или сквера. Точка показывает объект, а не проверенный вход."
        elif is_gate:
            note = caution + "Заложен внешний осмотр ворот с доступного общественного пространства; посещение музея внутри ворот не предполагается."
        elif category == "history_architecture":
            note = caution + f"{minutes} минут — осмотр памятника и короткая остановка; точка не является подтверждённым местом подхода."
        else:
            note = caution + f"{minutes} минут — осмотр скульптуры и короткая остановка; точка не является подтверждённым местом подхода."
        street, number = tags.get("addr:street"), tags.get("addr:housenumber")
        address = ", ".join(str(x) for x in (street, number) if x) if street or number else f"Санкт-Петербург, {district}; местоположение по картографической точке"
        url = f"https://www.openstreetmap.org/{kind}/{osm_id}"
        if is_park:
            topic = "парк, местная прогулка, зелёное пространство"
        elif is_gate:
            topic = "триумфальные ворота, архитектура, история"
        elif category == "history_architecture":
            topic = "памятник, память, местная прогулка"
        else:
            topic = "скульптура, искусство, местная прогулка"
        if kind == "node":
            uncertainty = (f"Картографический узел объекта из снимка Overpass от {snapshot_ts}; текущий узел проверен {CHECKED_ON}. "
                           "Точность и публичный подход не проверены независимо.")
        else:
            uncertainty = (f"Центр ограничивающего прямоугольника из снимка Overpass от {snapshot_ts}; актуальность геометрии не проверена. "
                           "Может находиться внутри территории и не обозначает вход.")
        additions.append({
            "catalog_key": key, "title": title, "short_description": short,
            "full_description": reason + " Остановка рассчитана на указанный вид осмотра. Перед выходом учитывайте, что физический доступ и точный пешеходный подход ещё требуют проверки.",
            "category": category, "is_unusual": unusual,
            "latitude": point["lat"], "longitude": point["lon"], "address": address,
            "photo_url": None, "working_hours": None, "tags": topic, "rating": None,
            "architectural_style": None, "year_built": None, "source_url": url,
            "coordinate_kind": "osm_node" if kind == "node" else "osm_bounds_center",
            "source_checked_on": CHECKED_ON,
            "address_status": "osm_tags" if any(k.startswith("addr:") for k in tags) else "location_description_not_verified",
            "recommended_visit_minutes": minutes, "why_visit": reason, "visit_note": note,
            "official_website": tags.get("website", tags.get("contact:website")),
            "wikipedia_url": (f"https://ru.wikipedia.org/wiki/{tags['wikipedia'].split(':', 1)[1].replace(' ', '_')}"
                              if tags.get("wikipedia", "").startswith("ru:") else None),
            "coordinate_source_url": url, "coordinate_uncertainty": uncertainty,
            "coordinate_snapshot_timestamp": snapshot_ts, "current_object_checked_on": CHECKED_ON,
            "access_point_status": "representative_point_not_verified", "district": district,
            "visit_mode": mode,
            "verification_evidence": (f"Координаты и исходные теги {kind}/{osm_id} сохранены из {query} (снимок Overpass от {snapshot_ts}); "
                                      f"текущие теги объекта отдельно проверены {CHECKED_ON} через официальный OSM API. "
                                      f"Район проверен по полигону OSM relation/{inside[0]['osm_relation_id']}. Доступ и вход не подтверждены."),
        })
        accepted_sources.append(source)

    items = old + additions
    assert len(additions) == 47, len(additions)
    write_json(ROOT / "app/db/seed_places.json", items)
    write_json(ROOT / "docs/phase_c3_osm_source_snapshot.json", {
        "checked_on": CHECKED_ON,
        "source": "OpenStreetMap via cached bounded Overpass GET (overpass-api.de, maps.mail.ru)",
        "license": "ODbL 1.0", "attribution": "© OpenStreetMap contributors",
        "acquisition_evidence": sorted(query_files.values()),
        "coordinate_snapshot_timestamps": per_query_timestamp,
        "current_object_evidence": [f"phase_c3_acquisition/current_{kind}_{i}.json" for kind in ("node", "way", "relation") for i in range(4)],
        "scope": "Exactly the 47 C3 additions; union with unchanged osm_source_snapshot.json and phase_c2_osm_source_snapshot.json for the complete catalog.",
        "elements": accepted_sources})
    write_json(ROOT / "docs/phase_c3_boundary_note.json", {
        "note": "No new district boundaries acquired; all C3 additions fall inside the frozen Phase B/C2 polygons.",
        "districts_used": sorted({i["district"] for i in additions})})
    assignments = json.loads((ROOT / "docs/phase_c2_district_assignments.json").read_text(encoding="utf-8"))
    for item in additions:
        b = next(b for b in boundaries["boundaries"] if b["district"] == item["district"])
        assignments[item["catalog_key"]] = {k: item[k] for k in ("catalog_key", "title", "latitude", "longitude", "district")}
        assignments[item["catalog_key"]].update(osm_boundary_url=b["osm_boundary_url"], assignment_method="osm_boundary_point_in_polygon", source_checked_on=CHECKED_ON)
    write_json(ROOT / "docs/phase_c3_district_assignments.json", assignments)

    # Candidate ledger: every named priority-district candidate from the
    # acquisition gets an explicit decision.
    from scripts._phase_c3_candidate_review import candidate_rows
    catalog_urls = {i["source_url"] for i in old}
    accepted_identities = {(s["type"], s["id"]) for s in accepted_sources}
    ledger = []
    for row in candidate_rows(sources, boundaries, catalog_urls):
        district, kind, osm_id, name, lat, lon, tags = row
        identity = (kind, osm_id)
        if identity in accepted_identities:
            status, reason = "accepted", "Explicit editorial selection; representative coordinate and access uncertainty retained."
        elif identity in EXCLUDED_NOTABLE:
            status, reason = "excluded", EXCLUDED_NOTABLE[identity]
        elif tags.get("access") in {"private", "no", "permit", "military"} or tags.get("foot") in {"no", "private"}:
            status, reason = "excluded", "Explicit restricted-access tag."
        elif tags.get("memorial") == "plaque" or tags.get("memorial") in {"cross", "grave"}:
            status, reason = "not_accepted", "Memorial plaque/cross/grave; insufficient standalone visitor activity for a walking stop."
        elif tags.get("tourism") == "museum":
            status, reason = "not_accepted", "Museum with unconfirmed public visiting conditions; no verified walk-in stop documented in this phase."
        else:
            status, reason = "not_accepted", ("Not selected: visitor usefulness, spatial spread or independence from another site "
                                              "not sufficiently established in this phase. No claim that this object is inaccessible.")
        ledger.append({"osm_type": kind, "osm_id": osm_id, "district": district, "name": name,
                       "status": status, "reason": reason})
    write_json(ROOT / "docs/phase_c3_candidate_review.json", {
        "candidate_count": len(ledger), "accepted_count": len(additions), "reviews": ledger})

    pairs = nearby_pairs(items)
    write_json(ROOT / "docs/phase_c3_nearby_pairs.json", pairs)
    print(json.dumps({"added": len(additions), "total": len(items),
                      "new_district_counts": dict(Counter(i["district"] for i in additions)),
                      "close_pairs": pairs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
