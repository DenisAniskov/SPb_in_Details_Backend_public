# Phase B: source licences and publication review

Reviewed 2026-10-10. This is the project's implementation decision based on the linked primary texts, not a legal guarantee for every deployment or jurisdiction.

## OpenStreetMap database

© OpenStreetMap contributors. OSM data is available under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/); see [OSM copyright and source acknowledgements](https://www.openstreetmap.org/copyright). OSM object URLs identify geographic provenance; they are not evidence that an entrance, address, opening schedule or public access has been independently confirmed.

Conservative classification: the selected and adapted OSM factual catalog is treated as a **Derivative Database**. A small record count does not by itself settle substantiality; systematic extraction and qualitative importance matter. A **Collective Database** requires genuinely independent databases, rather than simply adding editorial fields to OSM rows. A map display can be a **Produced Work**; this does not erase obligations for the underlying derivative database. ODbL §§1, 4.2–4.6 require preserved notices, compatible share-alike terms and an offer of the machine-readable derivative database or complete alterations when publicly used. Internet access to that offer must be free. [Licence text](https://opendatacommons.org/licenses/odbl/1-0/)

Implementation: the **complete** `app/db/seed_places.json` database, including additions, carries an [adjacent ODbL notice](../app/db/README.md). OSM source selections and their derived factual audit exports carry the same attribution/ODbL notice. Preserve raw identity, acquisition date and coordinate semantics. Do not import Yandex responses into these files. These files establish the licensing/provenance package; their presence in a private repository does not fulfil a public source offer.

Editorial layer: `short_description`, `full_description`, `why_visit`, `visit_note`, topic tags and `recommended_visit_minutes` are original SmartNavigator writing and judgments. Separate fields make origin auditable, without exempting the combined database. ODbL §2.4 does not automatically license copyright in individual contents; the adjacent notice explicitly permits copying/adapting/redistributing project editorial contents with the catalog and derivative catalogs under ODbL terms. Third-party sites verify facts; prose/photos are not copied. Independent editorial exports need a separate content-licence review.

## Application attribution

The [OSMF attribution guidelines](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines) call for readable credit near the display and access to origin/licence details; databases also need notices with their data/documentation. The existing map-adjacent text already credited OSM and Yandex but had no OSM licence link. Phase B makes that text open a small source dialog with clickable OSM copyright, ODbL and Yandex terms links. The existing menu reaches the same dialog. Credit says **places** are OSM-derived and **maps/routes** are Yandex, avoiding confusion between sources.

Publication verification found that the GitHub repository is **private** and its unauthenticated raw-catalog URL returns HTTP 404 even after a successful branch push. The inaccessible catalog link has therefore been removed from the application. A private branch push does **not** fulfil the public ODbL source offer. No repository visibility or hosting changes are part of this phase. When distributing a copied catalog, include the adjacent database notice and provenance artifacts; do not distribute a bare JSON file without licence information.

## Yandex boundary

The current [Yandex Maps API terms](https://yandex.ru/legal/maps_api/ru/) §§4.1.3 and 6.2 require keeping native map logos/copyright/links unobstructed, a mobile-app hyperlink to [Yandex Maps terms](https://yandex.ru/legal/maps_termsofuse/), and use of MapKit routing results within MapKit applications/maps. The source dialog adds the missing hyperlink; SDK controls, routing and map attribution are unchanged. The general terms also mention an Open in Maps button: verify the pinned SDK and final UI meet the applicable agreement rather than assuming the dialog replaces it.

OSM licensing does not license Yandex tiles or route responses. Keep those out of the ODbL source offer. HTTP Geocoder-to-MapKit rights remain unresolved: §6.6 specifies JavaScript API use under these public terms; an active key is insufficient evidence of an exception. Confirm the actual account agreement or disable that feature before public deployment. SDK quota/paid-plan applicability also needs the account agreement; these are existing publication concerns, not reasons to relabel catalog data.

## Publication work still required

1. **Before any public release/use**, publish a versioned, freely accessible machine-readable complete catalog/source export with its licence notice and provenance, through an explicitly authorised publication channel. Add its verified stable download URL to the application so recipients can find the offer. The private GitHub branch is insufficient; pushing it successfully does not complete this requirement.
2. Review actual Yandex account terms, HTTP Geocoder permission and the Open in Maps requirement. Verify native logos/links are unobscured on a real Android device in portrait, landscape and navigation states.
3. Release signing, MapKit key restrictions and genuine native SDK authorisation remain separate release checks. Compilation with no credentials cannot verify them.
4. Recheck primary terms at release time. Keep copied OSM facts and original editorial judgments traceable; independently verify entrances/access where still unresolved.
