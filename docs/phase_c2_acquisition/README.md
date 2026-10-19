# Phase C2 OSM source evidence

© OpenStreetMap contributors. Raw OSM responses and their factual derivatives
are supplied under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/), with
the accompanying [complete catalog notice](../../app/db/README.md).

`requests.json` records the bounded acquisition attempts. Only successful
responses are stored as data. `east_south_kumi.json` is discovery/coordinate
evidence with an OSM base timestamp of 2026-07-24T11:04:51Z, acquired on October 10.
`current_nodes.json`, `current_ways.json`, `current_relations.json` separately
verify selected parent objects/tags on October 10. The comparison file identifies
one changed parent version. These checks do not verify current member-node
geometry for ways/relations. `frunzensky_full.json` supplies complete current
district boundary-member geometry.

No failed response, guessed entrance, private GPS route, proprietary provider
geometry or credential appears in this evidence. One failed timeout entry was
restored from the command transcript after a concurrent ledger write; its exact
request timestamp is unknown and explicitly null. Successful requests were
serial; two failed diagnostic probes overlapped. Cached failed requests are not
automatically repeated.
