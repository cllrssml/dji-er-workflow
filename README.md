# DJI → EarthRanger

An [Ecoscope](https://ecoscope.io) workflow that turns a DJI drone mission into
EarthRanger records — from a folder of flight logs, a folder of photos, or both
at once:

- **Flight logs** (`.txt`) become GPS **tracks** and **Flight Folio events**, and
  optionally one **patrol** per flight (takeoff → landing, drone as leader).
- **Photos** (`.jpg`) are **geolocated by terrain raycast** and posted as
  **imagery events at the ground point the camera was aimed at** — not the drone's
  position — with the photo attached.

When you supply both in one run, each photo is automatically **linked to the
patrol of the flight it was taken on**, so tracks, Flight Folio events and photos
all appear together in EarthRanger with no manual cross-referencing.

> This is the batch, after-the-flight ingestion workflow. It combines and
> replaces the earlier `uas-flight-logs` and `uas-imagery` workflows.

---

## What makes the imagery geolocation work

Each DJI photo embeds the camera's GPS position, altitude and gimbal
orientation. The workflow casts a ray from the camera through the centre of the
frame and intersects it with a Digital Elevation Model (the "monoplotting"
technique) to find the ground point the camera was looking at.

**You do not need to download a DEM.** By default the workflow fetches Copernicus
GLO-30 (30 m) elevation data for the exact footprint of your photos, straight
from a public mirror — no account, no API key. A **DEM File** field lets you
supply your own GeoTIFF instead (e.g. for offline field use).

Accuracy is dominated by the drone's compass/gimbal yaw and barometric altitude —
expect roughly 5–15 m at a few hundred metres slant range. Photos taken within
~1° of horizontal are rejected (grazing rays are unreliable). Stills only;
video/SRT is not supported (video files in a photos folder are ignored).

---

## Installation (EarthRanger Desktop)

1. Open EarthRanger Desktop → **Workflow Templates → + Add Template**.
2. Paste this repository's GitHub URL and add it.
3. Add your EarthRanger instance under **Data Sources → Add → EarthRanger** (once).

The workflow installs directly from GitHub — the DJI log decoder and the
geolocation code travel with it.

---

## EarthRanger setup (once)

Create these in ER Admin before your first non-preview run. All are optional
depending on which halves you use.

| What | Where | Notes |
|---|---|---|
| **Flight Folio event type** | Admin → Event Types | Slug e.g. `uas_flight_folio`. Template in `schemas/`. |
| **Imagery event type** | Admin → Event Types | Slug e.g. `uas_imagery`. Template in `schemas/`. |
| **Aircraft subject type / subtype** | Admin → Subject Types | e.g. `aircraft` / `drone_quadcopter`. |
| **Source type** | Admin → Source Types | e.g. `tracking-device`. |
| **Patrol type** *(optional)* | Admin → Activity → Patrol types | e.g. `drone_patrol`. Patrol types **cannot** be created via the API. |

**Creating an event type from a template:** in ER Admin → Event Types, add a new
type, paste the matching JSON from `schemas/` into the V1 schema editor, save,
then click the **V1 → V2 conversion** button. Set the slug to match what you
enter in the form.

A free **DJI developer API key** is required to decrypt flight logs (not needed
for photos-only runs): developer.dji.com → Create App → select **Open API** →
activate via email → copy the ApiKey.

---

## Configuration (the form)

| Field | Purpose |
|---|---|
| **Flight Logs Folder** | Folder of DJI `.txt` logs. **Blank = skip flight logs.** |
| **Photos Folder** | Folder of drone `.jpg` photos. **Blank = skip imagery.** |
| **DJI API Key** | Decrypts flight logs. Only needed with a Flight Logs Folder. |
| **Flight Folio Event Type** | Slug for flight events. **Blank = tracking-only** (post tracks, no events). |
| **Imagery Event Type** | Slug for imagery events. **Blank = preview** (geolocate + map only, nothing posted). |
| **DEM File** | Optional local DEM GeoTIFF. **Blank = auto-fetch** (recommended). |
| **Patrol Type** | Optional. Flights create patrols of this type; photos link to them. Blank = no patrols. |
| **Aircraft Identity** | Registration + ER subject/source type slugs. Only used with flight logs. |
| **Track Decimation Rate** | GPS fixes/sec to post (default 1 Hz). |
| **Nature of Flight** | Applied to all Flight Folio events this run (blank for mixed batches). |
| **Camera Clock UTC Offset** | Fallback only for photos with no GPS time. |

### Common runs

- **Everything:** set both folders, both event types, a patrol type → tracks,
  Flight Folio events, patrols, and geolocated photos linked to their flights.
- **Photos only, preview first (recommended):** set the Photos Folder, leave the
  Imagery Event Type **blank** → photos are geolocated and shown on the map with
  nothing posted, so you can check geolocation quality. Needs no ER credentials.
- **Flight logs only:** set the Flight Logs Folder + DJI API Key, leave the Photos
  Folder blank.

Re-running is safe: tracks, events and patrols each have their own idempotency
check, so already-ingested data is skipped rather than duplicated.

---

## The dashboard

One map shows flight tracks (per-flight colours), the drone position (blue) and
the camera's ground target (red) for each photo, joined by a line of sight
(white). Stat cards summarise both halves (flights ingested/skipped/failed, time
flown, distance, aircraft; photos posted/located/duplicate/rejected). Two tables
give the per-flight and per-photo status. One KML per flight is written alongside
the results.

---

## Troubleshooting

- **"No drone-dji XMP tags"** — the photo lost its metadata (passed through a
  messaging app or editor). Use originals copied straight from the SD card.
- **"Camera altitude is at/below terrain"** — usually a bad barometric altitude or
  a DEM that doesn't cover the area. With auto-fetch this is rare; with a manual
  DEM, widen its extent.
- **"Ray left DEM coverage"** — the camera was aimed further than the DEM reaches.
  Auto-fetch sizes coverage to a 10 km margin; a manual DEM needs a generous box.
- **Patrol shows "no match"** on a photo — no patrol covers the photo's timestamp.
  Ingest the matching flight log in the same run (or first), and set the same
  Patrol Type on both.
- **A flight log fails to decrypt** — check the DJI API key (Open API app,
  activated). One bad file never aborts the batch; it shows as `failed`.

---

## Licence & credits

BSD 3-Clause. The imagery geolocation follows the terrain-raycast technique
popularised by [OpenAthena](https://github.com/Theta-Limited/OpenAthena); this is
an independent implementation. Elevation data: Copernicus GLO-30 DSM (ESA),
via the AWS Open Data mirror. Flight-log decoding uses
[dji-log-parser](https://github.com/lvauvillier/dji-log-parser).
