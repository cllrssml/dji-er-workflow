"""
dji_er_tasks — DJI drone flights + imagery into EarthRanger via Ecoscope Platform.

One workflow, two inputs, either optional:

* Flight Logs Folder — DJI .txt logs become GPS tracks + Flight Folio events,
  and (optionally) one patrol per flight (takeoff to landing, drone as leader).
* Photos Folder — drone JPEGs are geolocated by terrain raycast (camera pose +
  DEM) and posted as imagery events at the ground point the camera was aimed at,
  photo attached.

When both are supplied in the same run, the flights are ingested first so their
patrols exist, then each photo is linked to the patrol segment whose time range
contains the moment it was taken — so photos appear under the flight they were
shot on, with no manual cross-referencing.

The DEM for the raycast is fetched automatically from the keyless Copernicus
GLO-30 AWS mirror, sized to the photos' own GPS footprint (see demfetch.py). A
DEM File field is available to override this with a local GeoTIFF for offline use.

Tasks prefixed set_ expose typed parameters to the Desktop config form.
ingest_mission is the single processing task; accessor and stat tasks downstream
pull individual components from its MissionResult bundle.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import atan2, ceil, cos, floor, radians, sin, sqrt
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import geopandas as gpd
import pandas as pd
from pydantic import Field
from pydantic.json_schema import WithJsonSchema
from shapely.geometry import LineString, Point
from wt_registry import register

# ---------------------------------------------------------------------------
# Per-flight track colour palette (RGBA uint8) — cycles if >10 flights
# ---------------------------------------------------------------------------

_TRACK_PALETTE = [
    [228,  26,  28, 255],  # red
    [ 55, 126, 184, 255],  # blue
    [ 77, 175,  74, 255],  # green
    [152,  78, 163, 255],  # purple
    [255, 127,   0, 255],  # orange
    [ 23, 190, 207, 255],  # cyan
    [247, 129, 191, 255],  # pink
    [188, 189,  34, 255],  # yellow-green
    [140,  86,  75, 255],  # brown
    [ 31, 119, 180, 255],  # dark blue
]

_GDF = Annotated[Any, WithJsonSchema({"type": "ecoscope.platform.annotations.DataFrame"})]

# One row per .txt flight record processed.
_FLIGHT_RESULTS_COLUMNS = [
    "file",
    "aircraft_serial",
    "takeoff_utc",
    "flight_time_min",
    "battery_pct_takeoff",
    "battery_pct_landing",
    "battery_serial",
    "max_alt_agl_m",
    "max_speed_ms",
    "max_dist_m",
    "total_distance_m",
    "firmware",
    "status",
    "patrol",
    "error",
]

# One row per photo processed.
_PHOTO_RESULTS_COLUMNS = [
    "file",
    "time_utc",
    "status",
    "target_lat",
    "target_lon",
    "slant_range_m",
    "camera_alt_m",
    "confidence",
    "patrol",
    "error",
]

# A geolocation is flagged "low" when the camera was within this many degrees of
# level, or the target lies beyond this slant range — shallow, grazing shots put
# the target far away and a small compass/pitch error swings it a long way.
_LOW_CONF_PITCH_DEG = 5.0
_LOW_CONF_SLANT_M = 1200.0

_DJI_PROVIDER_KEY = "dji_rc_pro"

# Max raycast range; also the DEM auto-fetch margin (a photo aimed this far needs
# terrain coverage that far out).
_MAX_RANGE_M = 10_000.0


# ---------------------------------------------------------------------------
# Result bundle — returned by ingest_mission, consumed by accessor/stat tasks
# ---------------------------------------------------------------------------


@dataclass
class MissionResult:
    """Holds all outputs of the mission ingest. Not serialised by the SDK."""

    track_gdf: gpd.GeoDataFrame       # LineString per flight
    targets_gdf: gpd.GeoDataFrame     # ground target Point per located photo
    drone_gdf: gpd.GeoDataFrame       # drone Point per located photo
    sightline_gdf: gpd.GeoDataFrame   # drone->target line per located photo
    flight_results_df: pd.DataFrame   # one row per .txt file
    photo_results_df: pd.DataFrame    # one row per photo
    # flight stats
    n_ingested: int = 0
    n_skipped: int = 0
    n_failed: int = 0
    total_flight_seconds: float = 0.0
    total_distance_m: float = 0.0
    n_aircraft: int = 0
    # photo stats
    n_posted: int = 0
    n_located: int = 0
    n_photo_skipped: int = 0
    n_photo_failed: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_results_dir(root_path: str) -> Path:
    """Convert a file:// URL (or plain path string) to an absolute local Path."""
    if root_path.startswith("file://"):
        url_path = urlparse(root_path).path
        if url_path.startswith("/") and len(url_path) > 2 and url_path[2] == ":":
            url_path = url_path[1:]  # Windows: /C:/Users/... -> C:/Users/...
        return Path(url_path)
    return Path(root_path)


def _empty_track_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "aircraft_serial": pd.Series(dtype=str),
            "takeoff_utc": pd.Series(dtype=str),
            "flight_time_min": pd.Series(dtype=float),
            "track_color": pd.Series(dtype=object),
        },
        geometry=gpd.GeoSeries([], crs="EPSG:4326"),
    )


# Tooltip column labels are plain English on purpose — these GeoDataFrames feed
# the map's picking tooltip directly, and lonboard renders column names verbatim.
def _empty_targets_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "Photo": pd.Series(dtype=str),
            "Time (UTC)": pd.Series(dtype=str),
            "Distance from drone (m)": pd.Series(dtype=float),
            "Ground elevation (m)": pd.Series(dtype=float),
            "Confidence": pd.Series(dtype=str),
        },
        geometry=gpd.GeoSeries([], crs="EPSG:4326"),
    )


def _empty_drone_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "Photo": pd.Series(dtype=str),
            "Time (UTC)": pd.Series(dtype=str),
            "Aircraft": pd.Series(dtype=str),
            "Height above takeoff (m)": pd.Series(dtype=float),
            "Camera heading (deg)": pd.Series(dtype=float),
            "Camera tilt (deg)": pd.Series(dtype=float),
        },
        geometry=gpd.GeoSeries([], crs="EPSG:4326"),
    )


def _empty_sightline_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "Photo": pd.Series(dtype=str),
            "Distance (m)": pd.Series(dtype=float),
        },
        geometry=gpd.GeoSeries([], crs="EPSG:4326"),
    )


def _parse_dt(dt_str: str) -> datetime:
    """Parse an ISO 8601 UTC string (Z or +00:00 suffix) to a tz-aware datetime."""
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def _datetime_from_filename(filepath: Path) -> datetime | None:
    """Extract datetime from DJI filename: DJIFlightRecord_YYYY-MM-DD_[HH-MM-SS].txt."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})_\[(\d{2})-(\d{2})-(\d{2})\]", filepath.stem)
    if not m:
        return None
    try:
        y, mo, d = (int(x) for x in m.group(1).split("-"))
        h, mi, s = int(m.group(2)), int(m.group(3)), int(m.group(4))
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    except Exception:
        return None


def _in_flight_window(dt_str: str, window_start: datetime, window_end: datetime) -> bool:
    """True if dt_str parses to a datetime within the flight window."""
    try:
        return window_start <= _parse_dt(dt_str) <= window_end
    except Exception:
        return False


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS-84 points."""
    R = 6_371_000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2.0 * R * atan2(sqrt(a), sqrt(1.0 - a))


def _valid_wgs84(lat: float, lon: float) -> bool:
    """Reject 0,0 and the DJI firmware large-number GPS bug."""
    return lat != 0.0 and lon != 0.0 and abs(lat) <= 90.0 and abs(lon) <= 180.0


def _write_kml(kml_text: str, aircraft_serial: str, takeoff_dt: datetime,
               results_dir: Path, kml_paths: list) -> None:
    """Persist a KML string to results_dir and append its path to kml_paths."""
    fname = f"{aircraft_serial}_{takeoff_dt.strftime('%Y%m%dT%H%M%SZ')}.kml"
    kml_path = results_dir / fname
    kml_path.write_text(kml_text, encoding="utf-8")
    kml_paths.append(str(kml_path))


# ---------------------------------------------------------------------------
# Flight-log ingestion loop (internal — called by ingest_mission)
# ---------------------------------------------------------------------------


def _run_flights(
    client: Any,
    input_folder: str,
    dji_api_key: str,
    event_type_name: str,
    aircraft_identity: dict,
    decimation_rate: int,
    results_dir: Path,
    operational_defaults: Any,
    patrol_type: str,
) -> dict:
    """
    Ingest all DJI .txt logs in input_folder. Returns a dict of outputs including
    `run_patrols`: a list of (start_dt, end_dt, segment_id) for every patrol
    created or matched this run, so the photo loop can link into the same segments.
    Per-file exceptions become 'failed' rows so one bad file never aborts the batch.
    """
    from dji_er_tasks._binary import get_binary_path

    out: dict = {
        "track_rows": [], "rows": [], "kml_paths": [], "run_patrols": [],
        "n_ingested": 0, "n_skipped": 0, "n_failed": 0,
        "total_flight_seconds": 0.0, "total_distance_m": 0.0,
    }

    folder = Path(input_folder)
    if not folder.exists():
        row = {col: None for col in _FLIGHT_RESULTS_COLUMNS}
        row.update({"file": str(folder), "status": "failed",
                    "error": f"Folder not found: {folder}"})
        out["rows"].append(row)
        out["n_failed"] += 1
        return out

    binary = get_binary_path()

    # Resolve event type UUID once (blank = tracking-only mode).
    if event_type_name:
        event_types_df = client.get_event_types()
        et_match = event_types_df[event_types_df["value"] == event_type_name].drop_duplicates("id")
        if et_match.empty:
            raise ValueError(
                f"Event type '{event_type_name}' not found in EarthRanger. "
                "Create it in ER Admin -> Event Types using the template in the "
                f"repository README, with slug set to '{event_type_name}'."
            )
        event_type_uuid = str(et_match.iloc[0]["id"])
    else:
        event_type_uuid = None

    _nof = ""
    if isinstance(operational_defaults, dict):
        _nof = (operational_defaults.get("nature_of_flight") or "").strip()

    # Validate the patrol type slug once (patrol types are ER Admin-managed).
    _patrol_type = (patrol_type or "").strip() if isinstance(patrol_type, str) else ""
    if _patrol_type:
        patrol_types_df = client.get_patrol_types()
        known_types = set(patrol_types_df["value"]) if not patrol_types_df.empty else set()
        if _patrol_type not in known_types:
            raise ValueError(
                f"Patrol type '{_patrol_type}' not found in EarthRanger. "
                "Use the slug of an existing patrol type (ER Admin -> Activity -> "
                "Patrol types, 'value' column), or create one there first — "
                "patrol types cannot be created via the API."
            )

    txt_files = sorted(folder.glob("*.txt"))

    # Ensure the DJI source provider exists (idempotent).
    try:
        client.post_sourceproviders(provider_key=_DJI_PROVIDER_KEY, display_name="DJI")
    except Exception:
        pass

    _subject_cache: dict[str, str] = {}
    _source_cache: dict[str, str] = {}

    for filepath in txt_files:
        row: dict = {col: None for col in _FLIGHT_RESULTS_COLUMNS}
        row["file"] = filepath.name

        try:
            # Decrypt + parse via dji-log (JSON to stdout, KML to tmp file).
            with tempfile.TemporaryDirectory() as tmpdir:
                kml_tmp = Path(tmpdir) / "flight.kml"
                result = subprocess.run(
                    [binary, str(filepath), "--api-key", dji_api_key, "--kml", str(kml_tmp)],
                    capture_output=True, encoding="utf-8", check=True, timeout=120,
                )
                flight_data = json.loads(result.stdout)
                kml_text = kml_tmp.read_text(encoding="utf-8") if kml_tmp.exists() else ""

            log_details = flight_data["details"]
            frames = flight_data["frames"]
            if not frames:
                raise ValueError("No frames decoded from log file.")

            aircraft_serial = log_details["aircraftSn"]
            if not aircraft_serial:
                raise ValueError("Aircraft serial number missing from log file.")

            battery_serial = frames[0]["recover"]["batterySn"]
            firmware = log_details.get("appVersion", "")

            takeoff_idx = next(
                (i for i, f in enumerate(frames)
                 if not f["osd"]["isOnGround"] and f["osd"]["isMotorOn"]), 0,
            )
            landing_idx = len(frames) - 1 - next(
                (i for i, f in enumerate(reversed(frames))
                 if not f["osd"]["isOnGround"] and f["osd"]["isMotorOn"]), 0,
            )

            takeoff_dt = _parse_dt(frames[takeoff_idx]["custom"]["dateTime"])
            landing_dt = _parse_dt(frames[landing_idx]["custom"]["dateTime"])

            # Guard: garbage (pre-GPS-lock / future) timestamps on the takeoff frame.
            _MIN_YEAR = 2015
            _MAX_YEAR = datetime.now(timezone.utc).year + 1
            if takeoff_dt.year < _MIN_YEAR or takeoff_dt.year > _MAX_YEAR:
                for f in frames[takeoff_idx:]:
                    try:
                        dt = _parse_dt(f["custom"]["dateTime"])
                        if dt.year >= _MIN_YEAR and not f["osd"]["isOnGround"]:
                            takeoff_dt = dt
                            break
                    except Exception:
                        continue
            if takeoff_dt.year < _MIN_YEAR:
                fallback = _datetime_from_filename(filepath)
                if fallback is not None:
                    takeoff_dt = fallback
            if landing_dt.year < _MIN_YEAR or landing_dt.year > _MAX_YEAR:
                landing_dt = takeoff_dt

            _win_margin = timedelta(minutes=5)
            _win_start = takeoff_dt - _win_margin
            _win_end = landing_dt + _win_margin

            battery_pct_takeoff = int(frames[takeoff_idx]["battery"]["chargeLevel"])
            battery_pct_landing = int(frames[landing_idx]["battery"]["chargeLevel"])

            # Reference point — 3-step GPS fallback, all WGS-84 validated.
            ref_lat = frames[takeoff_idx]["osd"]["latitude"]
            ref_lon = frames[takeoff_idx]["osd"]["longitude"]
            if not _valid_wgs84(ref_lat, ref_lon):
                for f in frames[takeoff_idx:landing_idx + 1]:
                    if _valid_wgs84(f["osd"]["latitude"], f["osd"]["longitude"]):
                        ref_lat, ref_lon = f["osd"]["latitude"], f["osd"]["longitude"]
                        break
            if not _valid_wgs84(ref_lat, ref_lon):
                for f in frames:
                    if _valid_wgs84(f["home"]["latitude"], f["home"]["longitude"]):
                        ref_lat, ref_lon = f["home"]["latitude"], f["home"]["longitude"]
                        break

            flight_time_s = float(log_details["totalTime"])
            max_alt_agl_m = float(log_details["maxHeight"])
            max_speed_ms = float(log_details["maxHorizontalSpeed"])

            # max/total distance from GPS frames (log_details['totalDistance'] units
            # vary by firmware; frame haversine is unit-agnostic).
            if ref_lat != 0.0 and ref_lon != 0.0:
                valid_pts = [
                    (f["osd"]["latitude"], f["osd"]["longitude"])
                    for f in frames
                    if f["osd"]["latitude"] != 0.0 and f["osd"]["longitude"] != 0.0
                    and _in_flight_window(f["custom"]["dateTime"], _win_start, _win_end)
                ]
                max_dist_m = max(
                    (_haversine_m(ref_lat, ref_lon, lat, lon) for lat, lon in valid_pts),
                    default=0.0,
                )
                total_dist_m_gps = sum(
                    _haversine_m(valid_pts[i][0], valid_pts[i][1],
                                 valid_pts[i + 1][0], valid_pts[i + 1][1])
                    for i in range(len(valid_pts) - 1)
                ) if len(valid_pts) >= 2 else 0.0
            else:
                max_dist_m = 0.0
                total_dist_m_gps = 0.0

            native_hz = len(frames) / max(flight_time_s, 1.0)
            step = max(1, round(native_hz / decimation_rate))
            frames_dec = frames[::step]

            airborne_dec = [
                f for f in frames_dec
                if f["osd"]["latitude"] != 0.0 and f["osd"]["longitude"] != 0.0
                and not f["osd"]["isOnGround"]
            ]
            if len(airborne_dec) >= 2:
                line = LineString(
                    [(f["osd"]["longitude"], f["osd"]["latitude"]) for f in airborne_dec]
                )
                out["track_rows"].append({
                    "geometry": line,
                    "aircraft_serial": aircraft_serial,
                    "takeoff_utc": takeoff_dt.isoformat(),
                    "flight_time_min": round(flight_time_s / 60, 1),
                    "track_color": _TRACK_PALETTE[len(out["track_rows"]) % len(_TRACK_PALETTE)],
                })

            # get-or-create Subject (keyed on aircraft_serial)
            if aircraft_serial not in _subject_cache:
                subjects_df = client.get_subjects(name=aircraft_serial, include_inactive=True)
                if subjects_df.empty:
                    new_sub = client.post_subject(
                        subject_name=aircraft_serial,
                        subject_type=aircraft_identity["subject_type"],
                        subject_subtype=aircraft_identity["subject_subtype"],
                        additional={"registration": aircraft_identity["registration"]},
                    )
                    _subject_cache[aircraft_serial] = str(new_sub.iloc[0]["id"])
                else:
                    _subject_cache[aircraft_serial] = str(subjects_df.iloc[0]["id"])
            subject_id = _subject_cache[aircraft_serial]

            # get-or-create Source (keyed on aircraft_serial as manufacturer_id)
            if aircraft_serial not in _source_cache:
                sources_df = client.get_sources(manufacturer_id=aircraft_serial)
                if sources_df.empty:
                    new_src = client.post_source(
                        source_type=aircraft_identity["source_type"],
                        manufacturer_id=aircraft_serial,
                        model_name=log_details.get("aircraftName", "DJI Aircraft"),
                        provider=_DJI_PROVIDER_KEY,
                    )
                    source_id = str(new_src.iloc[0]["id"])
                    client.post_subjectsource(
                        subject_id=subject_id,
                        source_id=source_id,
                        lower_bound_assigned_range=datetime(2000, 1, 1, tzinfo=timezone.utc),
                        upper_bound_assigned_range=datetime(2099, 1, 1, tzinfo=timezone.utc),
                    )
                else:
                    source_id = str(sources_df.iloc[0]["id"])
                _source_cache[aircraft_serial] = source_id
            source_id = _source_cache[aircraft_serial]

            # Dual idempotency: observations + event, independently.
            flight_key = f"{aircraft_serial}_{takeoff_dt.strftime('%Y%m%dT%H%M%SZ')}"
            existing_obs = client._get_observations(
                source_ids=source_id,
                since=(takeoff_dt - timedelta(seconds=2)).isoformat(),
                until=(takeoff_dt + timedelta(seconds=2)).isoformat(),
            )
            has_observations = not existing_obs.empty

            has_event = False
            matched_event_id = None
            if event_type_uuid:
                candidates = client.get_events(
                    event_type=[event_type_uuid],
                    since=(takeoff_dt - timedelta(seconds=10)).isoformat(),
                    until=(takeoff_dt + timedelta(seconds=10)).isoformat(),
                )
                if not candidates.empty:
                    for _, ev in candidates.iterrows():
                        try:
                            ev_time = _parse_dt(str(ev.get("time") or ""))
                            if abs((ev_time - takeoff_dt).total_seconds()) <= 10:
                                has_event = True
                                matched_event_id = str(ev.get("id"))
                                break
                        except Exception:
                            continue

            # Patrol — third independent idempotency leg.
            patrol_segment_id = None
            patrol_status = ""
            if _patrol_type:
                existing_patrols = client.get_patrols(
                    since=(takeoff_dt - timedelta(seconds=30)).isoformat(),
                    until=(landing_dt + timedelta(seconds=30)).isoformat(),
                    patrol_type_value=_patrol_type,
                )
                if not existing_patrols.empty:
                    for _, pat in existing_patrols.iterrows():
                        for seg in (pat.get("patrol_segments") or []):
                            leader = seg.get("leader") or {}
                            if str(leader.get("id")) == subject_id:
                                patrol_segment_id = str(seg.get("id"))
                                patrol_status = "exists"
                                break
                        if patrol_segment_id:
                            break

                if patrol_segment_id is None:
                    end_lat, end_lon = ref_lat, ref_lon
                    for f in reversed(frames[takeoff_idx:landing_idx + 1]):
                        if _valid_wgs84(f["osd"]["latitude"], f["osd"]["longitude"]):
                            end_lat, end_lon = f["osd"]["latitude"], f["osd"]["longitude"]
                            break
                    segment_payload = {
                        "patrol_type": _patrol_type,
                        "time_range": {
                            "start_time": takeoff_dt.isoformat(),
                            "end_time": landing_dt.isoformat(),
                        },
                        "leader": {"content_type": "observations.subject", "id": subject_id},
                        "start_location": (
                            {"latitude": ref_lat, "longitude": ref_lon}
                            if _valid_wgs84(ref_lat, ref_lon) else None
                        ),
                        "end_location": (
                            {"latitude": end_lat, "longitude": end_lon}
                            if _valid_wgs84(end_lat, end_lon) else None
                        ),
                    }
                    new_patrol = client.post_patrol(
                        priority=0,
                        state="done",
                        title=f"UAS {aircraft_serial} {takeoff_dt.strftime('%Y-%m-%d %H:%M')}Z",
                        patrol_segments=[segment_payload],
                    )
                    new_segments = new_patrol.iloc[0].get("patrol_segments") or []
                    if new_segments:
                        patrol_segment_id = str(new_segments[0].get("id"))
                    patrol_status = "created"

                # Record this run's patrol window so the photo loop can link to it.
                if patrol_segment_id:
                    out["run_patrols"].append((takeoff_dt, landing_dt, patrol_segment_id))

                # Attach a pre-existing event to the patrol (merge, don't replace).
                # Best-effort: on a plain re-run the event is already linked, and
                # some erclient versions 404 on the raw event GET — never fail an
                # already-ingested flight over a re-link.
                if patrol_segment_id and has_event and matched_event_id:
                    try:
                        ev_data = client._get(f"activity/event/{matched_event_id}")
                        linked = [
                            str(s.get("id")) if isinstance(s, dict) else str(s)
                            for s in (ev_data.get("patrol_segments") or [])
                        ]
                        if patrol_segment_id not in linked:
                            client.patch_event(
                                matched_event_id,
                                {"patrol_segments": linked + [patrol_segment_id]},
                            )
                    except Exception:
                        pass

            fully_done = has_observations and (not event_type_uuid or has_event)

            common = {
                "aircraft_serial":     aircraft_serial,
                "takeoff_utc":         takeoff_dt.isoformat(),
                "flight_time_min":     round(flight_time_s / 60, 1),
                "battery_pct_takeoff": battery_pct_takeoff,
                "battery_pct_landing": battery_pct_landing,
                "battery_serial":      battery_serial,
                "max_alt_agl_m":       round(max_alt_agl_m, 1),
                "max_speed_ms":        round(max_speed_ms, 1),
                "max_dist_m":          round(max_dist_m, 1),
                "total_distance_m":    round(total_dist_m_gps, 1),
                "firmware":            firmware,
                "patrol":              patrol_status,
            }

            if fully_done:
                if kml_text:
                    _write_kml(kml_text, aircraft_serial, takeoff_dt, results_dir, out["kml_paths"])
                row.update(common)
                row["status"] = "skipped"
                out["n_skipped"] += 1
            else:
                if not has_observations:
                    airborne_all = [
                        f for f in frames_dec
                        if f["osd"]["latitude"] != 0.0 and f["osd"]["longitude"] != 0.0
                        and not f["osd"]["isOnGround"]
                        and _in_flight_window(f["custom"]["dateTime"], _win_start, _win_end)
                    ]
                    if airborne_all:
                        obs_gdf = gpd.GeoDataFrame(
                            {
                                "source": [source_id] * len(airborne_all),
                                "recorded_at": [f["custom"]["dateTime"] for f in airborne_all],
                                "device_status_properties": [
                                    {"altitude": f["osd"]["height"]} for f in airborne_all
                                ],
                            },
                            geometry=gpd.points_from_xy(
                                [f["osd"]["longitude"] for f in airborne_all],
                                [f["osd"]["latitude"] for f in airborne_all],
                            ),
                            crs="EPSG:4326",
                        )
                        client.post_observations(obs_gdf)

                if event_type_uuid and not has_event:
                    event_location = (
                        {"latitude": ref_lat, "longitude": ref_lon}
                        if _valid_wgs84(ref_lat, ref_lon) else None
                    )
                    event_details = {
                        "flight_key":            flight_key,
                        "aircraft_serial":       aircraft_serial,
                        "aircraft_registration": aircraft_identity["registration"],
                        "flight_date":           takeoff_dt.date().isoformat(),
                        "start_time_utc":        takeoff_dt.isoformat(),
                        "end_time_utc":          landing_dt.isoformat(),
                        "flight_time_min":       round(flight_time_s / 60, 1),
                        "battery_pct_takeoff":   battery_pct_takeoff,
                        "battery_pct_landing":   battery_pct_landing,
                        "battery_serial":        battery_serial,
                        "max_alt_agl_m":         round(max_alt_agl_m, 1),
                        "max_speed_ms":          round(max_speed_ms, 1),
                        "max_dist_m":            round(max_dist_m, 1),
                        "total_distance_m":      round(total_dist_m_gps, 1),
                        "home_lat":              ref_lat,
                        "home_lon":              ref_lon,
                        "firmware":              firmware,
                    }
                    if _nof:
                        event_details["nature_of_flight"] = _nof
                    event_payload = {
                        "event_type": event_type_name,
                        "time": takeoff_dt.isoformat(),
                        "location": event_location,
                        "event_details": event_details,
                    }
                    if patrol_segment_id:
                        event_payload["patrol_segments"] = [patrol_segment_id]
                    client.post_event(event_payload)

                if kml_text:
                    _write_kml(kml_text, aircraft_serial, takeoff_dt, results_dir, out["kml_paths"])

                row.update(common)
                row["status"] = "ingested"
                out["n_ingested"] += 1
                out["total_flight_seconds"] += flight_time_s
                out["total_distance_m"] += total_dist_m_gps

        except Exception as exc:
            row["status"] = "failed"
            row["error"] = f"{type(exc).__name__}: {exc}"
            out["n_failed"] += 1

        out["rows"].append(row)

    return out


# ---------------------------------------------------------------------------
# Photo geolocation loop (internal — called by ingest_mission)
# ---------------------------------------------------------------------------


def _run_photos(
    client: Any,
    photos_folder: str,
    dem_path: str,
    event_type_name: str,
    patrol_type: str,
    utc_offset_hours: float,
    results_dir: Path,
    run_patrols: list,
) -> dict:
    """
    Geolocate every JPEG in photos_folder against a DEM and post imagery events.
    If dem_path is blank the DEM is auto-fetched from Copernicus GLO-30 for the
    photos' GPS footprint. run_patrols (from _run_flights) lets a photo link to a
    patrol segment created this same run without an extra ER query.
    """
    from dji_er_tasks.demfetch import bbox_from_points, fetch_dem
    from dji_er_tasks.geolocate import (
        Dem, PoseError, RaycastError, extract_pose, photo_time_utc, raycast,
    )

    out: dict = {
        "target_rows": [], "drone_rows": [], "sightline_rows": [], "rows": [],
        "n_posted": 0, "n_located": 0, "n_skipped": 0, "n_failed": 0,
    }

    folder = Path(photos_folder)
    if not folder.exists():
        row = {col: None for col in _PHOTO_RESULTS_COLUMNS}
        row.update({"file": str(folder), "status": "failed",
                    "error": f"Folder not found: {folder}"})
        out["rows"].append(row)
        out["n_failed"] += 1
        return out

    # Resolve event type slug to UUID once (blank = preview mode, post nothing).
    event_type_uuid = None
    if event_type_name:
        event_types_df = client.get_event_types()
        et_match = event_types_df[event_types_df["value"] == event_type_name].drop_duplicates("id")
        if et_match.empty:
            raise ValueError(
                f"Event type '{event_type_name}' not found in EarthRanger. "
                "Create it in ER Admin -> Event Types using the template in the "
                f"repository README, with slug set to '{event_type_name}'."
            )
        event_type_uuid = str(et_match.iloc[0]["id"])

    _patrol_type = (patrol_type or "").strip() if isinstance(patrol_type, str) else ""
    if _patrol_type and event_type_uuid:
        patrol_types_df = client.get_patrol_types()
        known = set(patrol_types_df["value"]) if not patrol_types_df.empty else set()
        if _patrol_type not in known:
            raise ValueError(
                f"Patrol type '{_patrol_type}' not found in EarthRanger. "
                "Use the slug of an existing patrol type (ER Admin -> Activity -> "
                "Patrol types, 'value' column), or leave the field blank."
            )

    photos = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg")
    )

    # PASS 1: extract pose + timestamp for every photo. Failures are recorded now
    # and excluded from the DEM footprint. Successes carry forward to the raycast.
    candidates: list = []  # (photo, pose, taken_at)
    for photo in photos:
        row = {col: None for col in _PHOTO_RESULTS_COLUMNS}
        row["file"] = photo.name
        try:
            taken_at = photo_time_utc(photo, default_utc_offset_hours=utc_offset_hours)
            if taken_at is None:
                raise PoseError("No usable timestamp in photo metadata")
            pose = extract_pose(photo)
            row["time_utc"] = taken_at.isoformat()
            candidates.append((photo, pose, taken_at))
        except (PoseError, RaycastError) as exc:
            row["status"] = "failed"
            row["error"] = str(exc)
            out["n_failed"] += 1
            out["rows"].append(row)
        except Exception as exc:
            row["status"] = "failed"
            row["error"] = f"{type(exc).__name__}: {exc}"
            out["n_failed"] += 1
            out["rows"].append(row)

    if not candidates:
        return out

    # DEM: use the supplied file, else auto-fetch for the photos' footprint.
    try:
        dem_file = (dem_path or "").strip()
        if not dem_file:
            bbox = bbox_from_points(
                [(pose.lat, pose.lon) for _, pose, _ in candidates],
                margin_m=_MAX_RANGE_M,
            )
            # Round the box outward to 0.1 deg so repeat runs over the same area
            # reuse one cached DEM instead of re-downloading it every run.
            rb = (floor(bbox[0] * 10) / 10, floor(bbox[1] * 10) / 10,
                  ceil(bbox[2] * 10) / 10, ceil(bbox[3] * 10) / 10)
            key = hashlib.md5(repr(rb).encode()).hexdigest()[:12]
            cache_dir = Path(tempfile.gettempdir()) / "dji_er_dem_cache"
            cached = cache_dir / f"dem_{key}.tif"
            if cached.exists() and cached.stat().st_size > 0:
                dem_file = str(cached)
            else:
                dem_file = fetch_dem(rb, cached)
            # Keep a copy in this run's results folder for provenance.
            try:
                (results_dir / "dem").mkdir(parents=True, exist_ok=True)
                shutil.copy2(dem_file, results_dir / "dem" / "auto_dem.tif")
            except Exception:
                pass
        dem = Dem(dem_file)
    except Exception as exc:
        # DEM unavailable — every located candidate fails, but the run continues
        # (e.g. so the flight half's results still render).
        for photo, _pose, taken_at in candidates:
            row = {col: None for col in _PHOTO_RESULTS_COLUMNS}
            row.update({"file": photo.name, "time_utc": taken_at.isoformat(),
                        "status": "failed", "error": f"DEM unavailable: {exc}"})
            out["n_failed"] += 1
            out["rows"].append(row)
        return out

    # PASS 2: raycast, then post (unless preview mode).
    try:
        for photo, pose, taken_at in candidates:
            row = {col: None for col in _PHOTO_RESULTS_COLUMNS}
            row.update({"file": photo.name, "time_utc": taken_at.isoformat()})
            try:
                target = raycast(
                    pose.lat, pose.lon, pose.alt_abs_m,
                    pose.gimbal_yaw_deg, pose.gimbal_pitch_deg,
                    dem, max_range_m=_MAX_RANGE_M,
                )
                low_conf = (
                    abs(pose.gimbal_pitch_deg) < _LOW_CONF_PITCH_DEG
                    or target.slant_range_m > _LOW_CONF_SLANT_M
                )
                confidence = "low" if low_conf else "ok"
                row.update({
                    "target_lat":    round(target.lat, 6),
                    "target_lon":    round(target.lon, 6),
                    "slant_range_m": round(target.slant_range_m, 1),
                    "camera_alt_m":  round(target.camera_alt_m, 1),
                    "confidence":    confidence,
                })
                aircraft = f"{pose.make} {pose.model}".strip()
                out["target_rows"].append({
                    "geometry": Point(target.lon, target.lat),
                    "Photo": photo.name,
                    "Time (UTC)": taken_at.isoformat(),
                    "Distance from drone (m)": round(target.slant_range_m, 1),
                    "Ground elevation (m)": round(target.elevation_m, 1),
                    "Confidence": confidence,
                })
                out["drone_rows"].append({
                    "geometry": Point(pose.lon, pose.lat),
                    "Photo": photo.name,
                    "Time (UTC)": taken_at.isoformat(),
                    "Aircraft": aircraft,
                    "Height above takeoff (m)": (
                        round(pose.alt_rel_m, 1) if pose.alt_rel_m is not None else None
                    ),
                    "Camera heading (deg)": round(pose.gimbal_yaw_deg, 1),
                    "Camera tilt (deg)": round(pose.gimbal_pitch_deg, 1),
                })
                out["sightline_rows"].append({
                    "geometry": LineString([(pose.lon, pose.lat), (target.lon, target.lat)]),
                    "Photo": photo.name,
                    "Distance (m)": round(target.slant_range_m, 1),
                })

                if event_type_uuid is None:
                    row["status"] = "located"
                    out["n_located"] += 1
                    out["rows"].append(row)
                    continue

                # Idempotency: same type, ±5 s, same filename (include_details=True
                # is required — get_events omits event_details by default).
                has_event = False
                cand = client.get_events(
                    event_type=[event_type_uuid],
                    since=(taken_at - timedelta(seconds=5)).isoformat(),
                    until=(taken_at + timedelta(seconds=5)).isoformat(),
                    include_details=True,
                )
                if not cand.empty:
                    for _, ev in cand.iterrows():
                        details = ev.get("event_details") or {}
                        if isinstance(details, dict) and details.get("photo_filename") == photo.name:
                            has_event = True
                            break
                if has_event:
                    row["status"] = "skipped"
                    row["error"] = "already in EarthRanger (same photo + time)"
                    out["n_skipped"] += 1
                    out["rows"].append(row)
                    continue

                # Patrol link: first this run's freshly-created segments, then ER.
                patrol_segment_id = None
                if _patrol_type:
                    for pstart, pend, seg_id in run_patrols:
                        if pstart <= taken_at <= pend:
                            patrol_segment_id = seg_id
                            break
                    if patrol_segment_id is None:
                        patrols = client.get_patrols(
                            since=(taken_at - timedelta(seconds=1)).isoformat(),
                            until=(taken_at + timedelta(seconds=1)).isoformat(),
                            patrol_type_value=_patrol_type,
                        )
                        if not patrols.empty:
                            for _, pat in patrols.iterrows():
                                for seg in (pat.get("patrol_segments") or []):
                                    tr = seg.get("time_range") or {}
                                    try:
                                        seg_start = pd.Timestamp(tr.get("start_time"))
                                        seg_end = pd.Timestamp(tr.get("end_time"))
                                        ts = pd.Timestamp(taken_at)
                                        if seg_start <= ts <= seg_end:
                                            patrol_segment_id = str(seg.get("id"))
                                            break
                                    except Exception:
                                        continue
                                if patrol_segment_id:
                                    break
                    row["patrol"] = "linked" if patrol_segment_id else "no match"

                event_payload = {
                    "event_type": event_type_name,
                    "time": taken_at.isoformat(),
                    "location": {"latitude": target.lat, "longitude": target.lon},
                    "event_details": {
                        "photo_filename":     photo.name,
                        "taken_at_utc":       taken_at.isoformat(),
                        "aircraft":           aircraft,
                        "camera_lat":         round(pose.lat, 7),
                        "camera_lon":         round(pose.lon, 7),
                        "camera_alt_m":       round(target.camera_alt_m, 1),
                        "gimbal_yaw_deg":     pose.gimbal_yaw_deg,
                        "gimbal_pitch_deg":   pose.gimbal_pitch_deg,
                        "target_elevation_m": round(target.elevation_m, 1),
                        "slant_range_m":      round(target.slant_range_m, 1),
                        "confidence":         confidence,
                        "method": (
                            "terrain raycast (centre of frame)"
                            + (" — LOW CONFIDENCE: shallow angle / long slant range, "
                               "target position is approximate" if low_conf else "")
                        ),
                    },
                }
                if patrol_segment_id:
                    event_payload["patrol_segments"] = [patrol_segment_id]

                new_ev = client.post_event(event_payload)
                event_id = str(new_ev.iloc[0]["id"])
                try:
                    client.post_event_file(event_id, filepath=str(photo), comment="")
                except Exception as exc:
                    row["error"] = f"event posted but photo upload failed: {exc}"

                row["status"] = "ingested"
                out["n_posted"] += 1

            except (PoseError, RaycastError) as exc:
                row["status"] = "failed"
                row["error"] = str(exc)
                out["n_failed"] += 1
            except Exception as exc:
                row["status"] = "failed"
                row["error"] = f"{type(exc).__name__}: {exc}"
                out["n_failed"] += 1

            out["rows"].append(row)
    finally:
        dem.close()

    return out


# ---------------------------------------------------------------------------
# Form-field tasks — each exposes one config section to the Desktop form
# ---------------------------------------------------------------------------


@register(
    description=(
        "Full path to the folder of DJI .txt flight logs (blank = skip flight-log "
        "ingestion). Copy the logs folder off your DJI controller via USB and paste "
        "the path here."
    )
)
def set_input_folder(
    folder_path: Annotated[
        str,
        Field(
            title="Flight Logs Folder",
            description=(
                "Full path to a folder of DJI .txt flight records exported via USB from "
                "your DJI controller. All .txt files in it are processed into GPS tracks "
                "and Flight Folio events. Windows: C:\\Users\\you\\Documents\\FlightLogs. "
                "macOS: avoid spaces in the path. Leave blank to skip flight-log "
                "ingestion and process photos only."
            ),
            default="",
        ),
    ] = "",
) -> str:
    return folder_path


@register(
    description=(
        "Full path to the folder of drone photos (.jpg) copied from the aircraft SD "
        "card (blank = skip imagery). Photos must retain their original camera metadata."
    )
)
def set_photos_folder(
    folder_path: Annotated[
        str,
        Field(
            title="Photos Folder",
            description=(
                "Full path to a folder of drone JPEG photos. Each is geolocated by "
                "terrain raycast and posted as an imagery event at the ground point the "
                "camera was aimed at. Copy photos straight from the SD card — images that "
                "have passed through messaging apps or editors lose the camera-orientation "
                "metadata this depends on. Leave blank to skip imagery and process flight "
                "logs only."
            ),
            default="",
        ),
    ] = "",
) -> str:
    return folder_path


@register(
    description=(
        "Your DJI developer key, used to decrypt .txt flight logs (only needed when a "
        "Flight Logs Folder is set). Get one free at developer.dji.com (create an Open "
        "API app, activate via email, copy the ApiKey)."
    )
)
def set_dji_api_key(
    dji_api_key: Annotated[
        str,
        Field(
            title="DJI API Key",
            description=(
                "Your DJI developer API key (also called 'ApiKey' or 'SDK Key'), required "
                "to decrypt DJI .txt flight logs. Get one free at developer.dji.com: Create "
                "App -> select Open API -> activate via email -> copy the ApiKey. Only used "
                "when a Flight Logs Folder is set. The decryption keychain is cached, so DJI "
                "connectivity is only needed on first decrypt."
            ),
            default="",
        ),
    ] = "",
) -> str:
    return dji_api_key


@register(
    description=(
        "Recommended slug: uas_flight_folio (keep the default). Create this event type once "
        "in ER Admin from schemas/uas_flight_folio_schema.json — see README. Leave blank for "
        "tracking-only mode: GPS tracks posted, no Flight Folio events."
    )
)
def set_event_type_name(
    event_type_name: Annotated[
        str,
        Field(
            title="Flight Folio Event Type",
            description=(
                "Recommended slug: uas_flight_folio — create this event type once in ER "
                "Admin from schemas/uas_flight_folio_schema.json (see README), then keep this "
                "default. It is the lowercase-underscore 'value' in ER Admin -> Event Types "
                "(not the display name). Leave blank for tracking-only mode: GPS tracks are "
                "posted but no Flight Folio events."
            ),
            default="uas_flight_folio",
        ),
    ] = "uas_flight_folio",
) -> str:
    return event_type_name


@register(
    description=(
        "Recommended slug: uas_imagery (keep the default). Create this event type once in ER "
        "Admin from schemas/uas_imagery_schema.json — see README. Leave blank for preview "
        "mode: photos geolocated and shown on the map, nothing posted."
    )
)
def set_imagery_event_type(
    event_type_name: Annotated[
        str,
        Field(
            title="Imagery Event Type",
            description=(
                "Recommended slug: uas_imagery — create this event type once in ER Admin from "
                "schemas/uas_imagery_schema.json (see README), then keep this default. It is "
                "the lowercase-underscore 'value' in ER Admin -> Event Types (not the display "
                "name). Leave blank for preview mode: photos are geolocated and shown on the "
                "map but nothing is posted — handy to check geolocation before writing."
            ),
            default="uas_imagery",
        ),
    ] = "uas_imagery",
) -> str:
    return event_type_name


@register(
    description=(
        "Optional path to a local DEM GeoTIFF (geographic lat/lon CRS) for the photo "
        "raycast. Leave blank to auto-download Copernicus GLO-30 for your photos' area — "
        "set a path only for offline use or to use your own elevation data."
    )
)
def set_dem_path(
    dem_path: Annotated[
        str,
        Field(
            title="DEM File (optional)",
            description=(
                "Leave blank (recommended) and the workflow auto-fetches a Copernicus "
                "GLO-30 (30 m) Digital Elevation Model for the exact area your photos cover, "
                "from a keyless public mirror — no download or account needed. Set a path "
                "only to run offline or use your own DEM: it must be a GeoTIFF in a geographic "
                "CRS (EPSG:4326 lat/lon) covering your area of operations plus a generous "
                "margin, since photos aimed at distant terrain need coverage out to that terrain."
            ),
            default="",
        ),
    ] = "",
) -> str:
    return dem_path


@register(
    description=(
        "The slug of an existing EarthRanger patrol type — e.g. drone_patrol (create it in "
        "ER Admin -> Activity -> Patrol types; it cannot be made via the API). Blank = no "
        "patrols. Flights are filed as patrols of this type, and photos are linked to the "
        "patrol whose time range contains them."
    )
)
def set_patrol_type(
    patrol_type: Annotated[
        str,
        Field(
            title="Patrol Type",
            description=(
                "The EarthRanger patrol type slug (ER Admin -> Activity -> Patrol types, "
                "'value' column, not the display name). Example: 'drone_patrol'. Must already "
                "exist — patrol types cannot be created via the API. Each flight becomes one "
                "patrol (takeoff to landing, aircraft as leader); each photo is attached to "
                "the patrol segment whose time range contains it, so tracks, Flight Folio "
                "events and photos all appear together under the flight. Leave blank to skip "
                "patrols."
            ),
            default="",
        ),
    ] = "",
) -> str:
    return patrol_type


@register(
    description=(
        "Identifies the drone as an EarthRanger Subject by its serial number. The Subject "
        "and Source are created automatically if absent; these fields set their type/subtype. "
        "Only used when a Flight Logs Folder is set."
    )
)
def set_aircraft_identity(
    registration: Annotated[
        str,
        Field(
            title="Aircraft Registration",
            description=(
                "Legal registration as it appears on the airframe (e.g. ZT-000001). Stored "
                "on the EarthRanger Subject as metadata. Leave blank if unregistered."
            ),
            default="",
        ),
    ] = "",
    subject_type: Annotated[
        str,
        Field(
            title="Subject Type",
            description=(
                "EarthRanger subject type slug for this aircraft (ER Admin -> Subject Types). "
                "Must already exist. Example: 'aircraft'."
            ),
            default="aircraft",
        ),
    ] = "aircraft",
    subject_subtype: Annotated[
        str,
        Field(
            title="Subject Subtype",
            description=(
                "EarthRanger subject subtype slug (ER Admin -> Subject Types -> your type -> "
                "Subtypes). Must already exist. Example: 'drone_quadcopter'."
            ),
            default="drone_quadcopter",
        ),
    ] = "drone_quadcopter",
    source_type: Annotated[
        str,
        Field(
            title="Source Type",
            description=(
                "EarthRanger source type slug for GPS tracks from your DJI aircraft (ER Admin "
                "-> Source Types). Must already exist. Example: 'tracking-device'."
            ),
            default="tracking-device",
        ),
    ] = "tracking-device",
) -> dict:
    """Bundle aircraft identity config into a single dict for ingest_mission."""
    return {
        "registration": registration,
        "subject_type": subject_type,
        "subject_subtype": subject_subtype,
        "source_type": source_type,
    }


@register(
    description=(
        "How many GPS fixes per second to post to EarthRanger from flight logs. DJI logs "
        "at ~10 Hz; the default 1 Hz gives smooth tracks with 10x data reduction. "
        "Performance stats always use full-resolution data."
    )
)
def set_decimation_rate(
    rate_hz: Annotated[
        int,
        Field(
            title="Track Decimation Rate (Hz)",
            description=(
                "GPS fixes per second to post to EarthRanger. DJI logs at ~10 Hz. 1 Hz "
                "(default) gives smooth tracks with a 10x data reduction. Performance envelope "
                "stats (max altitude, speed, distance) always use full-resolution data."
            ),
            default=1, ge=1, le=10,
        ),
    ] = 1,
) -> int:
    return rate_hz


@register(
    description=(
        "Operational settings applied to every flight in this batch. Leave Nature of Flight "
        "blank for mixed batches — it can be set per-flight later in EarthRanger."
    )
)
def set_operational_defaults(
    nature_of_flight: Annotated[
        Literal["", "vlos", "r_vlos", "e_vlos", "b_vlos", "d_vlos"],
        Field(
            title="Nature of Flight",
            description=(
                "Operational category applied to all Flight Folio events in this run. Leave "
                "blank for mixed batches — the field stays unset and can be filled per-flight "
                "in EarthRanger."
            ),
            default="",
        ),
    ] = "",
) -> dict:
    """Bundle operational defaults into a dict for ingest_mission."""
    return {"nature_of_flight": nature_of_flight}


@register(
    description=(
        "Fallback UTC offset for photo timestamps, used only when a photo lacks GPS time "
        "metadata (most drone photos carry GPS time and ignore this)."
    )
)
def set_time_defaults(
    utc_offset_hours: Annotated[
        float,
        Field(
            title="Camera Clock UTC Offset",
            description=(
                "Hours to subtract to convert the camera's local clock to UTC, used only for "
                "photos with no GPS time or explicit timezone (rare). Example: 2 for South "
                "Africa (SAST = UTC+2)."
            ),
            default=2.0, ge=-12.0, le=14.0,
        ),
    ] = 2.0,
) -> float:
    return utc_offset_hours


# ---------------------------------------------------------------------------
# Main task
# ---------------------------------------------------------------------------


@register()
def ingest_mission(
    client: Any,
    root_path: str,
    input_folder: str,
    photos_folder: str,
    dji_api_key: str,
    event_type_name: str,
    imagery_event_type: str,
    aircraft_identity: Any,
    decimation_rate: int,
    operational_defaults: Any,
    dem_path: str,
    patrol_type: str,
    utc_offset_hours: float,
) -> Any:
    """
    Ingest a drone mission: DJI flight logs and/or geolocated photos, into EarthRanger.
    Either input is optional (blank folder skips that half). When both are present the
    flights are ingested first so photos can link to the patrols they created.
    """
    from ecoscope.platform.connections import EarthRangerConnection

    results_dir = _resolve_results_dir(root_path)
    results_dir.mkdir(parents=True, exist_ok=True)

    has_flights = bool((input_folder or "").strip())
    has_photos = bool((photos_folder or "").strip())
    photos_will_post = has_photos and bool((imagery_event_type or "").strip())

    # Resolve the ER client only when something will actually read/write ER:
    # flights always write (tracks); photos only when an imagery event type is set.
    # A photos-only preview run therefore needs zero ER credentials.
    need_client = has_flights or photos_will_post
    if isinstance(client, str) and need_client:
        client = EarthRangerConnection.client_from_named_connection(client)

    flights = None
    if has_flights:
        flights = _run_flights(
            client=client,
            input_folder=input_folder,
            dji_api_key=dji_api_key,
            event_type_name=event_type_name,
            aircraft_identity=aircraft_identity,
            decimation_rate=decimation_rate,
            results_dir=results_dir,
            operational_defaults=operational_defaults,
            patrol_type=patrol_type,
        )

    run_patrols = flights["run_patrols"] if flights else []

    photos = None
    if has_photos:
        photos = _run_photos(
            client=client,
            photos_folder=photos_folder,
            dem_path=dem_path,
            event_type_name=imagery_event_type,
            patrol_type=patrol_type,
            utc_offset_hours=utc_offset_hours,
            results_dir=results_dir,
            run_patrols=run_patrols,
        )

    # --- Assemble the flight side ------------------------------------------------
    if flights and flights["track_rows"]:
        track_gdf = gpd.GeoDataFrame(flights["track_rows"], geometry="geometry", crs="EPSG:4326")
    else:
        track_gdf = _empty_track_gdf()

    flight_rows = flights["rows"] if flights else []
    flight_results_df = (
        pd.DataFrame(flight_rows, columns=_FLIGHT_RESULTS_COLUMNS)
        if flight_rows else pd.DataFrame(columns=_FLIGHT_RESULTS_COLUMNS)
    )
    if not flight_results_df.empty:
        ingested_mask = flight_results_df["status"] == "ingested"
        n_aircraft = int(flight_results_df.loc[ingested_mask, "aircraft_serial"].nunique())
    else:
        n_aircraft = 0

    # --- Assemble the photo side -------------------------------------------------
    if photos and photos["target_rows"]:
        targets_gdf = gpd.GeoDataFrame(photos["target_rows"], geometry="geometry", crs="EPSG:4326")
    else:
        targets_gdf = _empty_targets_gdf()
    if photos and photos["drone_rows"]:
        drone_gdf = gpd.GeoDataFrame(photos["drone_rows"], geometry="geometry", crs="EPSG:4326")
    else:
        drone_gdf = _empty_drone_gdf()
    if photos and photos["sightline_rows"]:
        sightline_gdf = gpd.GeoDataFrame(photos["sightline_rows"], geometry="geometry", crs="EPSG:4326")
    else:
        sightline_gdf = _empty_sightline_gdf()

    photo_rows = photos["rows"] if photos else []
    photo_results_df = (
        pd.DataFrame(photo_rows, columns=_PHOTO_RESULTS_COLUMNS)
        if photo_rows else pd.DataFrame(columns=_PHOTO_RESULTS_COLUMNS)
    )

    return MissionResult(
        track_gdf=track_gdf,
        targets_gdf=targets_gdf,
        drone_gdf=drone_gdf,
        sightline_gdf=sightline_gdf,
        flight_results_df=flight_results_df,
        photo_results_df=photo_results_df,
        n_ingested=flights["n_ingested"] if flights else 0,
        n_skipped=flights["n_skipped"] if flights else 0,
        n_failed=flights["n_failed"] if flights else 0,
        total_flight_seconds=flights["total_flight_seconds"] if flights else 0.0,
        total_distance_m=flights["total_distance_m"] if flights else 0.0,
        n_aircraft=n_aircraft,
        n_posted=photos["n_posted"] if photos else 0,
        n_located=photos["n_located"] if photos else 0,
        n_photo_skipped=photos["n_skipped"] if photos else 0,
        n_photo_failed=photos["n_failed"] if photos else 0,
    )


# ---------------------------------------------------------------------------
# Accessor tasks
# ---------------------------------------------------------------------------


@register()
def extract_track_gdf(mission_result: Any) -> _GDF:
    """Combined flight-tracks GeoDataFrame (LineString per flight)."""
    return mission_result.track_gdf


@register()
def extract_targets_gdf(mission_result: Any) -> _GDF:
    """Located ground-target points GeoDataFrame (one per photo)."""
    return mission_result.targets_gdf


@register()
def extract_drone_gdf(mission_result: Any) -> _GDF:
    """Drone position points GeoDataFrame (one per photo)."""
    return mission_result.drone_gdf


@register()
def extract_sightline_gdf(mission_result: Any) -> _GDF:
    """Drone-to-target line GeoDataFrame (one per photo)."""
    return mission_result.sightline_gdf


@register()
def extract_flight_results_df(mission_result: Any) -> _GDF:
    """Per-file flight status DataFrame."""
    return mission_result.flight_results_df


@register()
def extract_photo_results_df(mission_result: Any) -> _GDF:
    """Per-photo status DataFrame."""
    return mission_result.photo_results_df


# ---------------------------------------------------------------------------
# Map layer combiner — assembles whichever layers are present into one list
# (handles skipped/empty layers itself, so it must NOT use any_dependency_skipped)
# ---------------------------------------------------------------------------


@register()
def combine_mission_layers(
    track_layer: Any = None,
    sightline_layer: Any = None,
    drone_layer: Any = None,
    target_layer: Any = None,
) -> list:
    """
    Collect the present map layers into a single geo_layers list for draw_ecomap.
    Any argument may be a real layer, None, or a SkipSentinel (when its branch/
    empty-df check skipped it); non-layers are dropped. Line drawn under the dots.
    """
    ordered = [track_layer, sightline_layer, drone_layer, target_layer]
    layers: list = []
    for item in ordered:
        if item is None:
            continue
        # SkipSentinel from a skipped upstream step — skip it (duck-typed to avoid
        # importing framework internals at module level).
        if item.__class__.__name__ == "SkipSentinel":
            continue
        if isinstance(item, list):
            layers.extend(item)
        else:
            layers.append(item)
    return layers


# ---------------------------------------------------------------------------
# Stat tasks — flight side
# ---------------------------------------------------------------------------


@register()
def count_ingested(mission_result: Any) -> int:
    """Flights successfully written to EarthRanger this run."""
    return mission_result.n_ingested


@register()
def count_skipped(mission_result: Any) -> int:
    """Flights skipped (already present in EarthRanger)."""
    return mission_result.n_skipped


@register()
def count_failed(mission_result: Any) -> int:
    """Flight-log files that failed (decrypt/parse/ER error)."""
    return mission_result.n_failed


@register()
def format_total_flight_time(mission_result: Any) -> str:
    """Total flight time across ingested flights, formatted H:MM."""
    total_s = mission_result.total_flight_seconds
    if total_s <= 0:
        return "0:00"
    h = int(total_s // 3600)
    m = int((total_s % 3600) // 60)
    return f"{h}:{m:02d}"


@register()
def format_total_distance(mission_result: Any) -> str:
    """Total GPS distance flown across ingested flights, formatted X.X km."""
    km = mission_result.total_distance_m / 1000.0
    return f"{km:.1f} km" if km > 0 else "0.0 km"


@register()
def count_aircraft(mission_result: Any) -> int:
    """Unique aircraft serials in this batch."""
    return mission_result.n_aircraft


# ---------------------------------------------------------------------------
# Stat tasks — photo side
# ---------------------------------------------------------------------------


@register()
def count_posted(mission_result: Any) -> int:
    """Imagery events posted to EarthRanger this run."""
    return mission_result.n_posted


@register()
def count_located(mission_result: Any) -> int:
    """Photos geolocated in preview mode (nothing posted)."""
    return mission_result.n_located


@register()
def count_photo_skipped(mission_result: Any) -> int:
    """Photos skipped (event already present in EarthRanger)."""
    return mission_result.n_photo_skipped


@register()
def count_photo_failed(mission_result: Any) -> int:
    """Photos that could not be geolocated or posted."""
    return mission_result.n_photo_failed
