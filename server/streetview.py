"""Mapillary street imagery selection, facade projection and session-scoped photo textures."""
from dataclasses import dataclass
import io
import json
import logging
import os
from collections import OrderedDict
from urllib.parse import urlencode

import numpy as np

log = logging.getLogger("earthd.streetview")

API = "https://graph.mapillary.com/images"
FIELDS = ("id,computed_geometry,computed_compass_angle,camera_parameters,camera_type,"
          "captured_at,thumb_2048_url,width,height,quality_score")
SEARCH_MARGIN_M = 80.0
MAX_IMAGES = 16
MIN_CONFIDENCE = 0.12


@dataclass(frozen=True)
class Camera:
    """Pinhole camera pose in ENU; heading is clockwise from north, pitch positive down."""
    position: tuple
    heading: float
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    pitch: float = 0.0
    roll: float = 0.0

    @classmethod
    def from_fov(cls, position, heading, width, height, hfov, vfov=None, **pose):
        """Build intrinsics from horizontal/optional vertical field of view, in degrees."""
        width, height = int(width), int(height)
        fx = width / (2.0 * np.tan(np.radians(float(hfov)) / 2.0))
        fy = (height / (2.0 * np.tan(np.radians(float(vfov)) / 2.0)) if vfov is not None else fx)
        return cls(tuple(position), float(heading), width, height, float(fx), float(fy),
                   (width - 1) / 2.0, (height - 1) / 2.0, **pose)


def camera_basis(camera):
    """Return unit right/up/forward axes, accounting for heading, pitch and roll."""
    h, p, r = np.radians([camera.heading, camera.pitch, camera.roll])
    forward = np.array([np.sin(h) * np.cos(p), np.cos(h) * np.cos(p), -np.sin(p)])
    right = np.array([np.cos(h), -np.sin(h), 0.0])
    up = np.cross(right, forward)
    cr, sr = np.cos(r), np.sin(r)
    return right * cr + up * sr, up * cr - right * sr, forward


def project(points, camera, *, margin=0.0):
    """Project ENU points to normalized image UVs (v up), returning None if behind/outside.

    ``margin`` permits a caller to accept slight image-edge bleed. Returned UVs are
    compatible with Mesh.quad/cdae's v-up convention.
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    right, up, forward = camera_basis(camera)
    d = pts - np.asarray(camera.position, dtype=np.float64)
    depth = d @ forward
    if np.any(depth <= 1e-4):
        return None
    px = camera.cx + camera.fx * (d @ right) / depth
    py = camera.cy - camera.fy * (d @ up) / depth
    u, v = px / camera.width, 1.0 - py / camera.height
    if np.any(u < -margin) or np.any(u > 1.0 + margin) or np.any(v < -margin) or np.any(v > 1.0 + margin):
        return None
    return np.column_stack((u, v))


def polygon_area(points):
    """Unsigned 2D shoelace area in pixels squared."""
    p = np.asarray(points, dtype=np.float64)
    if len(p) < 3:
        return 0.0
    return abs(float(np.dot(p[:, 0], np.roll(p[:, 1], -1)) -
                     np.dot(p[:, 1], np.roll(p[:, 0], -1)))) * 0.5


def project_facade(points, normal, camera, *, min_pixels=1600.0, max_off_axis=65.0):
    """Return useful facade UVs plus a confidence score, or None when the view is poor.

    ``normal`` points out from the facade. The camera must be in front of it, the view
    angle must be within ``max_off_axis`` degrees, and the projected polygon must be
    sufficiently large and fully inside the source image.
    """
    p = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    n = np.asarray(normal, dtype=np.float64).reshape(3)
    nlen = np.linalg.norm(n)
    if len(p) < 3 or nlen < 1e-9:
        return None
    n /= nlen
    centre = p.mean(axis=0)
    to_camera = np.asarray(camera.position, dtype=np.float64) - centre
    dist = np.linalg.norm(to_camera)
    if dist < 1e-6:
        return None
    incidence = float(np.dot(n, to_camera / dist))
    if incidence <= 0.0 or incidence < np.cos(np.radians(max_off_axis)):
        return None
    uv = project(p, camera)
    if uv is None:
        return None
    pixels = np.column_stack((uv[:, 0] * camera.width, (1.0 - uv[:, 1]) * camera.height))
    area = polygon_area(pixels)
    if area < float(min_pixels):
        return None
    # Prefer broad, near-normal views without allowing huge nearby facades to dominate.
    area_score = min(1.0, area / (camera.width * camera.height * 0.12))
    confidence = float(incidence * np.sqrt(area_score))
    return {"uv": uv, "confidence": confidence, "incidence": incidence, "pixel_area": area,
            "distance": float(dist)}


def wall_faces(ring, z0, z1):
    """Yield wall quads and outward normals for a counter-clockwise footprint."""
    r = np.asarray(ring, dtype=np.float64)[:, :2]
    if len(r) > 1 and np.linalg.norm(r[0] - r[-1]) < 1e-8:
        r = r[:-1]
    for i, a in enumerate(r):
        b = r[(i + 1) % len(r)]
        edge = b - a
        length = np.linalg.norm(edge)
        if length < 1e-4:
            continue
        # CCW polygon: its exterior is the right-hand side of each directed edge.
        normal = np.array([edge[1], -edge[0], 0.0]) / length
        yield np.array([[a[0], a[1], z0], [b[0], b[1], z0],
                        [b[0], b[1], z1], [a[0], a[1], z1]]), normal


def best_facade(ring, z0, z1, camera, **limits):
    """Find the single best visible wall of a footprint for one image."""
    best = None
    for index, (quad, normal) in enumerate(wall_faces(ring, z0, z1)):
        result = project_facade(quad, normal, camera, **limits)
        if result and (best is None or result["confidence"] > best["confidence"]):
            best = dict(result, edge=index, points=quad)
    return best


class Mapillary:
    """Authenticated, rate-limited Mapillary API client used only when a token is configured."""
    def __init__(self, token, fetcher):
        self.token = token.strip() if token else ""
        self.fetcher = fetcher
        self._search_cache = OrderedDict()
        self._image_cache = OrderedDict()

    @property
    def enabled(self):
        return bool(self.token and self.fetcher is not None)

    def _get(self, url):
        return self.fetcher.get(url, kind="demand", headers={"Authorization": f"OAuth {self.token}"})

    def search(self, frame, x0, y0, x1, y1):
        """Return perspective captures in a padded tile bbox; failures simply mean no photo."""
        if not self.enabled:
            return []
        pad = SEARCH_MARGIN_M
        E = np.array([x0 - pad, x1 + pad, x1 + pad, x0 - pad])
        N = np.array([y0 - pad, y0 - pad, y1 + pad, y1 + pad])
        lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
        bbox = (float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()))
        cache_key = tuple(round(v, 4) for v in bbox)
        if cache_key in self._search_cache:
            self._search_cache.move_to_end(cache_key)
            return self._search_cache[cache_key]
        q = urlencode({"fields": FIELDS, "bbox": ",".join(f"{v:.7f}" for v in bbox), "limit": 100})
        try:
            r = self._get(f"{API}?{q}")
            rows = json.loads(r.body.decode("utf-8")).get("data", [])
        except Exception as e:
            log.debug("Mapillary image search unavailable: %s", e)
            return []
        out = []
        for row in rows:
            if row.get("camera_type") not in ("perspective", None) or not row.get("thumb_2048_url"):
                continue
            geom = row.get("computed_geometry") or row.get("geometry") or {}
            coords = geom.get("coordinates") if isinstance(geom, dict) else None
            angle = row.get("computed_compass_angle")
            params = row.get("camera_parameters") or []
            w, h = row.get("width"), row.get("height")
            if not coords or len(coords) < 2 or angle is None or not params or not w or not h:
                continue
            try:
                focal = float(params[0])
                if not (0.25 <= focal <= 2.5):  # Mapillary focal is normalized by the image's maximum dimension
                    continue
                out.append(dict(row, lon=float(coords[0]), lat=float(coords[1]), heading=float(angle) % 360.0,
                                focal=focal))
            except (TypeError, ValueError):
                continue
        self._search_cache[cache_key] = out
        if len(self._search_cache) > 64:
            self._search_cache.popitem(last=False)
        return out

    def image(self, row):
        """Download/decode one thumbnail, memoized for this server session."""
        key = str(row["id"])
        if key in self._image_cache:
            self._image_cache.move_to_end(key)
            return self._image_cache[key]
        try:
            r = self._get(row["thumb_2048_url"])
            from PIL import Image
            image = Image.open(io.BytesIO(r.body)).convert("RGB")
            image.load()
            self._image_cache[key] = image
            if len(self._image_cache) > 4:
                self._image_cache.popitem(last=False)
            return image
        except Exception as e:
            log.debug("Mapillary thumbnail %s unavailable: %s", key, e)
            self._image_cache[key] = None
            if len(self._image_cache) > 4:
                self._image_cache.popitem(last=False)
            return None

    def prepare_tile(self, items, frame, bbox, ground, output_dir):
        """Choose/project up to 16 unique captures and return per-image material records.

        ``items`` are buildings.py's tile-local (Spec, ground min, ground max) rows;
        successful specs receive ``streetview`` with a selected edge and image UVs.
        Returns ({materials: [...]}, image count). The procedural walls are intact
        for every unselected building and for all distant LODs.
        """
        if not self.enabled or not items:
            return None, 0
        x0, y0, x1, y1 = bbox
        candidates = self.search(frame, x0, y0, x1, y1)
        if not candidates:
            return None, 0
        for spec, gmin, gmax in items:
            spec.streetview = None
        winners = []
        for spec, gmin, gmax in items:
            import buildings
            mh = spec.min_h or 0.0
            base = buildings.base_z(spec, gmin, gmax)
            z0 = gmin - buildings.SKIRT_M if mh <= 0.5 else base + mh
            z1 = base + mh + spec.height
            best = None
            facade_ring = buildings.drawn_ring(spec)
            for row in candidates:
                try:
                    ce, cn, _ = frame.geodetic_to_enu(row["lat"], row["lon"], 0.0)
                    camera = Camera((float(ce) - x0, float(cn) - y0,
                                     float(ground(row["lat"], row["lon"])) + 1.6),
                                    row["heading"], int(row["width"]), int(row["height"]),
                                    row["focal"] * max(int(row["width"]), int(row["height"])),
                                    row["focal"] * max(int(row["width"]), int(row["height"])),
                                    (int(row["width"]) - 1) / 2.0, (int(row["height"]) - 1) / 2.0)
                    face = best_facade(facade_ring, z0, z1, camera, min_pixels=800.0, max_off_axis=48.0)
                except Exception:
                    continue
                if face and face["confidence"] >= MIN_CONFIDENCE:
                    candidate = (face["confidence"], row, camera, face)
                    if best is None or candidate[0] > best[0]:
                        best = candidate
            if best:
                winners.append((spec, best))
        # Avoid repeated photos in the atlas and cap the per-tile texture workload.
        by_id = {}
        for _spec, (_score, row, _camera, _face) in sorted(winners, key=lambda p: p[1][0], reverse=True):
            by_id.setdefault(str(row["id"]), row)
            if len(by_id) >= MAX_IMAGES:
                break
        if not by_id:
            return None, 0
        os.makedirs(output_dir, exist_ok=True)
        placements, material_rows = {}, []
        for image_id, row in by_id.items():
            safe_id = "".join(ch for ch in image_id if ch.isalnum() or ch in "_-")
            if not safe_id:
                continue
            image_path = os.path.join(output_dir, f"streetview_image_{safe_id}.jpg")
            if not os.path.isfile(image_path):
                image = self.image(row)
                if image is None:
                    continue
                try:
                    image.save(image_path, format="JPEG", quality=92, optimize=False)
                except OSError as e:
                    log.debug("Mapillary thumbnail %s could not be staged: %s", image_id, e)
                    continue
            name = f"bne_bld_photo_{safe_id}"
            placements[image_id] = name
            material_rows.append({"name": name, "texture": image_path})
        if not placements:
            return None, 0
        for spec, (_score, row, camera, face) in winners:
            image_id = str(row["id"])
            name = placements.get(image_id)
            if not name:
                continue
            spec.streetview = {"edge": face["edge"], "uv": face["uv"], "material": name,
                               "image_id": image_id, "confidence": face["confidence"]}
        return {"materials": material_rows}, len(placements)
