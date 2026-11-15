"""Local visual evidence inventory, independent of parser/model packages."""

import hashlib
from pathlib import Path


def register_asset(assets, asset_id, path, image_root, page, bbox=None):
    path, root = Path(path).resolve(), Path(image_root).resolve()
    if not path.is_relative_to(root):
        raise ValueError(
            "Asset must be inside the extraction images directory"
        )
    record = {
        "asset_id": asset_id,
        "path": "images/" + path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "byte_size": path.stat().st_size,
        "page": page,
        "bbox": bbox,
        "coordinate_system": "pdf_points_top_left" if bbox else None,
    }
    assets[asset_id] = record
    return record


def verify_assets(root, assets):
    root = Path(root).resolve()
    for asset in assets:
        path = (root / asset["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Asset path escapes its chunk directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
            raise ValueError(f"Asset checksum mismatch: {path}")
