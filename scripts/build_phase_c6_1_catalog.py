"""Rebuild the reviewed C6.1 additions from cached OSM evidence, without network/DB.

Run from the repository root: python -m scripts.build_phase_c6_1_catalog
The seven additions are an explicit editorial review bound to canonical OSM
objects acquired on 2026-10-11 (docs/phase_c6_1_acquisition). Address and
operating status were re-checked against each attraction's official source
(docs/phase_c6_1_source_verification.json). Coordinates are representative
points from the OSM snapshot, never verified entrances; the routing schema
ships unfilled.
"""
from collections import Counter
import json

from scripts._phase_c6_1_candidate_review import (
    CHECKED_ON, NOT_ACCEPTED, PRIOR_CATALOG_COUNT, ROOT, SELECTED,
    acquisition_queries, binding_successes, boundaries, per_query_data, read_json,
    selected_sources,
)
from scripts.validate_phase_b_catalog import nearby_pairs, point_in_boundary

# Editorial review. Mechanical fields (coordinates, source URL, snapshot
# timestamp, district, official links) are derived from the OSM evidence below.
REVIEWED = {
    "museum-of-optics-itmo": {
        "title": "Музей оптики ИТМО",
        "category": "science_technology", "minutes": 60, "unusual": True, "visit_mode": "indoor_museum",
        "address": "Биржевая линия, 14", "address_status": "official_source_verified",
        "short": "Интерактивный музей оптики Университета ИТМО на Биржевой линии Васильевского острова.",
        "full": "Музей оптики — интерактивная площадка Университета ИТМО: экспозиция посвящена оптике, "
                "голографии и оптическим иллюзиям. Вместо привычных витрин — зеркальные залы, "
                "голограммы и экспонаты, с которыми можно взаимодействовать. Небольшой музей хорошо "
                "подходит для самостоятельного визита и для дождливого дня.",
        "why": "Наука, которую можно потрогать: голограммы и зеркальные залы в центре Васильевского острова.",
        "note": "Доступ и пешеходный подход независимо не проверены. 60 минут — осмотр экспозиции; "
                "расписание сеансов и билеты уточняйте на официальном сайте музея.",
        "tags": "музей, оптика, голограммы, интерактив, наука, васильевский остров, в дождь, с детьми",
        "official_website": None,  # OSM website tag (http://optimus.ifmo.ru) was dead on 2026-10-11
        "web_note": "Тег website в OSM-объекте (http://optimus.ifmo.ru) на дату проверки не открывался, "
                    "поэтому ссылка в карточке не указана.",
    },
    "glushko-space-museum": {
        "title": "Музей космонавтики и ракетной техники им. В. П. Глушко",
        "category": "science_technology", "minutes": 60, "unusual": True, "visit_mode": "indoor_museum",
        "address": "Петропавловская крепость, Иоанновский равелин",
        "address_status": "official_source_verified",
        "short": "Музей космонавтики и ракетной техники в Иоанновском равелине Петропавловской крепости.",
        "full": "Экспозиция посвящена раннему периоду отечественной космонавтики и ракетной техники: "
                "технологические образцы, документы и воссозданные кабинеты конструкторов. Музей "
                "расположен в Иоанновском равелине Петропавловской крепости и работает как часть "
                "Государственного музея истории Санкт-Петербурга — его удобно добавить к прогулке "
                "по крепости.",
        "why": "Космическая экспозиция внутри крепостных валов — неожиданный сюжет обычной прогулки "
               "по Петропавловской крепости.",
        "note": "Доступ и пешеходный подход независимо не проверены. 60 минут — осмотр экспозиции; "
                "часы работы и билеты уточняйте на официальном сайте музея.",
        "tags": "музей, космонавтика, ракетная техника, глушко, петропавловская крепость, наука, "
                "в дождь, с детьми",
        "web_note": None,
    },
    "vselennaya-vody": {
        "title": "Музей «Вселенная воды»",
        "category": "science_technology", "minutes": 75, "unusual": True, "visit_mode": "indoor_museum",
        "address": "Шпалерная улица, 56", "address_status": "official_source_verified",
        "short": "Музейный комплекс о воде и городском водоснабжении на Шпалерной улице.",
        "full": "Экспозиционно-выставочный комплекс «Вселенная Воды» рассказывает о воде и "
                "водоснабжении Петербурга. Исторические резервуары и водонапорная башня превращены "
                "в мультимедийные залы с экспозициями «Мир воды Санкт-Петербурга», «Подземный мир "
                "Петербурга» и «Вселенная Воды». Часть экспозиций открыта только в составе экскурсии — "
                "это стоит учесть при планировании.",
        "why": "Понятный рассказ о том, как вода попадает в город: водонапорная башня и старые "
               "резервуары стали мультимедийными залами.",
        "note": "Доступ и пешеходный подход независимо не проверены. 75 минут — осмотр основной "
                "экспозиции; часть залов доступна только с экскурсией, расписание уточняйте на "
                "официальном сайте музея.",
        "tags": "музей, вода, водоканал, водонапорная башня, мультимедиа, наука, в дождь, с детьми",
        "web_note": None,
    },
    "etagi-loft-project": {
        "title": "Лофт-проект «Этажи»",
        "category": "modern_spaces", "minutes": 60, "unusual": True, "visit_mode": "indoor_museum",
        "address": "Лиговский проспект, 74", "address_status": "osm_tags",
        "short": "Лофт-проект в здании бывшего хлебозавода на Лиговском проспекте: галереи, выставки "
                 "и смотровая площадка на крыше.",
        "full": "«Этажи» занимают здание бывшего хлебозавода на Лиговском проспекте. На пяти этажах "
                "работают галереи, выставочные пространства и кафе, а на крыше обустроена смотровая "
                "площадка с видом на Лиговский и окрестные кварталы. Хорошая остановка, чтобы "
                "посмотреть современную городскую культуру между центром и Обводным каналом.",
        "why": "Выставки и галереи в промышленных интерьерах бывшего хлебозавода — и крыша "
               "с панорамой Лиговского проспекта.",
        "note": "Доступ и пешеходный подход независимо не проверены. 60 минут — осмотр галерей; "
                "подъём на крышу и часы работы площадок уточняйте на официальном сайте проекта.",
        "tags": "лофт, выставки, галереи, крыша, смотровая площадка, современная культура, "
                "лиговский проспект, в дождь",
        "web_note": None,
    },
    "pushkinskaya-10-art-centre": {
        "title": "Арт-центр «Пушкинская-10»",
        "category": "modern_spaces", "minutes": 60, "unusual": True, "visit_mode": "indoor_museum",
        "address": "Пушкинская улица, 10 (вход с Лиговского проспекта, 53)",
        "address_status": "official_source_verified",
        "short": "Арт-центр независимого искусства с музеями, галереями и двором на Пушкинской улице.",
        "full": "«Пушкинская-10» — арт-центр, который с 1989 года объединяет независимые музеи и "
                "галереи: Музей нонконформистского искусства, Музей рок-музыки и интерактивный "
                "Музей Звука. Здесь проходят выставки и концерты, а во дворе находится «улица "
                "Джона Леннона». Это место неформальной художественной жизни города, которое стоит "
                "посмотреть рядом с Лиговским проспектом.",
        "why": "Неформальная сторона петербургского искусства: музеи андеграунда, галереи и двор "
               "с «улицей Джона Леннона».",
        "note": "Доступ и пешеходный подход независимо не проверены. 60 минут — осмотр галерей и "
                "музеев; часы работы уточняйте на официальном сайте арт-центра.",
        "tags": "арт-центр, неформальное искусство, музеи, галереи, концерты, музыка, "
                "лиговский проспект, в дождь",
        "web_note": None,
    },
    "elagin-island-central-park": {
        "title": "ЦПКиО им. С. М. Кирова (Елагин остров)",
        "category": "family_leisure", "minutes": 90, "unusual": False, "visit_mode": "park_walk",
        "address": "Елагин остров, 4, литера Б", "address_status": "official_source_verified",
        "short": "Центральный парк культуры и отдыха на Елагином острове: аллеи, пруды и выходы к воде.",
        "full": "ЦПКиО имени С. М. Кирова занимает весь Елагин остров между Большой и Средней Невками. "
                "На территории — регулярные аллеи, пруды, луговая часть и выходы к воде; здесь же "
                "находятся Елагин дворец, конюшенный и оранжерейный корпуса. Остров объявлен памятником "
                "природы, поэтому для прогулки стоит выбрать один-два маршрута, а не пытаться обойти "
                "всю территорию сразу.",
        "why": "Парк на целом острове: аллеи, пруды и виды на Невки — спокойный вариант семейного выхода.",
        "note": "Доступ и пешеходный подход независимо не проверены. 90 минут — прогулка по части "
                "парка, без обязательного обхода всего острова; точку входа и часы работы уточняйте "
                "на официальном сайте парка.",
        "tags": "парк, елагин остров, прогулка, пруды, семья, с детьми, природа, выход к воде",
        "web_note": None,
    },
    "volshebnaya-milya-park": {
        "title": "Парк «Волшебная миля»",
        "category": "family_leisure", "minutes": 90, "unusual": True, "visit_mode": "theme_park",
        "address": "проспект Энгельса, 154 (ТРК «Гранд Каньон»)", "address_status": "osm_tags",
        "short": "Детский парк развлечений и открытий на первом этаже ТРК «Гранд Каньон» "
                 "с отдельным входом с улицы Шостаковича.",
        "full": "«Волшебная миля» — семейный парк площадью около 3000 м² внутри торгового комплекса "
                "«Гранд Каньон». Посетителей ждут зеркальный лабиринт, сферический кинотеатр и "
                "игровые зоны для детей и родителей. Компактный формат делает парк удобным вариантом "
                "для семейного дня независимо от погоды.",
        "why": "Погода за окном не важна: парк открытий в торговом комплексе рядом с метро "
               "«Проспект Просвещения».",
        "note": "Доступ и пешеходный подход независимо не проверены. 90 минут — посещение основной "
                "части парка; отдельный вход с улицы Шостаковича, правила посещения и билеты "
                "уточняйте на официальном сайте парка.",
        "tags": "парк развлечений, дети, с детьми, лабиринт, кинотеатр, гранд каньон, в дождь, "
                "выходной день",
        "web_note": None,
    },
}

# Official-source checks behind address_status="official_source_verified" and the
# operating-status review. Recorded machine-readably for the source offer.
SOURCE_VERIFICATION = {
    "museum-of-optics-itmo": {
        "official_source": "https://optimus.itmo.ru/ (данные Университета ИТМО о музее)",
        "checked_on": CHECKED_ON,
        "confirms": ["name", "address", "operating_status"],
        "imprint": "Биржевая линия, 14; музей Университета ИТМО работает по расписанию (вт–чт 11:00–20:00, пт–вс 11:00–21:00, пн — выходной); интерактивная экспозиция об оптике, голографии и оптических иллюзиях подтверждена официальными данными ИТМО.",
    },
    "glushko-space-museum": {
        "official_source": "https://www.spbmuseum.ru/exhibits_and_exhibitions/permanent_displays/48241/",
        "checked_on": CHECKED_ON,
        "confirms": ["name", "address", "operating_status"],
        "imprint": "Петропавловская крепость, Иоанновский равелин; музей Государственного музея истории Санкт-Петербурга, среда — выходной; экспозиция о ракетной технике и космонавтике подтверждена страницей постоянной экспозиции и тегами OSM-объекта.",
    },
    "vselennaya-vody": {
        "official_source": "http://vodokanal-museum.ru/",
        "checked_on": CHECKED_ON,
        "confirms": ["name", "address", "operating_status"],
        "imprint": "Шпалерная, 56; на сайте опубликовано расписание на октябрь 2026 года, признаков закрытия нет; экспозиции «Мир воды Санкт-Петербурга», «Подземный мир Петербурга» и «Вселенная Воды» перечислены на официальном сайте, экскурсионный порядок части залов — там же.",
    },
    "etagi-loft-project": {
        "official_source": "https://www.loftprojectetagi.ru/",
        "checked_on": CHECKED_ON,
        "confirms": ["name", "operating_status"],
        "imprint": "Адрес подтверждён тегами OSM (Лиговский проспект, 74); этажность (building:levels=5) — из OSM-объекта; сведения о здании бывшего хлебозавода и составе площадок — по статье Википедии «Этажи (выставочный центр)» (вспомогательный источник).",
    },
    "pushkinskaya-10-art-centre": {
        "official_source": "https://p-10.ru/contacts",
        "checked_on": CHECKED_ON,
        "confirms": ["name", "address", "operating_status"],
        "imprint": "191040, Санкт-Петербург, Пушкинская ул., дом 10; вход с Лиговского пр. 53, под арку; музеи и галереи работают; Музей нонконформистского искусства, Музей рок-музыки, Музей Звука и «улица Джона Леннона» перечислены на официальном сайте; год начала работы (1989) — тег start_date OSM-объекта.",
    },
    "elagin-island-central-park": {
        "official_source": "https://elaginpark.org/ (СПб ГБУК «ЦПКиО им. С. М. Кирова»)",
        "checked_on": CHECKED_ON,
        "confirms": ["name", "address", "operating_status"],
        "imprint": "Елагин остров, 4, литера Б; парк работает по сезонному расписанию; статус памятника природы подтверждён объектом OSM relation/2965059 и данными ООПТ Санкт-Петербурга (oopt.spb.ru).",
    },
    "volshebnaya-milya-park": {
        "official_source": "https://magicmile.ru/",
        "checked_on": CHECKED_ON,
        "confirms": ["name", "operating_status"],
        "imprint": "Реквизиты: ТРК «Гранд Каньон», 1 этаж, вход с улицы Шостаковича; адрес подтверждён тегами OSM (проспект Энгельса, 154); зеркальный лабиринт, сферический кинотеатр, площадки и площадь парка — по официальному сайту и официальной странице арендатора ТРК.",
    },
}

DISTRICT_NAMES = ("Василеостровский район", "Петроградский район", "Центральный район",
                  "Выборгский район")


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    old = read_json(ROOT / "app/db/seed_places.json")
    assert len(old) == PRIOR_CATALOG_COUNT, len(old)
    # Freeze every earlier window by digest before appending.
    from scripts.validate_phase_b_catalog import catalog_digest
    manifests = {name: read_json(ROOT / "docs" / name)
                 for name in ("phase_b_catalog_manifest.json", "phase_c2_catalog_manifest.json",
                              "phase_c3_catalog_manifest.json")}
    assert catalog_digest(old[:173]) == manifests["phase_c3_catalog_manifest.json"]["catalog_sha256"]
    assert catalog_digest(old[:126]) == manifests["phase_c2_catalog_manifest.json"]["catalog_sha256"]
    assert catalog_digest(old[:77]) == manifests["phase_b_catalog_manifest.json"]["catalog_sha256"]

    sources, query_of = selected_sources()
    _, timestamps = per_query_data()
    boundary_evidence = boundaries()
    bindings = binding_successes(acquisition_queries())
    assert len(bindings) == len(SELECTED)

    additions, accepted_sources = [], []
    for key, identity in SELECTED.items():
        review = REVIEWED[key]
        assert identity in sources, (key, identity)
        source = sources[identity]
        tags = source["tags"]
        point = source if identity[0] == "node" else source["center"]
        inside = [b for b in boundary_evidence["boundaries"]
                  if point_in_boundary(point["lat"], point["lon"], b)]
        assert len(inside) == 1, (key, inside)
        district = inside[0]["district"]
        assert district in DISTRICT_NAMES, (key, district)
        snapshot_ts = timestamps[query_of[identity]]
        url = f"https://www.openstreetmap.org/{identity[0]}/{identity[1]}"
        if identity[0] == "node":
            uncertainty = (f"Картографический узел объекта из снимка Overpass от {snapshot_ts}; "
                           f"текущий узел проверен {CHECKED_ON}. Точность и публичный подход "
                           "не проверены независимо.")
        else:
            uncertainty = (f"Центр ограничивающего прямоугольника из снимка Overpass от {snapshot_ts}; "
                           "актуальность геометрии не проверена. Может находиться внутри территории "
                           "и не обозначать вход.")
        official = review["official_website"] if "official_website" in review else (
            tags.get("website") or tags.get("contact:website"))
        wikipedia = None
        if tags.get("wikipedia", "").startswith("ru:"):
            wikipedia = f"https://ru.wikipedia.org/wiki/{tags['wikipedia'].split(':', 1)[1].replace(' ', '_')}"
        evidence = (f"Координаты и исходные теги {identity[0]}/{identity[1]} сохранены из "
                    f"{query_of[identity]} (снимок Overpass от {snapshot_ts}); текущие теги объекта "
                    f"отдельно проверены {CHECKED_ON} через официальный OSM API. Район проверен по "
                    f"полигону OSM relation/{inside[0]['osm_relation_id']}. "
                    f"Адрес и статус работы сверены с официальным источником "
                    f"{SOURCE_VERIFICATION[key]['official_source']} {CHECKED_ON}. ")
        if review["web_note"]:
            evidence += review["web_note"] + " "
        evidence += "Доступ и вход не подтверждены."
        record = {
            "catalog_key": key, "title": review["title"], "short_description": review["short"],
            "full_description": review["full"], "category": review["category"],
            "is_unusual": review["unusual"], "latitude": point["lat"], "longitude": point["lon"],
            "address": review["address"], "photo_url": None, "working_hours": None,
            "tags": review["tags"], "rating": None, "architectural_style": None, "year_built": None,
            "source_url": url,
            "coordinate_kind": "osm_node" if identity[0] == "node" else "osm_bounds_center",
            "source_checked_on": CHECKED_ON, "address_status": review["address_status"],
            "recommended_visit_minutes": review["minutes"], "why_visit": review["why"],
            "visit_note": review["note"], "official_website": official, "wikipedia_url": wikipedia,
            "coordinate_source_url": url, "coordinate_uncertainty": uncertainty,
            "coordinate_snapshot_timestamp": snapshot_ts, "current_object_checked_on": CHECKED_ON,
            "access_point_status": "representative_point_not_verified", "district": district,
            "visit_mode": review["visit_mode"], "verification_evidence": evidence,
        }
        additions.append(record)
        accepted_sources.append(source)

    items = old + additions
    assert len(additions) == len(SELECTED) == 7
    write_json(ROOT / "app/db/seed_places.json", items)
    write_json(ROOT / "docs/phase_c6_1_osm_source_snapshot.json", {
        "checked_on": CHECKED_ON,
        "source": "OpenStreetMap via cached bounded Overpass GET (overpass-api.de, maps.mail.ru, overpass.kumi.systems)",
        "license": "ODbL 1.0", "attribution": "© OpenStreetMap contributors",
        "acquisition_evidence": sorted(entry["response_file"] for entry in bindings.values()),
        "coordinate_snapshot_timestamps": timestamps,
        "current_object_evidence": ["phase_c6_1_acquisition/current_node_0.json",
                                    "phase_c6_1_acquisition/current_way_0.json",
                                    "phase_c6_1_acquisition/current_relation_0.json"],
        "scope": "Exactly the 7 C6.1 additions; union with the unchanged Phase B/C2/C3 snapshots "
                 "for the complete catalog.",
        "elements": accepted_sources})

    assignments = {}
    for item in additions:
        b = next(b for b in boundary_evidence["boundaries"] if b["district"] == item["district"])
        assignments[item["catalog_key"]] = {
            **{field: item[field] for field in ("catalog_key", "title", "latitude", "longitude", "district")},
            "osm_boundary_url": b["osm_boundary_url"],
            "assignment_method": "osm_boundary_point_in_polygon",
            "source_checked_on": CHECKED_ON,
        }
    write_json(ROOT / "docs/phase_c6_1_district_assignments.json", assignments)

    write_json(ROOT / "docs/phase_c6_1_boundary_note.json", {
        "note": "No new district boundaries acquired; all C6.1 additions fall inside the frozen "
                "Phase B/C2 polygons.",
        "districts_used": sorted({i["district"] for i in additions})})
    write_json(ROOT / "docs/phase_c6_1_source_verification.json", {
        "checked_on": CHECKED_ON,
        "scope": "Official-source checks for the seven C6.1 additions (name, address, operating status).",
        "attractions": SOURCE_VERIFICATION})

    # Candidate ledger: every named candidate delivered by the seven bounded
    # queries gets an explicit decision.
    accepted_identities = {(s["type"], s["id"]) for s in accepted_sources}
    catalog_urls = {i["source_url"] for i in old}
    rows = []
    elements_by_query, _ = per_query_data()
    for base in sorted(elements_by_query):
        for (kind, osm_id), element in sorted(elements_by_query[base].items(),
                                              key=lambda pair: pair[0]):
            tags = element.get("tags", {})
            name = tags.get("name:ru") or tags.get("name")
            if not name or not tags.get("name"):
                continue
            url = f"https://www.openstreetmap.org/{kind}/{osm_id}"
            if url in catalog_urls:
                continue
            if (kind, osm_id) in accepted_identities:
                status, reason = "accepted", "Explicit editorial selection; representative coordinate and access uncertainty retained."
            elif (kind, osm_id) in NOT_ACCEPTED:
                status, reason = "not_accepted", NOT_ACCEPTED[(kind, osm_id)]
            else:
                status, reason = "not_accepted", ("Not selected: outside the seven reviewed places or "
                                                  "not an independent visitor stop; no claim about access.")
            rows.append({"query_id": base, "osm_type": kind, "osm_id": osm_id, "name": name,
                         "status": status, "reason": reason})
    write_json(ROOT / "docs/phase_c6_1_candidate_review.json", {
        "candidate_count": len(rows), "accepted_count": len(additions),
        "reviews": rows})

    pairs = nearby_pairs(items)
    window_pairs = [p for p in pairs
                    if any(k in SELECTED for k in p["catalog_keys"])]
    # The frozen-window pair review set must be exactly the C3 one: extending
    # the catalog may not silently disturb it, and any new close pair involving
    # a C6.1 addition needs an explicit review in this phase.
    frozen_pairs = {tuple(p["catalog_keys"]) for p in nearby_pairs(items[:173])}
    assert frozen_pairs == {tuple(p["catalog_keys"])
                            for p in manifests["phase_c3_catalog_manifest.json"]["nearby_pair_reviews"]}
    write_json(ROOT / "docs/phase_c6_1_nearby_pair_reviews.json", {
        "threshold_meters": 120,
        "catalog_pairs_within_threshold": window_pairs,
        "reviews": [],
        "decision": ("No catalog pair involving a C6.1 addition falls within the 120 m review "
                     "threshold; the frozen-window pair review set is unchanged.")})
    print(json.dumps({"added": len(additions), "total": len(items),
                      "new_district_counts": dict(Counter(i["district"] for i in additions)),
                      "new_window_close_pairs": window_pairs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
