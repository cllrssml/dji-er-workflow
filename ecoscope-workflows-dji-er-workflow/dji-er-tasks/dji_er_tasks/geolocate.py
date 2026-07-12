"""
uas_geolocate — locate the ground point a drone camera was aimed at.

Combines the camera pose embedded in a drone photo (GPS position, altitude,
gimbal yaw/pitch from EXIF/XMP) with a Digital Elevation Model: a ray is cast
from the camera along the gimbal direction and marched forward until it
intersects the terrain surface (monoplotting / terrain raycast).

Approach follows the technique popularised by OpenAthena
(https://github.com/Theta-Limited/OpenAthena, LGPL-2.1); this is an
independent implementation using rasterio with bilinear DEM sampling and
binary-search refinement of the intersection.

Requires: numpy, rasterio, Pillow.

Vertical datum note: DJI AbsoluteAltitude is approximately orthometric (MSL,
EGM96-ish, barometrically drifty). SRTM and Copernicus GLO-30 DEMs are also
orthometric (EGM96 / EGM2008), so the two are used together directly; the
datum mismatch (<1 m) is far below DJI barometric error. Where a takeoff
point is known, alt_mode="relative" uses RelativeAltitude + DEM elevation at
the takeoff point instead, which is usually more accurate.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio

_EARTH_RADIUS_M = 6_371_000.0


# ---------------------------------------------------------------------------
# Camera pose extraction (DJI XMP)
# ---------------------------------------------------------------------------


@dataclass
class CameraPose:
    lat: float                 # camera (drone) latitude, WGS-84 degrees
    lon: float                 # camera longitude
    alt_abs_m: float           # AbsoluteAltitude (orthometric-ish, barometric)
    alt_rel_m: float | None    # RelativeAltitude above takeoff point, if present
    gimbal_yaw_deg: float      # camera azimuth, 0 = north, clockwise
    gimbal_pitch_deg: float    # 0 = horizontal, -90 = straight down
    make: str = ""
    model: str = ""
    timestamp: str = ""        # EXIF DateTimeOriginal, if present


class PoseError(ValueError):
    """Photo lacks the metadata needed to geolocate it."""


# DJI XMP stores values either as XML attributes (tag="val") or elements
# (<tag>val</tag>); match both.
def _xmp_value(xmp: str, tag: str) -> float | None:
    m = re.search(tag + r'\s*=\s*"([-+]?[0-9.]+)"', xmp)
    if m is None:
        m = re.search(r"<" + tag + r">\s*([-+]?[0-9.]+)\s*</" + tag + r">", xmp)
    return float(m.group(1)) if m else None


def extract_pose(image_path: str | Path) -> CameraPose:
    """Read camera pose from a DJI JPEG's XMP packet (+ EXIF make/model/time)."""
    data = Path(image_path).read_bytes()

    start = data.find(b"<x:xmpmeta")
    end = data.find(b"</x:xmpmeta>")
    if start == -1 or end == -1:
        raise PoseError("No XMP metadata packet found in image")
    xmp = data[start : end + len(b"</x:xmpmeta>")].decode("utf-8", errors="replace")

    if "drone-dji:" not in xmp:
        raise PoseError("No drone-dji XMP tags — unsupported drone make or stripped metadata")

    lat = _xmp_value(xmp, "drone-dji:GpsLatitude")
    # older DJI firmware wrote 'GpsLongtitude' (sic)
    lon = _xmp_value(xmp, "drone-dji:GpsLongitude")
    if lon is None:
        lon = _xmp_value(xmp, "drone-dji:GpsLongtitude")
    alt_abs = _xmp_value(xmp, "drone-dji:AbsoluteAltitude")
    alt_rel = _xmp_value(xmp, "drone-dji:RelativeAltitude")
    yaw = _xmp_value(xmp, "drone-dji:GimbalYawDegree")
    pitch = _xmp_value(xmp, "drone-dji:GimbalPitchDegree")

    missing = [
        name
        for name, val in [
            ("GpsLatitude", lat), ("GpsLongitude", lon),
            ("AbsoluteAltitude", alt_abs),
            ("GimbalYawDegree", yaw), ("GimbalPitchDegree", pitch),
        ]
        if val is None
    ]
    if missing:
        raise PoseError(f"XMP present but missing tags: {', '.join(missing)}")
    if lat == 0.0 and lon == 0.0:
        raise PoseError("GPS position is 0,0 — no GPS lock when photo was taken")
    if yaw == 0.0 and pitch == 0.0:
        raise PoseError("Gimbal yaw and pitch both exactly 0 — orientation data untrustworthy")

    make = model = timestamp = ""
    try:
        from PIL import Image
        from PIL.ExifTags import Base as ExifBase

        with Image.open(image_path) as img:
            exif = img.getexif()
            make = str(exif.get(ExifBase.Make, "")).strip("\x00 ").strip()
            model = str(exif.get(ExifBase.Model, "")).strip("\x00 ").strip()
            sub = exif.get_ifd(0x8769)  # Exif IFD
            timestamp = str(sub.get(ExifBase.DateTimeOriginal, "")).strip()
    except Exception:
        pass  # EXIF niceties are optional; pose comes from XMP

    return CameraPose(
        lat=lat, lon=lon, alt_abs_m=alt_abs, alt_rel_m=alt_rel,
        gimbal_yaw_deg=yaw, gimbal_pitch_deg=pitch,
        make=make, model=model, timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# DEM sampling
# ---------------------------------------------------------------------------


class Dem:
    """A GeoTIFF DEM in geographic coordinates, sampled bilinearly."""

    def __init__(self, path: str | Path):
        self._ds = rasterio.open(path)
        if self._ds.crs is not None and not self._ds.crs.is_geographic:
            raise ValueError(
                f"DEM must be in a geographic CRS (lat/lon); got {self._ds.crs}"
            )
        self._band = self._ds.read(1).astype(np.float64)
        if self._ds.nodata is not None:
            self._band[self._band == self._ds.nodata] = np.nan
        self._inv = ~self._ds.transform

    def elevation(self, lat: float, lon: float) -> float:
        """Bilinear-interpolated elevation (m), NaN outside coverage."""
        col, row = self._inv * (lon, lat)
        col, row = col - 0.5, row - 0.5  # pixel-centre registration
        r0, c0 = int(math.floor(row)), int(math.floor(col))
        if r0 < 0 or c0 < 0 or r0 + 1 >= self._band.shape[0] or c0 + 1 >= self._band.shape[1]:
            return float("nan")
        fr, fc = row - r0, col - c0
        window = self._band[r0 : r0 + 2, c0 : c0 + 2]
        return float(
            window[0, 0] * (1 - fr) * (1 - fc)
            + window[0, 1] * (1 - fr) * fc
            + window[1, 0] * fr * (1 - fc)
            + window[1, 1] * fr * fc
        )

    def close(self):
        self._ds.close()


# ---------------------------------------------------------------------------
# Terrain raycast
# ---------------------------------------------------------------------------


@dataclass
class GroundTarget:
    lat: float
    lon: float
    elevation_m: float     # terrain elevation at the target (DEM datum)
    slant_range_m: float   # camera-to-target distance
    camera_alt_m: float    # camera altitude actually used for the cast


class RaycastError(ValueError):
    """The ray could not be intersected with the DEM."""


def _step(lat: float, lon: float, dist_m: float, azimuth_rad: float) -> tuple[float, float]:
    """Move dist_m along azimuth on the sphere; fine for sub-10-km ranges."""
    dlat = dist_m * math.cos(azimuth_rad) / _EARTH_RADIUS_M
    dlon = dist_m * math.sin(azimuth_rad) / (_EARTH_RADIUS_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def raycast(
    lat: float,
    lon: float,
    alt_m: float,
    azimuth_deg: float,
    pitch_deg: float,
    dem: Dem,
    max_range_m: float = 10_000.0,
    coarse_step_m: float = 5.0,
) -> GroundTarget:
    """
    March a ray from (lat, lon, alt_m) along azimuth_deg (0=N, clockwise) at
    signed elevation angle pitch_deg (DJI gimbal convention: 0 = horizontal,
    negative = down, positive = up) until it meets the DEM surface; refine by
    bisection.

    Downward rays hit the ground ahead. Level or upward rays hit rising terrain
    (a hillside or mountain slope) if any lies in the DEM within max_range_m — so
    a shot aimed at a slope is valid. A ray that never meets terrain (aimed above
    the skyline over flat or falling ground) raises RaycastError rather than
    inventing a target.
    """
    elev = math.radians(pitch_deg)
    # Fold a past-vertical gimbal over the top and reverse the azimuth.
    if elev > math.pi / 2:
        azimuth_deg += 180.0
        elev = math.pi - elev
    elif elev < -math.pi / 2:
        azimuth_deg += 180.0
        elev = -math.pi - elev
    az = math.radians(azimuth_deg % 360.0)

    start_ground = dem.elevation(lat, lon)
    if math.isnan(start_ground):
        raise RaycastError("Camera position is outside DEM coverage")
    if alt_m <= start_ground:
        raise RaycastError(
            f"Camera altitude {alt_m:.1f} m is at/below terrain "
            f"({start_ground:.1f} m) — bad altitude or wrong vertical datum"
        )

    # nadir shot: target is directly below
    if math.isclose(elev, -math.pi / 2, abs_tol=1e-9):
        return GroundTarget(lat, lon, start_ground, alt_m - start_ground, alt_m)

    sin_e, cos_e = math.sin(elev), math.cos(elev)

    def point_at(s: float) -> tuple[float, float, float]:
        """Position and ray height after s metres of slant travel (signed climb)."""
        plat, plon = _step(lat, lon, s * cos_e, az)
        return plat, plon, alt_m + s * sin_e

    # coarse march: find first step where the ray is at/below terrain
    prev_s = 0.0
    s = coarse_step_m
    hit_s = None
    while s <= max_range_m:
        plat, plon, ray_z = point_at(s)
        ground = dem.elevation(plat, plon)
        if math.isnan(ground):
            raise RaycastError(
                f"Ray left DEM coverage {s:.0f} m out — use a larger DEM extent"
            )
        if ray_z <= ground:
            hit_s = s
            break
        prev_s = s
        s += coarse_step_m
    if hit_s is None:
        raise RaycastError(
            f"No terrain intersection within {max_range_m:.0f} m — the camera was "
            "aimed above the skyline (at sky/horizon over flat or falling ground)"
        )

    # bisection refine between the last above-ground and first below-ground step
    lo, hi = prev_s, hit_s
    for _ in range(40):
        mid = (lo + hi) / 2.0
        plat, plon, ray_z = point_at(mid)
        ground = dem.elevation(plat, plon)
        if math.isnan(ground) or ray_z <= ground:
            hi = mid
        else:
            lo = mid
    final_s = (lo + hi) / 2.0
    tlat, tlon, _ = point_at(final_s)
    t_ground = dem.elevation(tlat, tlon)
    return GroundTarget(tlat, tlon, t_ground, final_s, alt_m)


def geolocate_photo(
    image_path: str | Path,
    dem: Dem,
    alt_mode: str = "absolute",
    takeoff_elevation_m: float | None = None,
    max_range_m: float = 10_000.0,
) -> tuple[GroundTarget, CameraPose]:
    """
    Full pipeline: extract pose from a DJI photo, cast against the DEM.

    alt_mode="absolute": use AbsoluteAltitude as-is (default).
    alt_mode="relative": use takeoff_elevation_m + RelativeAltitude — more
    accurate when the takeoff point's true elevation is known.
    """
    pose = extract_pose(image_path)
    if alt_mode == "relative":
        if pose.alt_rel_m is None:
            raise PoseError("RelativeAltitude tag not present in photo")
        if takeoff_elevation_m is None:
            raise ValueError("alt_mode='relative' requires takeoff_elevation_m")
        alt = takeoff_elevation_m + pose.alt_rel_m
    else:
        alt = pose.alt_abs_m
    target = raycast(
        pose.lat, pose.lon, alt,
        pose.gimbal_yaw_deg, pose.gimbal_pitch_deg,
        dem, max_range_m=max_range_m,
    )
    return target, pose


# ---------------------------------------------------------------------------
# Photo timestamp extraction
# ---------------------------------------------------------------------------


def photo_time_utc(image_path: str | Path, default_utc_offset_hours: float = 0.0):
    """
    Best-effort UTC timestamp for a photo, in preference order:

    1. EXIF GPS date/time stamps (already UTC, present once GPS is locked)
    2. DateTimeOriginal + OffsetTimeOriginal (written by recent DJI firmware)
    3. DateTimeOriginal shifted by default_utc_offset_hours (local clock guess)

    Returns a timezone-aware datetime, or None if no timestamp is present.
    """
    from datetime import datetime, timedelta, timezone

    from PIL import Image
    from PIL.ExifTags import Base as ExifBase

    with Image.open(image_path) as img:
        exif = img.getexif()
        gps = exif.get_ifd(0x8825)   # GPS IFD
        sub = exif.get_ifd(0x8769)   # Exif IFD

    # 1. GPS UTC stamps
    date_stamp = gps.get(29)  # GPSDateStamp "YYYY:MM:DD"
    time_stamp = gps.get(7)   # GPSTimeStamp (h, m, s) rationals
    if date_stamp and time_stamp:
        try:
            y, mo, d = (int(v) for v in str(date_stamp).split(":"))
            h, mi, s = (float(v) for v in time_stamp)
            return datetime(y, mo, d, tzinfo=timezone.utc) + timedelta(
                hours=h, minutes=mi, seconds=s
            )
        except Exception:
            pass

    dto = str(sub.get(ExifBase.DateTimeOriginal, "")).strip()
    if not dto:
        return None
    try:
        naive = datetime.strptime(dto, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None

    # 2. explicit EXIF offset
    off = str(sub.get(ExifBase.OffsetTimeOriginal, "")).strip()
    if off and len(off) >= 6 and off[0] in "+-":
        try:
            sign = 1 if off[0] == "+" else -1
            hh, mm = int(off[1:3]), int(off[4:6])
            tz = timezone(sign * timedelta(hours=hh, minutes=mm))
            return naive.replace(tzinfo=tz).astimezone(timezone.utc)
        except Exception:
            pass

    # 3. configured default offset
    tz = timezone(timedelta(hours=default_utc_offset_hours))
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)
