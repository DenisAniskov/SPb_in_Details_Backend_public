"""Rebuild the reviewed C2 additions from cached OSM evidence, without network/DB.

The explicit selection below is an editorial review, not an automated accept-all.
Existing 77 JSON record contents are copied unchanged from the frozen C1 baseline.
"""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from scripts._phase_c2_candidate_review import get_sources_boundaries
from scripts.validate_phase_b_catalog import canonical_identity, catalog_digest, nearby_pairs, point_in_boundary

ROOT = Path(__file__).resolve().parents[1]
# kind, OSM id, stable key, category, minutes, original visitor-facing reason.
# No individual component of an accepted park/memorial is separately accepted.
REVIEWED = [
    ("relation",1721304,"spartak-garden","nature",20,"Погулять по небольшому зелёному участку в южной части Невского района и сделать спокойную паузу между городскими кварталами."),
    ("relation",2909244,"builders-park","nature",30,"Выбрать короткую прогулку в парке Строителей вместо дальнего переезда к центральным садам."),
    ("relation",3144817,"brest-park","nature",25,"Сменить городские улицы правого берега на прогулку по части Брестского парка."),
    ("relation",3189848,"krasnykh-zor-boulevard","nature",20,"Пройти часть зелёного бульвара Красных Зорь и сделать местную прогулку без большого паркового круга."),
    ("relation",4657403,"vinogradovsky-square","nature",15,"Добавить короткую зелёную остановку к прогулке в окрестностях Ивановской улицы."),
    ("relation",6071458,"fishermens-settlement-park","nature",30,"Познакомиться с зелёным пространством Рыбацкого и пройти часть парка Рыбачья Слобода."),
    ("relation",10142370,"ivanovsky-garden","nature",20,"Сделать спокойный местный проход по Ивановскому саду, когда на большой парк нет времени."),
    ("relation",10823364,"yesenin-park","nature",30,"Пройти часть парка Есенина в жилой части правобережного района; весь парк не обязателен для короткой прогулки."),
    ("relation",12248920,"demin-garden","nature",20,"Зайти в Дёминский сад как в отдельную зелёную остановку в северной части Невского района."),
    ("relation",15879427,"leningrad-antiaircraft-women-alley","nature",20,"Пройти часть аллеи Ленинградских Зенитчиц и обратить внимание на мемориальное название зелёного пространства."),
    ("way",336857867,"yablonovsky-garden","nature",25,"Выбрать прогулку по части Яблоновского сада в окрестностях проспекта Большевиков."),
    ("way",437165341,"ekaterina-demidova-square","nature",15,"Сделать короткую паузу в сквере Екатерины Демидовой во время прогулки по Невской заставе."),
    ("way",1316790846,"vargunin-square","nature",15,"Увидеть ещё одно небольшое зелёное пространство Невской заставы и отдохнуть от движения соседних улиц."),
    ("relation",14092800,"nikolai-lvov-square","nature",20,"Пройти часть сквера Николая Львова: небольшое зелёное пространство даёт повод для местной прогулки."),
    ("way",23649849,"lomonosovsky-garden","nature",20,"Включить Ломоносовский сад в короткую прогулку по окрестностям Ивановской улицы."),
    ("node",968065187,"krupskaya-nevskaya-zastava-memorial","history_architecture",10,"Остановиться у бюста Надежды Крупской и рассмотреть местный памятник Невской заставы."),
    ("node",2343436176,"volodarsky-memorial","history_architecture",10,"Рассмотреть памятник Володарскому как самостоятельную короткую остановку на прогулке по Невскому району."),
    ("node",1213312029,"rybatskoye-volunteers-obelisk","history_architecture",15,"Увидеть обелиск крестьян-добровольцев и прочитать посвящение, связывающее прогулку по Рыбацкому с местной памятью."),
    ("node",1911139960,"rybatskoye-firefighter-memorial","history_architecture",10,"Рассмотреть памятник пожарному: небольшая остановка посвящена профессии спасателя."),
    ("node",3736513615,"olga-berggolts-memorial","history_architecture",15,"Увидеть памятник Ольге Берггольц и сделать литературную остановку на прогулке по Невской заставе."),
    ("node",3067326952,"polar-convoys-sailors-memorial","history_architecture",15,"Рассмотреть памятник морякам полярных конвоев и уделить время его посвящению."),
    ("relation",9713316,"nevsky-cranes-memorial","history_architecture",25,"Осмотреть мемориал «Журавли» как единый ансамбль и сделать спокойную остановку памяти; отдельные части комплекса не считаются разными пунктами маршрута."),
    ("node",5257740630,"first-leningrad-panel-house","history_architecture",15,"Посмотреть снаружи на дом, отмеченный как первый панельный дом Ленинграда, и сравнить его с соседней жилой застройкой."),
    ("node",5324730821,"sausage-house","history_architecture",20,"Рассмотреть необычную протяжённую форму Дома Колбаса с общедоступной улицы; это архитектурная остановка без посещения квартир."),
    ("node",3514545041,"imperial-porcelain-factory-museum","art",70,"Выделить отдельное время на музей фарфора: это полноценное посещение экспозиции, а не короткая остановка у проходной."),
    ("relation",2461308,"dimitrov-square","nature",20,"Пройти часть сквера Димитрова и сделать зелёную паузу в местной прогулке по Купчину."),
    ("relation",6017384,"liberator-soldiers-square","nature",20,"Погулять по части сквера Воинов-Освободителей, оставив время на спокойную остановку у зелёного пространства."),
    ("relation",10005007,"firefighter-heroes-park","nature",35,"Пройти часть парка Героев-Пожарных; на большой обход всего парка потребуется больше времени."),
    ("relation",10045022,"internationalists-park","nature",35,"Выбрать прогулку по части парка Интернационалистов; мемориальные объекты внутри входят в эту остановку и не добавлены отдельными точками."),
    ("relation",10103986,"fedorovsky-square","nature",20,"Сделать местную зелёную остановку в Фёдоровском сквере и пройти небольшой участок его территории."),
    ("relation",11093020,"alpine-square","nature",20,"Пройти часть Альпийского сквера и сменить прогулку по улицам Купчина на небольшой зелёный участок."),
    ("relation",12320282,"isfahan-square","nature",20,"Увидеть Исфаханский сквер как самостоятельное зелёное пространство и повод для прогулки в западной части Купчина."),
    ("relation",12369635,"filatovsky-square","nature",20,"Включить часть Филатовского сквера в местную прогулку по южной части Фрунзенского района."),
    ("relation",12602447,"carpathian-square","nature",20,"Пройти часть Карпатского сквера и сделать спокойную паузу в южном Купчине."),
    ("relation",18752543,"friendship-park","nature",25,"Пройти часть парка Дружбы в окрестностях Международной; скульптуры внутри относятся к этой общей остановке."),
    ("way",23241233,"kupchino-apple-garden","nature",25,"Сделать прогулку по части Яблоневого сада в Купчине и выбрать зелёное пространство поблизости вместо поездки в центр."),
    ("way",23566651,"rescuers-square","nature",15,"Пройти небольшой участок сквера Спасателей и добавить зелёную паузу к прогулке по Фрунзенскому району."),
    ("way",52959591,"kupchinsky-square","nature",20,"Погулять по части Купчинского сквера: это отдельная местная остановка между городскими кварталами."),
    ("way",123318764,"budapest-square","nature",20,"Пройти часть Будапештского сквера и сделать короткую прогулку в южной части Купчина."),
    ("way",174092701,"fruit-garden","nature",25,"Выбрать прогулку по части Плодового сада в восточной части Купчина; название не означает сезонную доступность плодов."),
    ("node",262615345,"zhukov-kupchino-memorial","history_architecture",10,"Рассмотреть местный памятник Георгию Жукову и прочитать его посвящение."),
    ("node",1266486020,"frunzensky-militia-volunteers-memorial","history_architecture",15,"Остановиться у памятника добровольцам Фрунзенской дивизии народного ополчения и уделить время местной военной памяти."),
    ("node",1936080260,"brave-soldier-svejk-memorial","art",10,"Увидеть скульптуру бравого солдата Швейка возле Купчина: узнаваемый литературный герой даёт повод для короткой остановки."),
    ("way",23243126,"zanevsky-park","nature",30,"Пройти часть Заневского парка и добавить местную зелёную прогулку на Охте."),
    ("way",46041696,"yuri-knorozov-square","nature",20,"Пройти часть сквера Юрия Кнорозова и обратить внимание на мемориальное имя зелёного пространства."),
    ("relation",12903102,"okhta-okkervil-park","nature",25,"Выбрать прогулку по части парка «Охта-Оккервиль» вместо длинного перехода к паркам исторического центра."),
    ("way",23239466,"georgievsky-okhta-square","nature",20,"Сделать зелёную остановку в Георгиевском сквере во время местной прогулки по Большой Охте."),
    ("node",2850643829,"carl-faberge-okhta-memorial","history_architecture",10,"Рассмотреть бюст Карла Фаберже: это самостоятельная небольшая остановка о петербургском ювелире."),
    ("node",3658412519,"sail-okhta-sculpture","art",10,"Рассмотреть каменную скульптуру «Парус» на Охте и добавить короткую художественную остановку к прогулке."),
]


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    sources, boundaries = get_sources_boundaries()
    old = json.loads((ROOT / "docs/phase_c1_catalog_baseline.json").read_text(encoding="utf-8"))
    additions = []
    accepted_sources = []
    for kind, id, key, category, minutes, reason in REVIEWED:
        source = sources[(kind, id)]
        tags = source["tags"]
        p = source if kind == "node" else source["center"]
        districts = [b for b in boundaries["boundaries"] if point_in_boundary(p["lat"], p["lon"], b)]
        assert len(districts) == 1, (key, districts)
        district = districts[0]["district"]
        title = tags.get("name:ru", tags["name"]).replace("\u00a0", " ")
        is_park = tags.get("leisure") in {"park", "garden"}
        is_museum = tags.get("tourism") == "museum"
        is_exterior = key in {"first-leningrad-panel-house", "sausage-house"}
        mode = "park_walk" if is_park else "indoor_museum" if is_museum else "exterior" if is_exterior else "outdoor_memorial" if category == "history_architecture" else "outdoor_monument"
        short = (f"{title} — зелёная остановка для местной прогулки ({district})." if is_park
                 else f"{title} — {'музейная' if is_museum else 'архитектурная' if is_exterior else 'памятная' if category == 'history_architecture' else 'художественная'} остановка ({district}).")
        caution = "Доступ и пешеходный подход независимо не проверены. "
        if is_park:
            note = caution + f"{minutes} минут — прогулка по части территории, без обязательного обхода всего парка или сквера. Точка показывает объект, а не проверенный вход."
        elif is_museum:
            note = caution + "Заложены 70 минут на экспозицию. До выхода уточните билет, расписание и доступ посетителей музея при заводе; проход через производственную территорию не предполагается."
        elif is_exterior:
            note = caution + "Заложен внешний осмотр с доступного общественного пространства. Вход в жилые помещения, двор или закрытую территорию не предполагается."
        else:
            note = caution + f"{minutes} минут — осмотр памятника или скульптуры и короткая остановка; точка не является подтверждённым местом подхода."
        street, number = tags.get("addr:street"), tags.get("addr:housenumber")
        address = ", ".join(str(x) for x in (street, number) if x) if street or number else f"Санкт-Петербург, {district}; местоположение по картографической точке"
        url = f"https://www.openstreetmap.org/{kind}/{id}"
        topic = "парк, местная прогулка, зелёное пространство" if is_park else "фарфор, музей, декоративное искусство" if is_museum else "архитектура, внешний осмотр, городская среда" if is_exterior else "памятник, память, местная прогулка" if category == "history_architecture" else "скульптура, искусство, местная прогулка"
        additions.append({"catalog_key": key, "title": title, "short_description": short,
                          "full_description": reason + " Остановка рассчитана на указанный вид осмотра. Перед выходом учитывайте, что физический доступ и точный пешеходный подход ещё требуют проверки.",
                          "category": category, "is_unusual": category == "art" or is_exterior,
                          "latitude": p["lat"], "longitude": p["lon"], "address": address,
                          "photo_url": None, "working_hours": None, "tags": topic, "rating": None,
                          "architectural_style": None, "year_built": None, "source_url": url,
                          "coordinate_kind": "osm_node" if kind == "node" else "osm_bounds_center",
                          "source_checked_on": "2026-10-10", "address_status": "osm_tags" if any(k.startswith("addr:") for k in tags) else "location_description_not_verified",
                          "recommended_visit_minutes": minutes, "why_visit": reason, "visit_note": note,
                          "official_website": tags.get("website", tags.get("contact:website")), "wikipedia_url": None,
                          "coordinate_source_url": url, "coordinate_uncertainty": "Картографический узел объекта из снимка на 24 июля 2026 года; текущий узел проверен 10 октября. Точность и публичный подход не проверены независимо." if kind == "node" else "Центр ограничивающего прямоугольника из снимка OSM на 24 июля 2026 года; актуальность геометрии не проверена. Может находиться внутри территории и не обозначает вход.",
                          "coordinate_snapshot_timestamp": "2026-07-24T11:04:51Z", "current_object_checked_on": "2026-10-10",
                          "access_point_status": "representative_point_not_verified", "district": district,
                          "visit_mode": mode, "verification_evidence": f"Координаты и исходные теги {kind}/{id} сохранены из east_south_kumi (срез 24 июля); текущие теги объекта отдельно проверены 10 октября через официальный OSM API. Район проверен по полигону OSM relation/{districts[0]['osm_relation_id']}. Доступ и вход не подтверждены."})
        accepted_sources.append(source)
    items = old + additions
    assert len(old) == 77 and len(additions) == 49
    write_json(ROOT / "app/db/seed_places.json", items)
    write_json(ROOT / "docs/phase_c2_osm_source_snapshot.json", {
        "checked_on": "2026-10-10", "source": "OpenStreetMap via cached bounded Overpass GET",
        "license": "ODbL 1.0", "attribution": "© OpenStreetMap contributors",
        "acquisition_evidence": "phase_c2_acquisition/east_south_kumi.json",
        "coordinate_snapshot_timestamp": "2026-07-24T11:04:51Z",
        "current_object_evidence": ["phase_c2_acquisition/current_nodes.json", "phase_c2_acquisition/current_ways.json", "phase_c2_acquisition/current_relations.json"],
        "scope": "Exactly the 49 additions; geometry remains from July 24 mirror. Current parent-object checks do not prove way/relation member-node geometry current. Union with unchanged osm_source_snapshot.json for the complete catalog.",
        "elements": accepted_sources})
    extra = dict(boundaries)
    extra.update(boundary_count=1, boundaries=[boundaries["boundaries"][-1]],
                 purpose="Additive Frunzensky district evidence; union with unchanged Phase B 12 boundaries.")
    write_json(ROOT / "docs/phase_c2_boundary_evidence.json", extra)
    assignments = json.loads((ROOT / "docs/phase_b_district_assignments.json").read_text(encoding="utf-8"))
    for item in additions:
        b = next(b for b in boundaries["boundaries"] if b["district"] == item["district"])
        assignments[item["catalog_key"]] = {k:item[k] for k in ("catalog_key", "title", "latitude", "longitude", "district")}
        assignments[item["catalog_key"]].update(osm_boundary_url=b["osm_boundary_url"], assignment_method="osm_boundary_point_in_polygon", source_checked_on="2026-10-10")
    write_json(ROOT / "docs/phase_c2_district_assignments.json", assignments)
    selected = {(e["type"], e["id"]) for e in accepted_sources}
    old_sources = {canonical_identity(i["source_url"]) for i in old}
    ledger = []
    for identity, source in sorted(sources.items()):
        tags = source.get("tags", {})
        if identity == ("way", 29390114):
            status, reason = "excluded", "Official Nevskaia Zastava museum page reports guarded grounds and future adaptation; no verified public exterior routing point. Bounds center inside guarded site is unsuitable."
        elif identity in selected:
            status, reason = "accepted", "Explicit editorial selection; representative coordinate and access uncertainty retained."
        elif identity in old_sources:
            status, reason = "excluded", "Already represented by original 77 canonical record."
        elif tags.get("access") in {"private", "no", "permit", "military"} or tags.get("foot") in {"no", "private"}:
            status, reason = "excluded", "Explicit restricted-access tag."
        elif tags.get("building") in {"collapsed", "ruins"} or tags.get("memorial") == "grave":
            status, reason = "excluded", "Ruined/collapsed building or individual grave; no verified suitable visitor activity."
        else:
            status, reason = "not_accepted", "Not selected: visitor usefulness, public access, district containment or independence from another site not sufficiently established in this phase. No claim that this object is inaccessible."
        review = {"osm_type":identity[0], "osm_id":identity[1], "name":tags.get("name:ru",tags.get("name")), "status":status, "reason":reason}
        if identity == ("way",29390114):
            review.update(supplementary_source_url="https://nevzastava.ru/dachachernova", source_checked_on="2026-10-10")
        ledger.append(review)
    write_json(ROOT / "docs/phase_c2_candidate_review.json", {"candidate_count":len(ledger), "accepted_count":len(additions), "reviews":ledger})
    # All final close pairs need explicit human review; the manifest builder
    # does not invent those reviews. Preserve a separate reviewed document.
    write_json(ROOT / "docs/phase_c2_nearby_pairs.json", nearby_pairs(items))
    print(json.dumps({"added":len(additions), "total":len(items), "new_districts":dict(Counter(i['district'] for i in additions)), "close_pairs":nearby_pairs(items)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
