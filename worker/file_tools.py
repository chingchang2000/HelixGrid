#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tarfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

CHUNK = 1024 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def files(root: Path):
    root = root.resolve()
    items = []
    errors = []

    def onerror(exc: OSError):
        errors.append(str(exc))

    for directory, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
        base = Path(directory)
        dirnames[:] = [name for name in dirnames if not (base / name).is_symlink()]
        for name in filenames:
            path = base / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                items.append(
                    {
                        "path": path,
                        "relative": path.relative_to(root).as_posix(),
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "ext": path.suffix.lower() or "<none>",
                    }
                )
            except OSError as exc:
                errors.append(f"{path}: {exc}")
    items.sort(key=lambda x: x["relative"])
    return items, errors


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def inventory(args):
    items, errors = files(Path(args.root))
    total = sum(int(x["size"]) for x in items)
    ext_count = Counter(x["ext"] for x in items)
    ext_bytes = Counter()
    for item in items:
        ext_bytes[item["ext"]] += int(item["size"])

    largest = sorted(items, key=lambda x: (-int(x["size"]), x["relative"]))[:100]
    save(
        Path(args.output),
        {
            "generated_at": now(),
            "file_count": len(items),
            "total_bytes": total,
            "total_human": human(total),
            "errors": errors,
            "largest_files": [
                {"path": x["relative"], "bytes": x["size"], "human": human(int(x["size"]))}
                for x in largest
            ],
            "extensions": [
                {
                    "extension": ext,
                    "files": ext_count[ext],
                    "bytes": ext_bytes[ext],
                    "human": human(ext_bytes[ext]),
                }
                for ext in sorted(ext_count)
            ],
        },
    )
    print(f"inventory: {len(items)} files, {human(total)}")
    return 0


def checksums(args):
    items, errors = files(Path(args.root))
    rows = []
    started = time.monotonic()
    for index, item in enumerate(items, 1):
        try:
            rows.append(
                {
                    "path": item["relative"],
                    "bytes": item["size"],
                    "sha256": sha256(item["path"]),
                    "mtime_ns": item["mtime_ns"],
                }
            )
        except OSError as exc:
            errors.append(f"{item['path']}: {exc}")
        if index % 250 == 0:
            print(f"checksums: {index}/{len(items)}")

    save(
        Path(args.output),
        {
            "generated_at": now(),
            "file_count": len(rows),
            "errors": errors,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "files": rows,
        },
    )
    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("path", "bytes", "sha256", "mtime_ns"))
        writer.writeheader()
        writer.writerows(rows)
    print(f"checksums: {len(rows)} files")
    return 0


def duplicates(args):
    items, errors = files(Path(args.root))
    by_size = defaultdict(list)
    for item in items:
        by_size[int(item["size"])].append(item)

    groups = []
    for size, candidates in by_size.items():
        if len(candidates) < 2:
            continue
        by_hash = defaultdict(list)
        for item in candidates:
            try:
                by_hash[sha256(item["path"])].append(item)
            except OSError as exc:
                errors.append(f"{item['path']}: {exc}")
        for digest, matching in by_hash.items():
            if len(matching) < 2:
                continue
            groups.append(
                {
                    "sha256": digest,
                    "copies": len(matching),
                    "bytes_each": size,
                    "human_each": human(size),
                    "reclaimable_bytes": size * (len(matching) - 1),
                    "paths": [x["relative"] for x in matching],
                }
            )

    groups.sort(key=lambda g: -int(g["reclaimable_bytes"]))
    reclaimable = sum(int(g["reclaimable_bytes"]) for g in groups)
    save(
        Path(args.output),
        {
            "generated_at": now(),
            "groups": len(groups),
            "reclaimable_bytes": reclaimable,
            "reclaimable_human": human(reclaimable),
            "errors": errors,
            "duplicates": groups,
        },
    )
    print(f"duplicates: {len(groups)} groups, {human(reclaimable)} reclaimable")
    return 0


def backup(args):
    root = Path(args.root).resolve()
    items, errors = files(root)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    with tarfile.open(output, "w:gz", compresslevel=6) as archive:
        for index, item in enumerate(items, 1):
            try:
                archive.add(item["path"], arcname=item["relative"], recursive=False)
            except OSError as exc:
                errors.append(f"{item['path']}: {exc}")
            if index % 250 == 0:
                print(f"backup: {index}/{len(items)}")

    archive_size = output.stat().st_size
    save(
        Path(args.metadata),
        {
            "generated_at": now(),
            "file_count": len(items),
            "archive": str(output),
            "archive_bytes": archive_size,
            "archive_human": human(archive_size),
            "sha256": sha256(output),
            "errors": errors,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        },
    )
    print(f"backup: {len(items)} files -> {output}")
    return 0


def summary(args):
    inv = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    dup = json.loads(Path(args.duplicates).read_text(encoding="utf-8"))
    chk = json.loads(Path(args.checksums).read_text(encoding="utf-8"))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "HelixGrid File Audit",
        "====================",
        f"Files scanned: {inv.get('file_count', 0)}",
        f"Total size: {inv.get('total_human', '?')}",
        f"Checksum records: {chk.get('file_count', 0)}",
        f"Duplicate groups: {dup.get('groups', 0)}",
        f"Potential space in duplicates: {dup.get('reclaimable_human', '0 B')}",
        "",
        "Largest files:",
    ]
    for item in inv.get("largest_files", [])[:20]:
        lines.append(f"  {item.get('human', '?'):>12}  {item.get('path', '?')}")
    lines += ["", "Largest duplicate groups:"]
    for group in dup.get("duplicates", [])[:20]:
        first = (group.get("paths") or ["?"])[0]
        lines.append(
            f"  {group.get('copies', 0)} copies x {group.get('human_each', '?')} - {first}"
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output.read_text(encoding="utf-8"))
    return 0


def parser():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("inventory")
    x.add_argument("--root", default="/workspace")
    x.add_argument("--output", default="/results/inventory.json")
    x.set_defaults(func=inventory)

    x = sub.add_parser("checksums")
    x.add_argument("--root", default="/workspace")
    x.add_argument("--output", default="/results/checksums.json")
    x.add_argument("--csv", default="/results/checksums.csv")
    x.set_defaults(func=checksums)

    x = sub.add_parser("duplicates")
    x.add_argument("--root", default="/workspace")
    x.add_argument("--output", default="/results/duplicates.json")
    x.set_defaults(func=duplicates)

    x = sub.add_parser("backup")
    x.add_argument("--root", default="/workspace")
    x.add_argument("--output", default="/results/backup.tar.gz")
    x.add_argument("--metadata", default="/results/backup.json")
    x.set_defaults(func=backup)

    x = sub.add_parser("summary")
    x.add_argument("--inventory", default="/results/inventory.json")
    x.add_argument("--duplicates", default="/results/duplicates.json")
    x.add_argument("--checksums", default="/results/checksums.json")
    x.add_argument("--output", default="/results/summary.txt")
    x.set_defaults(func=summary)
    return p


if __name__ == "__main__":
    raise SystemExit(parser().parse_args().func(parser().parse_args()))
