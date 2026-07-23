from __future__ import annotations

import math
from typing import Tuple

import numpy as np


def normalize(vector: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vector)
    if norm == 0:
        raise ValueError("Cannot normalize a zero-length vector.")
    return vector / norm


def build_camera_axes(
    forward: Tuple[float, float, float],
    up: Tuple[float, float, float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build a stable right-handed camera basis from direction and up vectors."""
    forward_vec = normalize(np.asarray(forward, dtype=np.float64))
    up_vec = normalize(np.asarray(up, dtype=np.float64))
    right_vec = normalize(np.cross(forward_vec, up_vec))
    corrected_up = normalize(np.cross(right_vec, forward_vec))
    return forward_vec, right_vec, corrected_up


def apply_panorama_angular_offsets(
    forward_vec: np.ndarray,
    right_vec: np.ndarray,
    up_vec: np.ndarray,
    *,
    yaw_offset_deg: float = 0.0,
    pitch_offset_deg: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return panorama axes with small residual angular corrections applied.

    The offset signs are deliberately defined in image space so an operator can
    read them directly from a reprojection QA overlay:

    * positive ``yaw_offset_deg`` moves projected world points to the right;
    * positive ``pitch_offset_deg`` moves projected world points downward.

    The returned basis remains orthonormal.  Applying the same basis to both
    pixel-to-ray and point-to-pixel operations therefore preserves exact
    projection round trips.
    """

    forward = normalize(np.asarray(forward_vec, dtype=np.float64))
    right = normalize(np.asarray(right_vec, dtype=np.float64))
    up = normalize(np.asarray(up_vec, dtype=np.float64))

    yaw = math.radians(float(yaw_offset_deg))
    yaw_forward = normalize((math.cos(yaw) * forward) - (math.sin(yaw) * right))
    yaw_right = normalize((math.sin(yaw) * forward) + (math.cos(yaw) * right))

    pitch = math.radians(float(pitch_offset_deg))
    corrected_forward = normalize(
        (math.cos(pitch) * yaw_forward) + (math.sin(pitch) * up)
    )
    corrected_up = normalize(
        (-math.sin(pitch) * yaw_forward) + (math.cos(pitch) * up)
    )
    return corrected_forward, yaw_right, corrected_up


def build_view_axes(
    forward_vec: np.ndarray,
    reference_up_vec: np.ndarray,
    fallback_right_vec: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build a stable view basis for a rectified perspective camera."""
    forward_vec = normalize(np.asarray(forward_vec, dtype=np.float64))
    reference_up_vec = normalize(np.asarray(reference_up_vec, dtype=np.float64))

    right_vec = np.cross(forward_vec, reference_up_vec)
    if np.linalg.norm(right_vec) < 1e-8:
        if fallback_right_vec is None:
            fallback_right_vec = np.asarray((1.0, 0.0, 0.0), dtype=np.float64)
        right_vec = np.cross(forward_vec, np.asarray(fallback_right_vec, dtype=np.float64))
        if np.linalg.norm(right_vec) < 1e-8:
            right_vec = np.cross(forward_vec, np.asarray((0.0, 1.0, 0.0), dtype=np.float64))

    right_vec = normalize(right_vec)
    up_vec = normalize(np.cross(right_vec, forward_vec))
    return forward_vec, right_vec, up_vec


def _overview_ray_rows(
    rays: np.ndarray | None,
    *,
    label: str,
    required: bool = False,
) -> np.ndarray:
    """Normalize an optional ray group into finite ``(N, 3)`` rows."""

    if rays is None:
        if required:
            raise ValueError(f"{label} must contain at least one ray")
        return np.empty((0, 3), dtype=np.float64)
    values = np.asarray(rays, dtype=np.float64)
    if values.shape == (3,):
        values = values[None, :]
    if values.ndim != 2 or values.shape[1] != 3:
        raise ValueError(f"{label} must have shape (N, 3) or (3,)")
    if values.shape[0] == 0:
        if required:
            raise ValueError(f"{label} must contain at least one ray")
        return np.empty((0, 3), dtype=np.float64)
    if not np.all(np.isfinite(values)):
        raise ValueError(f"{label} must contain only finite rays")
    norms = np.linalg.norm(values, axis=1)
    if np.any(norms <= 1e-12):
        raise ValueError(f"{label} cannot contain a zero-length ray")
    return values / norms[:, None]


def _build_roll_stable_view_axes(
    forward_vec: np.ndarray,
    reference_up_vec: np.ndarray,
    reference_right_vec: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build axes whose roll follows reference-up, including near its pole."""

    forward = normalize(np.asarray(forward_vec, dtype=np.float64))
    reference_up = normalize(np.asarray(reference_up_vec, dtype=np.float64))
    projected_up = reference_up - (forward * float(np.dot(reference_up, forward)))
    if np.linalg.norm(projected_up) >= 1e-8:
        up = normalize(projected_up)
        right = normalize(np.cross(forward, up))
        return forward, right, up

    if reference_right_vec is None:
        return build_view_axes(forward, reference_up)
    reference_right = normalize(np.asarray(reference_right_vec, dtype=np.float64))
    projected_right = reference_right - (
        forward * float(np.dot(reference_right, forward))
    )
    if np.linalg.norm(projected_right) < 1e-8:
        return build_view_axes(forward, reference_up, reference_right)
    right = normalize(projected_right)
    up = normalize(np.cross(right, forward))
    return forward, right, up


def fit_perspective_overview(
    sign_bbox_corner_rays: np.ndarray,
    pole_base_ray: np.ndarray | None,
    pole_axis_rays: np.ndarray | None,
    ground_support_rays: np.ndarray | None,
    reference_up_vec: np.ndarray,
    *,
    padding_deg: float,
    max_fov_deg: float,
    output_aspect_ratio: float,
    reference_right_vec: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float]:
    """Fit a roll-stable perspective overview containing every supplied ray.

    The sign corners, fitted pole axis endpoints, pole base and local-ground
    support are treated as one angular envelope.  The returned horizontal and
    vertical FOVs use a common focal scale for ``output_aspect_ratio`` so the
    overview has square pixels.  A ``ValueError`` is raised instead of silently
    clipping when the requested ``max_fov_deg`` cannot contain that envelope.
    """

    padding = float(padding_deg)
    max_fov = float(max_fov_deg)
    aspect = float(output_aspect_ratio)
    if not math.isfinite(padding) or padding < 0.0:
        raise ValueError("padding_deg must be finite and non-negative")
    if not math.isfinite(max_fov) or not 0.0 < max_fov < 180.0:
        raise ValueError("max_fov_deg must be finite and between 0 and 180")
    if not math.isfinite(aspect) or aspect <= 0.0:
        raise ValueError("output_aspect_ratio must be finite and positive")

    ray_groups = (
        _overview_ray_rows(
            sign_bbox_corner_rays,
            label="sign_bbox_corner_rays",
            required=True,
        ),
        _overview_ray_rows(pole_base_ray, label="pole_base_ray"),
        _overview_ray_rows(pole_axis_rays, label="pole_axis_rays"),
        _overview_ray_rows(ground_support_rays, label="ground_support_rays"),
    )
    rays = np.concatenate(ray_groups, axis=0)

    # A spherical centroid is a good initial direction, while the iterative
    # angular-midpoint updates below remove bias from groups with many points.
    summed_ray = rays.sum(axis=0)
    if np.linalg.norm(summed_ray) <= 1e-10:
        raise ValueError("Overview rays do not share a stable forward hemisphere")
    forward = normalize(summed_ray)

    reference_right = None
    if reference_right_vec is not None:
        reference_right = normalize(np.asarray(reference_right_vec, dtype=np.float64))
    for _ in range(20):
        forward, right, up = _build_roll_stable_view_axes(
            forward,
            reference_up_vec,
            reference_right,
        )
        local_x = rays @ right
        local_y = rays @ up
        local_z = rays @ forward
        if np.any(local_z <= 1e-8):
            raise ValueError("Overview rays do not fit inside one perspective hemisphere")
        horizontal_angles = np.arctan2(local_x, local_z)
        vertical_angles = np.arctan2(local_y, local_z)
        horizontal_shift = float(
            (horizontal_angles.min() + horizontal_angles.max()) * 0.5
        )
        vertical_shift = float((vertical_angles.min() + vertical_angles.max()) * 0.5)
        if max(abs(horizontal_shift), abs(vertical_shift)) < 1e-11:
            break
        if max(abs(horizontal_shift), abs(vertical_shift)) >= math.radians(89.0):
            raise ValueError("Overview angular envelope is too wide for perspective fitting")
        forward = normalize(
            forward
            + (right * math.tan(horizontal_shift))
            + (up * math.tan(vertical_shift))
        )

    forward, right, up = _build_roll_stable_view_axes(
        forward,
        reference_up_vec,
        reference_right,
    )
    local_x = rays @ right
    local_y = rays @ up
    local_z = rays @ forward
    if np.any(local_z <= 1e-8):
        raise ValueError("Overview rays do not fit inside one perspective hemisphere")

    padding_rad = math.radians(padding)
    horizontal_half_angle = float(
        np.max(np.abs(np.arctan2(local_x, local_z))) + padding_rad
    )
    vertical_half_angle = float(
        np.max(np.abs(np.arctan2(local_y, local_z))) + padding_rad
    )
    if max(horizontal_half_angle, vertical_half_angle) >= math.radians(89.9):
        raise ValueError("Overview rays plus padding approach the perspective horizon")

    # Couple the two FOVs through the requested pixel aspect ratio.  This is
    # equivalent to choosing the smallest common focal length that contains
    # both angular extents without stretching one image axis.
    focal_scale = max(
        math.tan(horizontal_half_angle) / aspect,
        math.tan(vertical_half_angle),
        math.tan(math.radians(1e-4)),
    )
    vfov_deg = math.degrees(2.0 * math.atan(focal_scale))
    hfov_deg = math.degrees(2.0 * math.atan(aspect * focal_scale))
    if hfov_deg > max_fov + 1e-9 or vfov_deg > max_fov + 1e-9:
        raise ValueError(
            "Overview rays require hfov/vfov "
            f"{hfov_deg:.3f}/{vfov_deg:.3f} deg, exceeding max_fov_deg={max_fov:.3f}"
        )
    return forward, right, up, float(hfov_deg), float(vfov_deg)


def _perspective_focal_lengths(
    image_width: int,
    image_height: int,
    hfov_deg: float,
    vfov_deg: float,
) -> tuple[float, float]:
    hfov_rad = math.radians(hfov_deg)
    vfov_rad = math.radians(vfov_deg)
    fx = (image_width * 0.5) / math.tan(hfov_rad * 0.5)
    fy = (image_height * 0.5) / math.tan(vfov_rad * 0.5)
    return fx, fy


def project_points_equirectangular(
    points_xyz: np.ndarray,
    origin_xyz: np.ndarray,
    forward_vec: np.ndarray,
    right_vec: np.ndarray,
    up_vec: np.ndarray,
    image_width: int,
    image_height: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Project world points onto a 360 equirectangular panorama."""
    vectors = points_xyz - origin_xyz[None, :]
    distances = np.linalg.norm(vectors, axis=1)
    valid = distances > 0
    safe_vectors = np.zeros_like(vectors, dtype=np.float64)
    safe_vectors[valid] = vectors[valid] / distances[valid, None]

    local_x = safe_vectors @ right_vec
    local_y = np.clip(safe_vectors @ up_vec, -1.0, 1.0)
    local_z = safe_vectors @ forward_vec

    lon = np.arctan2(local_x, local_z)
    lat = np.arcsin(local_y)

    u = ((lon / (2.0 * math.pi)) + 0.5) * image_width
    v = (0.5 - (lat / math.pi)) * image_height
    return u, v, distances


def project_points_perspective(
    points_xyz: np.ndarray,
    origin_xyz: np.ndarray,
    forward_vec: np.ndarray,
    right_vec: np.ndarray,
    up_vec: np.ndarray,
    image_width: int,
    image_height: int,
    hfov_deg: float,
    vfov_deg: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Project world points onto a rectified perspective image."""
    vectors = points_xyz - origin_xyz[None, :]
    distances = np.linalg.norm(vectors, axis=1)
    local_x = vectors @ right_vec
    local_y = vectors @ up_vec
    local_z = vectors @ forward_vec

    fx, fy = _perspective_focal_lengths(image_width, image_height, hfov_deg, vfov_deg)
    u = np.full(points_xyz.shape[0], np.nan, dtype=np.float64)
    v = np.full(points_xyz.shape[0], np.nan, dtype=np.float64)

    valid = local_z > 1e-6
    u[valid] = (image_width * 0.5) + (fx * (local_x[valid] / local_z[valid]))
    v[valid] = (image_height * 0.5) - (fy * (local_y[valid] / local_z[valid]))
    return u, v, distances, valid


def pixel_to_world_ray(
    pixel_x: float,
    pixel_y: float,
    image_width: int,
    image_height: int,
    forward_vec: np.ndarray,
    right_vec: np.ndarray,
    up_vec: np.ndarray,
) -> np.ndarray:
    """Convert a panorama pixel location to a world-space unit ray."""
    lon = ((pixel_x / image_width) - 0.5) * 2.0 * math.pi
    lat = (0.5 - (pixel_y / image_height)) * math.pi

    cos_lat = math.cos(lat)
    local = (
        forward_vec * (cos_lat * math.cos(lon))
        + right_vec * (cos_lat * math.sin(lon))
        + up_vec * math.sin(lat)
    )
    return normalize(local)


def world_ray_to_equirectangular_pixel(
    ray_world: np.ndarray,
    forward_vec: np.ndarray,
    right_vec: np.ndarray,
    up_vec: np.ndarray,
    image_width: int,
    image_height: int,
) -> tuple[float, float]:
    """Project a world-space unit ray onto a 360 equirectangular panorama pixel."""
    ray_world = normalize(np.asarray(ray_world, dtype=np.float64))
    local_x = float(np.dot(ray_world, right_vec))
    local_y = float(np.clip(np.dot(ray_world, up_vec), -1.0, 1.0))
    local_z = float(np.dot(ray_world, forward_vec))

    lon = math.atan2(local_x, local_z)
    lat = math.asin(local_y)
    u = ((lon / (2.0 * math.pi)) + 0.5) * image_width
    v = (0.5 - (lat / math.pi)) * image_height
    return u, v


def world_ray_to_perspective_pixel(
    ray_world: np.ndarray,
    forward_vec: np.ndarray,
    right_vec: np.ndarray,
    up_vec: np.ndarray,
    image_width: int,
    image_height: int,
    hfov_deg: float,
    vfov_deg: float,
) -> tuple[float, float, float]:
    """Project a world-space unit ray onto a rectified perspective image."""
    ray_world = normalize(np.asarray(ray_world, dtype=np.float64))
    local_x = float(np.dot(ray_world, right_vec))
    local_y = float(np.dot(ray_world, up_vec))
    local_z = float(np.dot(ray_world, forward_vec))
    if local_z <= 1e-6:
        return float("nan"), float("nan"), local_z

    fx, fy = _perspective_focal_lengths(image_width, image_height, hfov_deg, vfov_deg)
    u = (image_width * 0.5) + (fx * (local_x / local_z))
    v = (image_height * 0.5) - (fy * (local_y / local_z))
    return u, v, local_z


def perspective_pixel_to_world_ray(
    pixel_x: float,
    pixel_y: float,
    image_width: int,
    image_height: int,
    forward_vec: np.ndarray,
    right_vec: np.ndarray,
    up_vec: np.ndarray,
    hfov_deg: float,
    vfov_deg: float,
) -> np.ndarray:
    """Convert a rectified perspective pixel to a world-space unit ray."""
    fx, fy = _perspective_focal_lengths(image_width, image_height, hfov_deg, vfov_deg)
    local_x = (pixel_x - (image_width * 0.5)) / fx
    local_y = ((image_height * 0.5) - pixel_y) / fy
    ray = forward_vec + (right_vec * local_x) + (up_vec * local_y)
    return normalize(ray)


def render_perspective_view_from_panorama(
    image_rgb: np.ndarray,
    pano_forward_vec: np.ndarray,
    pano_right_vec: np.ndarray,
    pano_up_vec: np.ndarray,
    view_forward_vec: np.ndarray,
    view_right_vec: np.ndarray,
    view_up_vec: np.ndarray,
    output_width: int,
    output_height: int,
    hfov_deg: float,
    vfov_deg: float,
) -> np.ndarray:
    """Render a rectified perspective image from a 360 equirectangular panorama."""
    import cv2

    x_coords = np.arange(output_width, dtype=np.float64)
    y_coords = np.arange(output_height, dtype=np.float64)
    grid_x, grid_y = np.meshgrid(x_coords, y_coords)

    fx, fy = _perspective_focal_lengths(output_width, output_height, hfov_deg, vfov_deg)
    local_x = (grid_x - (output_width * 0.5)) / fx
    local_y = ((output_height * 0.5) - grid_y) / fy

    rays = (
        view_forward_vec[None, None, :]
        + (view_right_vec[None, None, :] * local_x[:, :, None])
        + (view_up_vec[None, None, :] * local_y[:, :, None])
    )
    ray_norms = np.linalg.norm(rays, axis=2, keepdims=True)
    rays /= np.clip(ray_norms, 1e-12, None)

    local_pano_x = rays @ pano_right_vec
    local_pano_y = np.clip(rays @ pano_up_vec, -1.0, 1.0)
    local_pano_z = rays @ pano_forward_vec

    lon = np.arctan2(local_pano_x, local_pano_z)
    lat = np.arcsin(local_pano_y)

    source_width = image_rgb.shape[1]
    source_height = image_rgb.shape[0]
    pano_u = (((lon / (2.0 * math.pi)) + 0.5) * source_width).astype(np.float32)
    pano_v = ((0.5 - (lat / math.pi)) * source_height).astype(np.float32)

    return cv2.remap(
        image_rgb,
        pano_u,
        pano_v,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_WRAP,
    )


def angular_radius_from_bbox(
    bbox_xyxy: tuple[float, float, float, float],
    image_width: int,
    image_height: int,
    forward_vec: np.ndarray,
    right_vec: np.ndarray,
    up_vec: np.ndarray,
) -> tuple[np.ndarray, float]:
    x1, y1, x2, y2 = bbox_xyxy
    center_x = (x1 + x2) * 0.5
    center_y = (y1 + y2) * 0.5

    center_ray = pixel_to_world_ray(
        center_x,
        center_y,
        image_width,
        image_height,
        forward_vec,
        right_vec,
        up_vec,
    )

    corner_rays = []
    for px, py in (
        (x1, y1),
        (x1, y2),
        (x2, y1),
        (x2, y2),
        (center_x, y1),
        (center_x, y2),
        (x1, center_y),
        (x2, center_y),
    ):
        corner_rays.append(
            pixel_to_world_ray(
                px,
                py,
                image_width,
                image_height,
                forward_vec,
                right_vec,
                up_vec,
            )
        )

    max_angle = 0.0
    for ray in corner_rays:
        dot_value = float(np.clip(np.dot(center_ray, ray), -1.0, 1.0))
        max_angle = max(max_angle, math.acos(dot_value))
    return center_ray, max_angle
