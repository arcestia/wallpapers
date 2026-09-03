"""Reclassification engine to re-evaluate existing wallpapers without changing permanent IDs."""

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .classifier import classify_image
from .config import DB_PATH, WALLPAPERS_DIR
from .db import db_session, get_all_wallpapers
from .storage import get_target_path, prune_empty_folders


def reclassify_archive(
    db_path: Path = DB_PATH,
    wallpapers_dir: Path = WALLPAPERS_DIR,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Re-evaluate all wallpapers in the database using the updated classifier.
    Reorganizes filesystem paths directly into category folders while preserving permanent sequential database IDs.
    """
    wallpapers = get_all_wallpapers(db_path=db_path)
    summary = {
        "total": len(wallpapers),
        "unchanged": 0,
        "type_updated": 0,
        "category_updated": 0,
        "both_updated": 0,
        "missing_on_disk": 0,
        "changes": [],
    }

    with db_session(db_path=db_path) as conn:
        cursor = conn.cursor()

        for w in wallpapers:
            w_id = w["id"]
            old_type = w["type"]
            old_category = w["category"]
            filename = w["filename"]
            source = w["source"]
            source_url = w["source_url"]

            # Check flat category path first, then legacy nested path
            flat_path = wallpapers_dir / old_category / filename
            nested_path = wallpapers_dir / old_type / old_category / filename

            if flat_path.exists():
                current_path = flat_path
            elif nested_path.exists():
                current_path = nested_path
            else:
                summary["missing_on_disk"] += 1
                continue

            orig_filename = w["original_filename"] or ""

            # Run new classifier on the file
            res = classify_image(
                file_path=current_path,
                source=f"{source or ''} {orig_filename}".strip(),
                source_url=source_url,
                tags=[orig_filename] if orig_filename else None,
            )

            new_type = res.type
            new_category = res.category
            new_conf = res.ai_confidence

            type_changed = new_type != old_type
            cat_changed = new_category != old_category
            needs_move = (current_path == nested_path) or cat_changed

            if not type_changed and not cat_changed and not needs_move:
                summary["unchanged"] += 1
                continue

            if type_changed and cat_changed:
                summary["both_updated"] += 1
            elif type_changed:
                summary["type_updated"] += 1
            else:
                summary["category_updated"] += 1

            new_path = get_target_path(
                wallpaper_id=w_id,
                category_name=new_category,
                extension=current_path.suffix,
                base_dir=wallpapers_dir,
            )

            change_record = {
                "id": w_id,
                "file": filename,
                "from": str(current_path.relative_to(wallpapers_dir)),
                "to": str(new_path.relative_to(wallpapers_dir)),
                "signal": res.detected_signals,
            }
            summary["changes"].append(change_record)

            if not dry_run:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                if current_path != new_path:
                    shutil.move(str(current_path), str(new_path))

                cursor.execute("""
                    UPDATE wallpapers
                    SET type = ?, category = ?, ai_confidence = ?
                    WHERE id = ?
                """, (new_type, new_category, new_conf, w_id))

    if not dry_run:
        prune_empty_folders(wallpapers_dir)

    return summary
