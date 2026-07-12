"""
demfetch — build a Digital Elevation Model for the raycast, automatically.

The imagery half of this workflow casts a ray from the drone camera onto a DEM
to find the ground point the camera was aimed at. Rather than making the user
download a DEM by hand, we derive the area of interest from the photos' own GPS
positions and mosaic Copernicus GLO-30 (30 m) tiles straight from the AWS Open
Data mirror — no API key, no Google Earth Engine account, just an HTTPS fetch
that GDAL/rasterio streams over /vsicurl/.

`bbox_from_points` sizes the box from the camera positions plus a margin (a
photo aimed at distant terrain needs coverage out to that terrain, so the margin
should be at least the raycast's max range). `fetch_dem` mosaics the whole 1x1
degree GLO-30 tiles covering that box into a single geographic-CRS GeoTIFF that
the `Dem` class in geolocate.py can sample directly.

Offline / air-gapped sites can bypass all of this by passing an explicit DEM
file to the workflow instead — see the "DEM File" form field.

Requires: rasterio (already a dependency of the raycast).
"""

from __future__ import annotations

import math
from pathlib import Path

# Copernicus GLO-30 Digital Surface Model, 1x1 degree COG tiles, keyless AWS mirror.
_TILE_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM/"
    "Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM.tif"
)

# Metres per degree of latitude (spherical approximation; good to ~0.5%).
_M_PER_DEG_LAT = 111_320.0


def _tile_name(lat_floor: int, lon_floor: int) -> str:
    lat = f"{'N' if lat_floor >= 0 else 'S'}{abs(lat_floor):02d}"
    lon = f"{'E' if lon_floor >= 0 else 'W'}{abs(lon_floor):03d}"
    return _TILE_URL.format(lat=lat, lon=lon)


def bbox_from_points(
    points: list[tuple[float, float]],
    margin_m: float,
) -> tuple[float, float, float, float]:
    """
    Bounding box (min_lon, min_lat, max_lon, max_lat) enclosing every (lat, lon)
    point, expanded by margin_m on all sides.

    The margin is converted to degrees using the highest-latitude point for the
    longitude padding (cos(lat) shrinks a degree of longitude toward the poles),
    so the box is never under-sized. Raises ValueError on an empty list.
    """
    if not points:
        raise ValueError("Cannot build a DEM bounding box from zero points")

    lats = [lat for lat, _ in points]
    lons = [lon for _, lon in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    dlat = margin_m / _M_PER_DEG_LAT
    worst_lat = max(abs(min_lat), abs(max_lat))
    cos_lat = max(math.cos(math.radians(worst_lat)), 1e-6)
    dlon = margin_m / (_M_PER_DEG_LAT * cos_lat)

    return (
        max(min_lon - dlon, -180.0),
        max(min_lat - dlat, -90.0),
        min(max_lon + dlon, 180.0),
        min(max_lat + dlat, 90.0),
    )


def fetch_dem(
    bbox: tuple[float, float, float, float],
    out_path: str | Path,
) -> str:
    """
    Mosaic the Copernicus GLO-30 tiles covering bbox (min_lon, min_lat, max_lon,
    max_lat) into a single deflate-compressed geographic-CRS GeoTIFF at out_path,
    and return that path.

    Tiles that 404 (ocean, no land data) are skipped — normal near coastlines.
    Raises RuntimeError if no tile in the box is available. The tiles are fetched
    over HTTPS via GDAL's /vsicurl/ handler, so the environment needs network
    access and a curl-enabled GDAL (standard in rasterio wheels).
    """
    import rasterio
    from rasterio.merge import merge

    min_lon, min_lat, max_lon, max_lat = bbox
    if not (min_lon < max_lon and min_lat < max_lat):
        raise ValueError(
            "bbox must be (min_lon, min_lat, max_lon, max_lat) with min < max"
        )

    urls = [
        _tile_name(la, lo)
        for la in range(math.floor(min_lat), math.floor(max_lat) + 1)
        for lo in range(math.floor(min_lon), math.floor(max_lon) + 1)
    ]

    sources = []
    for url in urls:
        try:
            sources.append(rasterio.open(f"/vsicurl/{url}"))
        except rasterio.errors.RasterioIOError:
            # Ocean tile — not in the dataset. Fine.
            continue
    if not sources:
        raise RuntimeError(
            "No Copernicus GLO-30 tiles are available for this area "
            f"(bbox={bbox}). If the flight was over water or the coordinates "
            "look wrong, check the photo GPS metadata; otherwise supply a DEM "
            "file manually via the DEM File field."
        )

    try:
        mosaic, transform = merge(sources, bounds=(min_lon, min_lat, max_lon, max_lat))
        profile = sources[0].profile.copy()
    finally:
        for src in sources:
            src.close()

    profile.update(
        height=mosaic.shape[1],
        width=mosaic.shape[2],
        transform=transform,
        count=1,
        driver="GTiff",
        compress="deflate",
        tiled=True,
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(mosaic[0], 1)

    return str(out_path)
