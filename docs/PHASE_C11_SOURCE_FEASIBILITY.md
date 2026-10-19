# Phase C11 — source feasibility: quiet walking in Saint Petersburg

Date: 2026-10-17. Scope: a read-only, relative road-noise exposure proxy for an
existing MapKit pedestrian walk. This document decides which sources may be
used and which claims are forbidden.

## 1. What was checked

| Candidate | Availability | Licence / permission | Decision |
| --- | --- | --- | --- |
| Official Saint Petersburg noise maps / environmental GIS | Historical city-commissioned noise maps and an ecological GIS exist, but no open machine-readable road-noise layer or API with a documented reuse licence was found. Data.gov.spb.ru does not provide an anonymous open noise dataset (the API answered 401 in the checked state). | Not established for derivative use | **Not used.** A map without a clear licence cannot be shipped. |
| Point acoustic measurements from papers | Sparse measurement campaigns, not a city-wide network; no continuous, reusable dataset. | Paper-specific; not an open dataset | **Not used as a dataset.** Cited only as context. |
| OpenStreetMap road network | Real, openly licensed, versioned OSM extract for SPb. Functional classes and some traffic tags (`lanes`, `maxspeed`, `access`, `motor_vehicle`, `tunnel`) are present. | ODbL-1.0; attribution required | **Selected as the only spatial source.** |
| OpenStreetMap foot network | Real pedestrian geometry is available, but the backend does not route or replace MapKit geometry. The app supplies the real MapKit route instead. | ODbL-1.0 | Used for reproducible evaluation inputs only. |
| Random / unattributed noise maps | Origin, date or licence unclear | Unknown | **Rejected.** No use. |
| Modelled hourly traffic volumes | No verified SPb dataset with a reusable licence and known calibration | Unknown | **Rejected.** The model is explicitly static. |
| Client-supplied coefficients or datasets | Untrusted user input | — | **Rejected.** Only the packaged, hash-pinned dataset is accepted. |

The search found descriptions of the 2006 city noise map and later GIS and
research work, but not an open data product that could be downloaded, versioned
and redistributed under a known licence. The absence of such a dataset is a
finding, not permission to invent dB values.

## 2. Scientific starting point

Novack, Wang and Zipf (2018), *A System for Generating Customized Pleasant
Pedestrian Routes Based on OpenStreetMap Data*, Sensors 18(11):3794,
<https://doi.org/10.3390/s18113794>, shows that OSM traffic indicators can
support a relative "quietness" factor in pedestrian routing. Wang et al.
(2020), *Quiet Route Planning for Pedestrians in Traffic Noise Polluted
Environments*, IEEE TITS, extends this with computed traffic-noise levels.

Neither result is a validated acoustic model for Saint Petersburg. C11
therefore uses the weaker, defensible claim: a **relative proximity proxy**,
not a sound-level prediction.

## 3. Selected geographic pilot

The pilot is a central rectangle, deliberately bounded and versioned:

```
west=30.28, south=59.915, east=30.38, north=59.965
```

It contains Palace Square, the Hermitage, Nevsky Prospekt, the Summer Garden,
the Fontanka embankments, St Isaac's and the central pedestrian streets. It
does **not** claim city-wide coverage. Routes outside it are returned as
`unknown` / `outside_coverage`, never as quiet.

Source extract: Geofabrik GmbH, *Northwestern Federal District* OSM extract,
dated 2026-10-16, SHA-256
`3051f24dd1dc0ab6efc63f0a015781ef8380d1523c02a0f09c1e6c5ed52d7fb7`.
The extract is filtered to core motor-road classes; footways, paths, cycleways
and service aisles are excluded from the noise layer. The resulting snapshot
contains **3,638** source ways (3,595 usable after motor-access/tunnel rules).

## 4. Why this is feasible and honest

* Every source way is real OSM geometry with an OSM id, timestamp and tags.
* The dataset is versioned, hash-pinned and shipped inside the image; the
  runtime never downloads OSM and never invents roads.
* The result is explicitly dimensionless and relative. It does not claim dB,
  acoustic measurements, sanitary compliance, health risk or guaranteed
  silence.
* Missing data is `unknown`, not zero. A route outside the pilot has no index.
* A static road-network proxy cannot model hourly traffic. The response says
  this and uses time only to align the report.

## 5. Blocked claims

The following are **NOT_VERIFIED / not claimed** in C11:

* acoustic noise levels or dB(A);
* field-calibrated quietness for any street;
* hourly or weekday traffic volume;
* health, sleep or sanitary effects;
* city-wide coverage outside the pilot;
* equivalence between OSRM evaluation routes and live MapKit alternatives.

`FIELD_VALIDATED` remains false until real measurements are made with a
documented protocol and instrument.
