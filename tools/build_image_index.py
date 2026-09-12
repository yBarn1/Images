"""Build index.json and .thumbs/ in every folder of this repository that holds images.

The web gallery (pjt-Y/docker-fastapi) and the WordPress github-image-gallery plugin read these two
artefacts per folder: one static file from raw.githubusercontent.com instead of api.github.com calls,
and 480px WebP thumbnails instead of the originals. Folders are walked recursively; dot-folders
(.thumbs, .github, .git) are never indexed.

    python tools/build_image_index.py                # every folder below the repository root
    python tools/build_image_index.py -folder halloween

index.json shape (per folder):
    {"generated", "folder", "thumb_dir", "thumb_width", "count",
     "items": [{"n": name, "s": bytes, "h1": sha1, "d": last commit epoch, "e": EXIF taken epoch,
                "w": width, "ht": height, "t": ".thumbs/<name>.webp"}]}

author: yRocket

changelog:
    0.1.0.2026.9.12: recursive version of ykim2718/WordPress tools/build_image_index.py
"""
__version__ = "0.1.0.2026.9.12"  # Semantic Versioning: Major.Minor.Patch.Date(YYYY.M.D)

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from typing import List

from PIL import Image
from tqdm import tqdm

SOURCE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"}
SKIP_EXT = {".svg"}          # listed in the index, but no raster thumbnail
THUMB_DIR = ".thumbs"
EXIF_TAKEN = 36867           # DateTimeOriginal


def repo_root() -> pathlib.Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
    return pathlib.Path(out.stdout.strip())


def commit_epoch(path: pathlib.Path, root: pathlib.Path) -> int:
    """Unix time of the newest commit that touched the file, 0 if git knows no commit for it yet."""
    rel = path.relative_to(root).as_posix()
    out = subprocess.run(["git", "log", "-1", "--format=%ct", "--", rel], capture_output=True, text=True, cwd=root)
    raw = out.stdout.strip()
    return int(raw) if raw.isdigit() else 0


def exif_epoch(im: Image.Image) -> int:
    taken = (im.getexif() or {}).get(EXIF_TAKEN)
    if not taken:
        return 0
    try:
        return int(datetime.strptime(str(taken), "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0  # cameras write odd strings here; a missing taken-date is not worth failing the build


def sha1_of(path: pathlib.Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def image_folders(root: pathlib.Path, start: pathlib.Path) -> List[pathlib.Path]:
    """Every folder at or below `start` that directly holds at least one image, dot-folders excluded."""
    found = []
    for folder in sorted([start, *start.rglob("*")], key=lambda p: p.as_posix().lower()):
        if not folder.is_dir():
            continue
        if any(part.startswith(".") for part in folder.relative_to(root).parts):
            continue
        if any(p.is_file() and p.suffix.lower() in SOURCE_EXT | SKIP_EXT for p in folder.iterdir()):
            found.append(folder)
    return found


def build(folder: pathlib.Path, root: pathlib.Path, width: int, quality: int) -> dict:
    """Write folder/index.json and folder/.thumbs/*.webp; reuse thumbnails whose source is unchanged."""
    thumbs = folder / THUMB_DIR
    thumbs.mkdir(exist_ok=True)
    index_path = folder / "index.json"
    previous = {}
    if index_path.exists():
        try:
            previous = {it["n"]: it for it in json.loads(index_path.read_text(encoding="utf-8")).get("items", [])}
        except (ValueError, KeyError, TypeError) as exc:
            print(f"{index_path}: unreadable, rebuilding from scratch ({exc})", file=sys.stderr)

    sources = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SOURCE_EXT | SKIP_EXT),
                     key=lambda p: p.name.lower())
    items, kept, made, wanted = [], 0, 0, set()
    pbar = tqdm(sources, ncols=100, unit="image", leave=False)
    for src in pbar:
        pbar.set_description(f"{folder.relative_to(root).as_posix() or '.'}/{src.stem[:24]}")
        digest = sha1_of(src)
        entry = {"n": src.name, "s": src.stat().st_size, "h1": digest, "d": commit_epoch(src, root)}
        if src.suffix.lower() in SKIP_EXT:
            entry.update({"w": 0, "ht": 0, "t": "", "e": 0})
            items.append(entry)
            continue

        # keep the extension in the thumbnail name: foo.jpg and foo.webp are two pictures
        thumb = thumbs / (src.name + ".webp")
        wanted.add(thumb.name)
        old = previous.get(src.name)
        if old and old.get("h1") == digest and thumb.exists():
            entry.update({"w": old.get("w", 0), "ht": old.get("ht", 0),
                          "t": f"{THUMB_DIR}/{thumb.name}", "e": old.get("e", 0)})
            kept += 1
        else:
            with Image.open(src) as im:
                entry["w"], entry["ht"] = im.size
                entry["e"] = exif_epoch(im)
                im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
                im.thumbnail((width, width * 4), Image.LANCZOS)
                im.save(thumb, "WEBP", quality=quality, method=6)
            entry["t"] = f"{THUMB_DIR}/{thumb.name}"
            made += 1
        items.append(entry)

    removed = 0
    for stale in thumbs.glob("*.webp"):        # thumbnails whose source is gone
        if stale.name not in wanted:
            stale.unlink()
            removed += 1

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "folder": folder.relative_to(root).as_posix(),
        "thumb_dir": THUMB_DIR,
        "thumb_width": width,
        "count": len(items),
        "items": items,
    }
    index_path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{payload['folder'] or '.'}: {len(items)} images | thumbnails {made} new, {kept} reused, {removed} pruned")
    return payload


def prune_orphans(root: pathlib.Path, start: pathlib.Path, live: List[pathlib.Path]) -> None:
    """A folder whose last image was deleted still carries index.json and .thumbs/: remove them,
    or the gallery would keep listing pictures that are gone."""
    for index_path in start.rglob("index.json"):
        folder = index_path.parent
        if folder in live or any(part.startswith(".") for part in folder.relative_to(root).parts):
            continue
        index_path.unlink()
        thumbs = folder / THUMB_DIR
        if thumbs.is_dir():
            for stale in thumbs.glob("*.webp"):
                stale.unlink()
            thumbs.rmdir()
        print(f"{folder.relative_to(root).as_posix()}: no images left, removed index.json and {THUMB_DIR}/")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-folder", default="", help="folder (relative to the repository root) to start from; "
                                                    "default: the root, so every folder is indexed")
    parser.add_argument("-width", type=int, default=480, help="thumbnail width in px (default 480)")
    parser.add_argument("-quality", type=int, default=78, help="WebP quality (default 78)")
    args = parser.parse_args()
    if args.width < 16:
        parser.error(f"-width must be at least 16, got {args.width}")
    if not 1 <= args.quality <= 100:
        parser.error(f"-quality must be 1..100, got {args.quality}")
    return args


if __name__ == "__main__":
    args = parse_args()
    root = repo_root()
    start = (root / args.folder).resolve() if args.folder else root
    if not start.is_dir():
        raise SystemExit(f"no such folder: {start}")
    folders = image_folders(root=root, start=start)
    if not folders:
        raise SystemExit(f"no images anywhere below {start.relative_to(root).as_posix() or '.'}")
    for folder in folders:
        build(folder=folder, root=root, width=args.width, quality=args.quality)
    prune_orphans(root=root, start=start, live=folders)
