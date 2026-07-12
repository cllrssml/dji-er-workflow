# dji_er — workflow notes

DJI drone mission → EarthRanger. One workflow that ingests **flight logs**
(tracks + Flight Folio + patrols) and/or **geolocated photos** (imagery events at
the camera's ground target). Merges and replaces `uas_flight_logs` (v5.0.0) and
`uas_imagery` (v0.2.0).

Custom package: `dji-er-tasks` at `/home/sam/Ecoscope_Projects/dji-er-tasks/`
(module `dji_er_tasks`). Working dir: `/home/sam/Ecoscope_Projects/dji_er/`.
Built 2026-07-12.

---

## Naming (Sam's call, 2026-07-12)

Sam wants "DJI" in the name and to **reserve `dji-earthranger` / `dji-er` for the
future live DJI→ER integration** (which won't be an Ecoscope workflow). So this
batch Ecoscope workflow takes a qualified slug. **Recommended public repo slug:
`dji-er-workflow`.** Local spec `id: dji_er`, package `dji-er-tasks`. Confirm the
public slug with Sam before creating the GitHub repo.

---

## Architecture decision — ONE task, not two DAG branches

The approved plan described two skipif-gated DAG branches. During the build this
was implemented instead as a **single `ingest_mission` task** that runs the
flights loop then the photos loop in-process. Reasons:

- **Guaranteed ordering + in-run patrol linking:** flights create patrols, then
  each photo links to the segment whose time range contains it — matched first
  against the patrols created *this same run* (in-memory `run_patrols`), falling
  back to an ER `get_patrols` query for cross-run linking. A DAG-branch design
  could not guarantee flights ran before photos without an edge that would also
  over-skip photos-only runs.
- **Branch optionality is trivial Python** (`if input_folder:` / `if
  photos_folder:`) — no fragile `any_dependency_is_empty_string` gating on a
  multi-input task (which would mis-fire on a blank patrol_type / event slug).
- The only DAG-level optionality left is the four map layers (each `any_is_empty_df`)
  combined by `combine_mission_layers`, which is the Desktop-proven hex-tasks pattern.

This realises the plan's intent (one workflow, both inputs optional, shared
patrols) more reliably. Same-run photo→patrol linking is actually *stronger* here
than the branch design would have given.

## Task chain

`set_workflow_details` → `set_er_connection` →
`set_input_folder` (Flight Logs Folder, blank = skip logs) →
`set_photos_folder` (Photos Folder, blank = skip imagery) →
`set_dji_api_key` → `set_event_type_name` (Flight Folio, blank = tracking-only) →
`set_aircraft_identity` → `set_decimation_rate` → `set_operational_defaults` →
`set_imagery_event_type` (blank = preview) → `set_dem_path` (blank = auto-fetch) →
`set_time_defaults` → `set_patrol_type` (shared) →
**`ingest_mission`** (the one loop; returns `MissionResult`) →
6 accessors (`extract_track_gdf` / `extract_targets_gdf` / `extract_drone_gdf` /
`extract_sightline_gdf` / `extract_flight_results_df` / `extract_photo_results_df`) →
4 layers (`create_polyline_layer` track + sightline, `create_point_layer` drone +
target; each `skipif: any_is_empty_df`) → `combine_mission_layers`
(no skipif — handles SkipSentinel itself) → `draw_ecomap` → `persist_text` →
map/table widgets → `gather_dashboard` (`time_range: ~`).

## Dashboard layout (13 widgets, ids 0–12)

Row 1 (y0 h3, w2 each): Ingested, Skipped, Failed, Flown, Distance.
Row 2 (y3 h3, w2 each): Aircraft, Posted, Located, **Duplicate** (photo skipped),
**Rejected** (photo failed). Map (y6 h14 w10), Flight table (y20 h10 w10),
Photo table (y30 h10 w10). Single-word titles (Trap 15); "Duplicate"/"Rejected"
disambiguate the photo counts from the flight Skipped/Failed.

## DEM auto-fetch (the headline change vs uas_imagery)

`dji_er_tasks/demfetch.py`: `bbox_from_points(points, margin_m)` +
`fetch_dem(bbox, out_path)`. In `_run_photos`, PASS 1 extracts every photo's pose;
if the DEM File field is blank, the bbox of all photo GPS (10 km margin =
`_MAX_RANGE_M`) is mosaicked from **Copernicus GLO-30 on the keyless AWS mirror**
(`/vsicurl/`, ocean-tile-safe) into `results/dem/auto_dem.tif`, then PASS 2
raycasts. A supplied DEM File bypasses the fetch (offline use). **No GEE** — it
would add a mandatory Desktop service-account Data Source, per-request pixel
budgets, and online-only operation; the AWS mirror needs none of that. See the
memory note `project_dji_er` / plan for the full rationale.

Runtime needs curl-enabled GDAL for `/vsicurl/` — present in the rasterio wheel
used here (verified). Re-runs currently re-fetch into the per-run results dir
(a few MB, seconds); a persistent cache is a future optimisation.

## Package sync discipline (TWO copies)

Source of truth: `/home/sam/Ecoscope_Projects/dji-er-tasks/`. A bundled copy lives
inside the compiled dir. After any code change, re-copy the bundle (or re-run the
post-compile patch below). Files: `__init__.py` (all tasks), `geolocate.py`
(raycast, self-contained), `demfetch.py` (DEM), `_binary.py` + `bin/` (dji-log
decoders), `pyproject.toml` (deps + `wt_registry` entry point + `bin/*` artifacts).

## Build → compile → verify

```bash
cd /home/sam/Ecoscope_Projects/dji_er
export TMPDIR=/home/sam/wt-tmp
# First compile: --clobber only. Subsequent: add --update.
wt-compiler compile --spec=spec.yaml \
  --pkg-name-prefix=ecoscope-workflows \
  --results-env-var=ECOSCOPE_WORKFLOWS_RESULTS --clobber
# Post-compile patch (Trap 9) — every recompile:
D=ecoscope-workflows-dji-er-workflow
cp -r /home/sam/Ecoscope_Projects/dji-er-tasks "$D/dji-er-tasks"
sed -i 's|path = "/home/sam/Ecoscope_Projects/dji-er-tasks"|path = "./dji-er-tasks"|' "$D/pixi.toml"
cd "$D" && pixi install
# First compile leaves VERSION.yaml 0.0.0 — set it manually:
printf '{MAJ: 0, MIN: 1, PATCH: 0}\n' > VERSION.yaml
```

## Verification record (2026-07-12, autonomous build)

- **Compile:** clean on first try (validates all task names/signatures + full DAG).
- **pixi install:** ok, multi-platform lock present.
- **mock-io:** full DAG runs, exit 0.
- **Real photos-only preview (no --mock-io, zero ER creds):** 2 synthetic DJI
  photos → **Located = 2**, DEM **auto-fetched** to `results/dem/auto_dem.tif`,
  map + both tables + all 13 widgets rendered, empty flight-branch table renders
  fine, `error: null`. Confirms the DEM-auto-fetch UX and that
  `combine_mission_layers` correctly drops the skipped (empty) track layer.
- **DEM parity:** a raycast against the auto-fetched DEM landed **1.55 m** from the
  same raycast against the known-good `uas_imagery/dem/cfw_demo_dem.tif` — the
  auto-fetch yields an equivalent, usable DEM.
- **DJI XMP pose parse:** verified in the merged package.
- **NOT yet done (needs Sam / live ER):** a flight-log run (always writes to ER),
  a full both-halves live run on CFW's own instance (Trap 34 — never the sandbox),
  and a Desktop GitHub-URL install.

## Merged traps carried over (all preserved in code)

- **Gated ER auth** (Trap 18): client resolved only when a write/read will happen
  (`need_client = has_flights or photos_will_post`) — photos-only preview needs
  zero ER creds. Fixes uas_flight_logs' old unconditional resolution.
- **Subject-source lower bound = `datetime(2000,1,1)`** (Trap 19), never takeoff.
- **Named source provider** `dji_rc_pro` / "DJI" via `post_sourceproviders` first
  (Trap 23).
- **`get_events(..., include_details=True)`** for photo idempotency — the default
  omits `event_details` and silently defeats the filename match.
- **Patrol create+link superset:** flights create (leader = subject) + link new
  events inline / PATCH-merge existing (never replace `patrol_segments`); photos
  link only. Patrol types can't be created via API — validated at start.
- **GPS 3-step fallback** + **past/future garbage-year guard** + **GPS-frame
  haversine distance** (firmware unit variance) — all preserved from v5.0.0.
- **Windows subprocess `encoding="utf-8"`** for the dji-log decoder (cp1252 bug).

## Outstanding for Sam (deliberately left)

1. **Confirm the public repo slug** (recommended `dji-er-workflow`).
2. **Create the ER event types** from `schemas/` if not already present, and a
   `drone_patrol` patrol type (CFW already has one).
3. **Live run on CFW's own ER** (never sandbox — Trap 34): a small logs+photos
   batch → verify tracks + Flight Folio + patrol created and photos posted at
   target points, linked into the same patrol; re-run → all idempotency legs skip.
4. **Desktop install** from the new GitHub URL (Sam's standard validation path).
5. **Publish:** create the new repo, then **archive `uas-flight-logs` and
   `uas-imagery`** with READMEs pointing here (dNBR precedent). Update the
   top-level playbook's "Workflows built" table and Outside comms log; consider a
   community post noting the merge.
6. Old cleanup still pending from uas_imagery: 4 pre-fix duplicate imagery
   events on CFW ER (2 pairs) — see the `project_uas_imagery` memory for serials.
