# Phase C5 — Scenic Scoring Model

> **Revision note (Phase C4/C5 runtime fixes, branch
> `codex/phase-c4-c5-runtime-fixes`):** `penaltyPerExtraMinute` was revised
> 0.8 → 0.2. With 0.8 any detour from ≈2 minutes was mathematically
> unacceptable at any coverage. Every hard limit below is unchanged.
> Review and calibration table:
> `PHASE_C4_C5_SCENIC_SCORING_REVIEW.md`; tests:
> `test/scenic_scoring_calibration_test.dart`. The parameter tables in this
> document keep the Phase C5 values as the historical record.
>
> **Second revision note (junction final fix):** the score is measured twice
> per candidate — once on the evaluated plan and again after junction
> confirmation, on the chain that will actually be walked (a confirmed
> connector adds its own native time, so the extra-time penalty is applied to
> the real totals). The post-confirmation re-check runs the same gate battery:
> coverage/evidence, detour share, extra-time cap, preset budget, score
> threshold (`_recheckAfterJunctions`, rejection detail `junctionRecheck`).

The scoring model is an **explainable, deterministic heuristic**. It is not
an objective measure of beauty, and it is never presented as one. All
weights and thresholds live in `ScenicScoringConfig`
(`lib/services/scenic_walk_engine.dart`), are constructor-injectable, and
are covered by unit tests.

## 1. Per-candidate score

```
score = benefitWeight · typeWeight · confidence · coverageShare
      − penaltyPerExtraMinute · max(0, extraMinutes)
      − uncertaintyPenaltyWeight · (1 − confidence)
```

Defaults: `benefitWeight = 2.0`, `penaltyPerExtraMinute = 0.8`,
`uncertaintyPenaltyWeight = 1.0`.

| Component | Meaning |
| --- | --- |
| `typeWeight` | embankment 0.95, pedestrian street 0.85, square 0.75 — verified-feature category attractiveness prior |
| `confidence` | dataset source confidence (0.9 for street-level public ways, 0.8 for squares) |
| `coverageShare` | fraction of the COMBINED stage length within 60 m of the verified feature line/point — geometry-verified engagement, not intent |
| `extraMinutes` | native MapKit walking time of the scenic stage minus the normal stage (seconds→minutes) |
| `(1 − confidence)` | source-uncertainty penalty |

**Principle (§5 of the brief):** a small useful detour can improve the walk;
a big detour for a dubious object cannot — the linear extra-minute penalty
and the hard caps below enforce exactly that.

## 2. Hard gates (any failure rejects the candidate)

| Gate | Default | Rationale |
| --- | --- | --- |
| Coverage evidence | ≥ 0.15 of stage length inside the 60 m band, else `ambiguous` → reject | a waypoint in the request is NOT a scenic claim; §4.3 |
| Detour share | scenic distance ≤ 1.6 × normal stage distance | pointless loops |
| Extra walking | ≤ 15 min per stage | walk-time honesty |
| Walk budget | total native walking (all stages, with the substitution) + visit time ≤ preset budget | §6; the walk total is native MapKit seconds, never geometric estimates; the engine accumulates the extras of ALREADY ACCEPTED insertions, so the n-th waypoint is checked against the whole walk built so far, not the original normal total (post-RC review fix, regression-tested) |
| Evidence level | `verifiedOnPath` ≥ 0.5 coverage; `verifiedNearby` ≥ 0.15; the explanation wording differs accordingly («Проходит по …» vs «Проходит рядом с …») | honest wording |

## 3. Acceptance and selection

- A candidate is accepted when its score ≥ `acceptanceThreshold` (0.25)
  after passing all gates.
- Within one insertion step the best-scoring candidate wins; ties break
  deterministically (fewer… by construction the score is a total order —
  exact ties are broken by candidate order: cross-track distance, then
  feature id), so identical inputs always produce identical output
  (unit-tested).
- Up to 2 waypoints per stage, 3 candidates evaluated per insertion step,
  2 scenic requests (A→W, W→B) consumed from the SHARED 12-request
  per-build budget before each attempt (reserved even on failure — the
  same conservative policy as the normal walk build).

## 4. Stage-level evidence and wording (post-RC review fix)

`ScenicStageInfo.evidence` and the user-facing explanation are derived from
MEASURED geometry, never from the feature type weight:

- each engaged feature gets its own coverage share over the FULL combined
  stage geometry (all segments, not only the scenic ones);
- the stage `evidence` is the strongest band any engaged feature reaches
  (≥ 0.5 → `verifiedOnPath`, ≥ 0.15 → `verifiedNearby`, else `ambiguous`);
- «Проходит по …» lists only features whose measured share reaches the
  on-path band; «Проходит рядом с …» lists the nearby band; features below
  the nearby band stay in the stage metadata but are never claimed as a
  benefit.

The type weight (embankment 0.95, pedestrian street 0.85, square 0.75)
influences only the score and the shortlist priority — never the evidence
or the wording. Both directions are pinned by regression tests (a square
that follows the way reads «Проходит по»; a barely engaged embankment
stays «Проходит рядом с»).

## 5. What the score is not

- Not a POI count (tourist stops are chosen before scenic routing starts
  and are never affected by the score).
- Not a beauty measurement — category weights are editorial priors about
  public-space types.
- Not comparable across different datasets/versions (weights may be
  recalibrated with the dataset; the version travels with the dataset).
