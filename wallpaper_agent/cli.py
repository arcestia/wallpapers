"""Command Line Interface for the Wallpaper Collection Agent."""

import argparse
import sys
from pathlib import Path

from .auto_collector import AutoCollector, DEFAULT_CONFIG_PATH
from .classifier import classify_image
from .config import CATEGORIES, INCOMING_DIR, TYPES, WALLPAPERS_DIR
from .db import (
    db_session,
    find_visual_duplicates,
    get_all_wallpapers,
    get_stats,
    get_wallpaper_by_id,
    init_db,
)
from .downloaders.wallhaven import WallhavenDownloader
from .migrator import migrate_legacy_collection
from .pipeline import download_image, process_image, process_incoming
from .reclassifier import reclassify_archive
from .storage import ensure_storage_structure, prune_empty_folders


def format_bytes(size: int) -> str:
    """Format bytes to human readable format."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize database and directory hierarchy."""
    init_db()
    ensure_storage_structure()
    print("Database and storage directory initialized successfully.")


def cmd_stats(args: argparse.Namespace) -> None:
    """Display wallpaper collection statistics."""
    init_db()
    stats = get_stats()

    print("\n" + "=" * 50)
    print(" WALLPAPER COLLECTION STATISTICS")
    print("=" * 50)
    print(f"Total Wallpapers (>= 2K): {stats['total']:,}")
    print(f"Total Library Size:       {format_bytes(stats['total_size_bytes'])}")
    if stats["avg_width"] and stats["avg_height"]:
        print(f"Average Resolution:       {stats['avg_width']} x {stats['avg_height']}")

    print("\n--- By Classification Type ---")
    for t in TYPES:
        count = stats["by_type"].get(t, 0)
        pct = (count / stats["total"] * 100) if stats["total"] > 0 else 0
        print(f"  {t:<10}: {count:>5} ({pct:>5.1f}%)")

    print("\n--- By Category ---")
    for cat in CATEGORIES:
        count = stats["by_category"].get(cat, 0)
        if count > 0:
            pct = (count / stats["total"] * 100) if stats["total"] > 0 else 0
            print(f"  {cat:<15}: {count:>5} ({pct:>5.1f}%)")

    print("\n--- By Orientation ---")
    for orient, count in sorted(stats["by_orientation"].items()):
        pct = (count / stats["total"] * 100) if stats["total"] > 0 else 0
        print(f"  {orient:<15}: {count:>5} ({pct:>5.1f}%)")
    print("=" * 50 + "\n")


def cmd_process(args: argparse.Namespace) -> None:
    """Process all files in the incoming directory."""
    incoming_dir = Path(args.incoming) if args.incoming else INCOMING_DIR
    print(f"Processing incoming directory: {incoming_dir}")
    res = process_incoming(incoming_dir=incoming_dir)

    print(f"\nIncoming Batch Results:")
    print(f"  Total scanned: {res['total']}")
    print(f"  Completed:     {res['completed']}")
    print(f"  Rejected (<2K):{res['rejected']}")
    print(f"  Duplicates:    {res['duplicate']}")
    print(f"  Failed:        {res['failed']}")


def cmd_add(args: argparse.Namespace) -> None:
    """Add a single wallpaper file or URL to the library."""
    target = args.target
    is_url = target.startswith("http://") or target.startswith("https://")

    if is_url:
        print(f"Downloading from URL: {target}...")
        try:
            file_path = download_image(target)
            source_url = target
            source = args.source or "Web Download"
        except Exception as e:
            print(f"Download failed: {e}")
            sys.exit(1)
    else:
        file_path = Path(target)
        source_url = None
        source = args.source or "CLI Add"

    result = process_image(
        file_path=file_path,
        source=source,
        source_url=source_url,
        category_hint=args.category,
        type_hint=args.type,
        move=is_url or args.move,
    )

    print(f"Status: {result.status}")
    print(f"Reason: {result.reason}")
    if result.wallpaper_id:
        print(f"Saved:  {result.target_path}")


def cmd_wallhaven(args: argparse.Namespace) -> None:
    """Download and ingest wallpapers from Wallhaven."""
    downloader = WallhavenDownloader(
        api_key=args.apikey,
        delay_seconds=args.delay,
    )

    if args.id:
        print(f"Fetching Wallhaven wallpaper: {args.id}...")
        res = downloader.download_and_ingest_single(
            wallpaper_id=args.id,
            category_hint=args.category,
            type_hint=args.type,
        )
        print(f"Status: {res['status']}")
        print(f"Reason: {res['reason']}")
        if res.get("wallpaper_id"):
            print(f"Saved:  ID {res['wallpaper_id']} ({res['target_path']})")
        return

    print(f"Searching Wallhaven: query='{args.query or '*'}' (sorting={args.sort}, limit={args.limit})...")
    res = downloader.batch_collect(
        query=args.query,
        limit=args.limit,
        categories=args.categories,
        purity=args.purity,
        sorting=args.sort,
        top_range=args.top_range,
        ratios=args.ratios,
        category_hint=args.category,
        type_hint=args.type,
    )

    print("\n" + "=" * 50)
    print(" WALLHAVEN BATCH RESULTS")
    print("=" * 50)
    print(f"Total Scanned:  {res['total_scanned']}")
    print(f"Ingested (2K+): {res['completed']}")
    print(f"Rejected (<2K): {res['rejected']}")
    print(f"Duplicates:     {res['duplicates']}")
    print(f"Failed:         {res['failed']}")
    print("=" * 50 + "\n")


def cmd_auto(args: argparse.Namespace) -> None:
    """Run automated wallpaper collection cycle or continuous daemon."""
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    collector = AutoCollector(
        config_path=config_path,
        api_key=args.apikey,
    )

    if args.run_once:
        summary = collector.run_cycle(limit_override=args.limit)
        print("\n" + "=" * 50)
        print(" AUTOMATED COLLECTION SUMMARY")
        print("=" * 50)
        print(f"Targets Processed: {summary['targets_count']}")
        print(f"Total Scanned:     {summary['total_scanned']}")
        print(f"Ingested (2K+):    {summary['total_ingested']}")
        print(f"Duplicates:        {summary['total_duplicates']}")
        print(f"Under 2K:          {summary['total_rejected']}")
        print(f"Failed:            {summary['total_failed']}")
        print("=" * 50 + "\n")
    else:
        collector.run_continuous(interval_seconds=args.interval, limit_override=args.limit)


def cmd_migrate(args: argparse.Namespace) -> None:
    """Migrate legacy repository wallpaper folders."""
    print("Starting migration of legacy wallpaper folders...")
    summary = migrate_legacy_collection(clean_old=args.clean)

    print("\n" + "=" * 50)
    print(" MIGRATION SUMMARY")
    print("=" * 50)
    print(f"Total Scanned:         {summary['scanned']}")
    print(f"Accepted (2K+):        {summary['completed']}")
    print(f"Rejected (< 2K):       {summary['rejected_under_2k']}")
    print(f"Duplicates (Skipped):  {summary['duplicates']}")
    print(f"Failed:                {summary['failed']}")

    print("\nBreakdown by Legacy Folder:")
    for folder, st in summary["by_folder"].items():
        print(f"  {folder:<20}: Total={st['total']:3} | Accepted={st['completed']:3} | Rejected={st['rejected']:3} | Dups={st['duplicate']:3}")
    print("=" * 50 + "\n")


def cmd_verify(args: argparse.Namespace) -> None:
    """Verify consistency between database and filesystem."""
    init_db()
    prune_empty_folders()
    wallpapers = get_all_wallpapers()
    print(f"Verifying {len(wallpapers)} database records against filesystem...")

    missing_files = []
    for w in wallpapers:
        w_type = w["type"]
        w_cat = w["category"]
        w_filename = w["filename"]
        flat_path = WALLPAPERS_DIR / w_cat / w_filename
        nested_path = WALLPAPERS_DIR / w_type / w_cat / w_filename

        if not flat_path.exists() and not nested_path.exists():
            missing_files.append((w["id"], str(flat_path)))

    if missing_files:
        print(f"WARNING: {len(missing_files)} missing files found on disk:")
        for w_id, path_str in missing_files[:10]:
            print(f"  ID {w_id}: {path_str}")
        if len(missing_files) > 10:
            print(f"  ... and {len(missing_files) - 10} more.")
    else:
        print("All database records match files on disk perfectly.")


def cmd_reclassify(args: argparse.Namespace) -> None:
    """Re-evaluate all existing wallpapers with the updated classifier."""
    init_db()
    mode_str = " (DRY RUN)" if args.dry_run else ""
    print(f"Starting archive reclassification{mode_str}...")
    summary = reclassify_archive(dry_run=args.dry_run)

    print("\n" + "=" * 50)
    print(f" RECLASSIFICATION SUMMARY{mode_str}")
    print("=" * 50)
    print(f"Total Evaluated:     {summary['total']}")
    print(f"Unchanged:           {summary['unchanged']}")
    print(f"Type (AI/Non) Moved: {summary['type_updated']}")
    print(f"Category Moved:      {summary['category_updated']}")
    print(f"Both Moved:          {summary['both_updated']}")
    print(f"Missing on Disk:     {summary['missing_on_disk']}")

    if summary["changes"]:
        print("\nSample Reclassifications (first 10):")
        for ch in summary["changes"][:10]:
            print(f"  ID {ch['id']:<4} | {ch['from']:<20} -> {ch['to']:<20} ({ch['signal']})")
    print("=" * 50 + "\n")


def cmd_search(args: argparse.Namespace) -> None:
    """Search wallpapers by filters."""
    init_db()
    with db_session() as conn:
        cursor = conn.cursor()
        query = "SELECT id, filename, type, category, width, height, aspect_ratio, format FROM wallpapers WHERE 1=1"
        params = []

        if args.type:
            query += " AND type = ?"
            params.append(args.type)
        if args.category:
            query += " AND category = ?"
            params.append(args.category)
        if args.orientation:
            query += " AND orientation = ?"
            params.append(args.orientation)
        if args.min_width:
            query += " AND width >= ?"
            params.append(args.min_width)

        query += " ORDER BY id ASC LIMIT ?"
        params.append(args.limit or 50)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        print(f"\nFound {len(rows)} matching wallpapers:")
        for r in rows:
            print(f"  ID {r['id']:<5} | {r['type']:<8} | {r['category']:<12} | {r['width']}x{r['height']} ({r['aspect_ratio']}) | {r['filename']}")


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="wallpaper-agent",
        description="Automated Wallpaper Collection, Validation, Classification, and Deduplication Agent."
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init
    subparsers.add_parser("init", help="Initialize database and folder structure")

    # stats
    subparsers.add_parser("stats", help="Show wallpaper library statistics")

    # process
    p_proc = subparsers.add_parser("process", help="Process incoming wallpapers")
    p_proc.add_argument("--incoming", "-i", type=str, help="Custom incoming directory")

    # add
    p_add = subparsers.add_parser("add", help="Add a single image or URL")
    p_add.add_argument("target", help="File path or URL of wallpaper")
    p_add.add_argument("--type", "-t", choices=TYPES, help="Classification type override (AI, NON-AI, UNKNOWN)")
    p_add.add_argument("--category", "-c", choices=CATEGORIES, help="Category override")
    p_add.add_argument("--source", "-s", help="Source name or credit")
    p_add.add_argument("--move", "-m", action="store_true", help="Move source file instead of copying")

    # wallhaven
    p_wh = subparsers.add_parser("wallhaven", help="Download wallpapers from Wallhaven.cc")
    p_wh.add_argument("--query", "-q", type=str, help="Search query or tag (e.g. cyberpunk, anime, landscape)")
    p_wh.add_argument("--id", type=str, help="Single Wallhaven wallpaper ID or URL (e.g. 1k7j9w)")
    p_wh.add_argument("--limit", "-n", type=int, default=5, help="Number of wallpapers to download (default: 5)")
    p_wh.add_argument("--sort", "-s", choices=["toplist", "hot", "views", "random", "date_added"], default="toplist", help="Sorting method")
    p_wh.add_argument("--top-range", choices=["1d", "3d", "1w", "1M", "3M", "6M", "1y"], default="1M", help="Toplist time range")
    p_wh.add_argument("--categories", default="111", help="Categories mask: General/Anime/People (e.g. 111 or 010)")
    p_wh.add_argument("--purity", default="100", help="Purity mask: SFW/Sketchy/NSFW (default: 100 SFW)")
    p_wh.add_argument("--ratios", type=str, help="Filter aspect ratios (e.g. 16x9, 21x9)")
    p_wh.add_argument("--category", "-c", choices=CATEGORIES, help="Category override")
    p_wh.add_argument("--type", "-t", choices=TYPES, help="Type override (AI, NON-AI, UNKNOWN)")
    p_wh.add_argument("--apikey", type=str, help="Wallhaven API key")
    p_wh.add_argument("--delay", type=float, default=1.0, help="Delay between downloads in seconds (default: 1.0)")

    # auto
    p_auto = subparsers.add_parser("auto", help="Run automated wallpaper collection daemon or batch cycle")
    p_auto.add_argument("--run-once", action="store_true", help="Run one single collection cycle and exit")
    p_auto.add_argument("--limit", "-n", type=int, help="Override number of wallpapers to collect per target (e.g. 10, 20)")
    p_auto.add_argument("--interval", "-i", type=int, default=3600, help="Continuous loop interval in seconds (default: 3600)")
    p_auto.add_argument("--config", "-c", type=str, help="Path to custom collector_config.json")
    p_auto.add_argument("--apikey", type=str, help="Wallhaven API key")

    # migrate
    p_mig = subparsers.add_parser("migrate", help="Migrate legacy folders into standardized archive")
    p_mig.add_argument("--clean", action="store_true", help="Clean up legacy folders after migration")

    # verify
    subparsers.add_parser("verify", help="Verify database integrity against disk")

    # reclassify
    p_rec = subparsers.add_parser("reclassify", help="Re-evaluate and organize existing wallpapers using updated classifier")
    p_rec.add_argument("--dry-run", action="store_true", help="Preview changes without modifying database or moving files")

    # search
    p_srch = subparsers.add_parser("search", help="Search wallpaper records")
    p_srch.add_argument("--type", "-t", choices=TYPES, help="Filter by type")
    p_srch.add_argument("--category", "-c", choices=CATEGORIES, help="Filter by category")
    p_srch.add_argument("--orientation", "-o", help="Filter by orientation (Landscape, Portrait, Ultrawide, Square)")
    p_srch.add_argument("--min-width", type=int, help="Minimum width in pixels")
    p_srch.add_argument("--limit", "-l", type=int, default=50, help="Maximum results")

    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "init": cmd_init,
        "stats": cmd_stats,
        "process": cmd_process,
        "add": cmd_add,
        "wallhaven": cmd_wallhaven,
        "auto": cmd_auto,
        "migrate": cmd_migrate,
        "verify": cmd_verify,
        "reclassify": cmd_reclassify,
        "search": cmd_search,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
