#!/usr/bin/env python3
"""Verified code/input backup. No results, weather archive, credentials or venv binaries.

Run on a compute node. A backup is not off-cluster until the resulting archive
AND checksum have been downloaded and checked on an independent computer.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
from datetime import datetime, timezone

SCOPES = ("scripts", "slurm", "config", "templates", "tests", "inputs",
          "source/dismo_dssat", "apps/dssat-dismo-4.8", "releases", "data/dssat48")
SKIP_DIRS = {".git", ".svn", ".ssh", ".aws", ".azure", ".vs", "__pycache__",
             "node_modules", "results", "work", "venvs", "backups", "rehearsal",
             ".pytest_cache", ".mypy_cache", "CMakeFiles", "build", "Build", "x64"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".o", ".obj", ".mod", ".out", ".log", ".pdb"}
SKIP_NAMES = {".env", ".netrc", ".pypirc", "credentials", "id_rsa", "id_ed25519"}
ESSENTIAL = ("source/dismo_dssat/Plant/Generic-Pest/DISMO.for",
             "apps/dssat-dismo-4.8/dscsm048", "inputs/disease/disease_parameters.txt")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def walk_files(directory):
    """Do not recursively dereference symlink directories into shared datasets."""
    for base, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in dirs[:]:
            path = Path(base) / name
            if path.is_symlink():
                yield path
                dirs.remove(name)
        for name in sorted(files):
            path = Path(base) / name
            if name not in SKIP_NAMES and path.suffix.lower() not in SKIP_SUFFIXES:
                yield path


def select_files(root, pythia_package):
    root, pythia_package = Path(root).resolve(), Path(pythia_package).resolve()
    selected = {}
    missing = []
    for scope in SCOPES:
        directory = root / scope
        if not directory.is_dir():
            missing.append(scope)
            continue
        for path in walk_files(directory):
            selected["project/" + path.relative_to(root).as_posix()] = path
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix.lower() in {".md", ".txt", ".json", ".sh"} and path.name not in SKIP_NAMES:
            selected["project/" + path.name] = path
    # Production inputs, selected GMs, manifests and saved job IDs; no smoke/rehearsal output.
    world = root / "generated/world"
    if world.is_dir():
        for path in walk_files(world):
            if path.is_symlink() or path.suffix.lower() in {".json", ".env", ".txt", ".csv", ".gz", ".gpkg", ".tif", ".nc"}:
                selected["project/" + path.relative_to(root).as_posix()] = path
    # Keep actual imported package code, including locally patched Pythia.
    if not (pythia_package / "__init__.py").is_file():
        raise ValueError(f"Pythia package not found: {pythia_package}")
    for path in walk_files(pythia_package):
        selected["environment/pythia/" + path.relative_to(pythia_package).as_posix()] = path
    for name in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "README.md", "LICENSE"):
        path = pythia_package.parent / name
        if path.is_file():
            selected["environment/pythia_repository/" + name] = path
    for relative in ESSENTIAL:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"Essential file missing: {path}")
        # Independent regular-file snapshots even if canonical paths are symlinks.
        selected["critical_files/" + path.name] = path.resolve()
    # Compilation configuration without copying object files or old builds.
    build = root / "build"
    if build.is_dir():
        for directory in build.iterdir():
            if directory.is_dir() and not directory.is_symlink():
                for name in ("CMakeCache.txt", "compile_commands.json"):
                    path = directory / name
                    if path.is_file():
                        selected["project/" + path.relative_to(root).as_posix()] = path
    return selected, missing


def environment_info(python):
    program = (
        "import importlib.util,importlib.metadata,json,sys; "
        "s=importlib.util.find_spec('pythia'); "
        "print(json.dumps({'python':sys.version,'pythia_origin':s.origin if s else None,"
        "'packages':sorted([(d.metadata.get('Name',''),d.version) for d in importlib.metadata.distributions()])}))"
    )
    # Only package names/versions; no pip index credentials, env dump or shell history.
    result = subprocess.run([str(python), "-I", "-c", program], check=True,
                            capture_output=True, text=True)
    info = json.loads(result.stdout)
    if not info["pythia_origin"]:
        raise ValueError("Pythia is not installed in the specified simulation environment")
    return info


def add_json(archive, name, content):
    blob = (json.dumps(content, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(blob), 0o600
    archive.addfile(info, io.BytesIO(blob))


def create_backup(root, output, package, environment):
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    selected, missing = select_files(root, package)
    manifest = []
    partial = output / "sbrust_critical.tar.gz.partial"
    final = output / "sbrust_critical.tar.gz"
    started = datetime.now(timezone.utc).isoformat()
    try:
        with tarfile.open(partial, "w:gz", compresslevel=3) as archive:
            for index, (name, path) in enumerate(sorted(selected.items()), 1):
                info = archive.gettarinfo(str(path), arcname=name)
                # gettarinfo may infer a hardlink for previously archived inode;
                # use a regular copy for explicit critical snapshots.
                if name.startswith("critical_files/") and info.islnk():
                    info.type, info.linkname, info.size = tarfile.REGTYPE, "", path.stat().st_size
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if info.isfile():
                    before = path.stat()
                    digest = sha256(path)
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise RuntimeError(f"File changed during snapshot: {path}; retry in a NEW directory")
                    manifest.append({"archive_path": name, "size": info.size, "sha256": digest})
                else:
                    archive.addfile(info)
                    manifest.append({"archive_path": name, "link_target": info.linkname,
                                     "kind": "symlink" if info.issym() else "hardlink"})
                if index % 1000 == 0:
                    print(f"Archived {index}/{len(selected)} files", flush=True)
            add_json(archive, "backup_manifest.json", manifest)
            add_json(archive, "environment/packages.json", environment)
            add_json(archive, "backup_scope.json", {
                "started_utc": started, "root": str(root), "missing_optional_scopes": missing,
                "scopes": SCOPES, "pythia_source": str(package),
                "not_included": ["results (including production results)", "logs", "scratch/work",
                                 "historical/future WTH", "data/spatial (SPAM, soils, global GGCMI, boundaries)",
                                 "virtual-environment binaries", "Git history", "credentials"],
                "important": "Not a self-contained global-data backup. Symlink targets may remain external. Download archive AND checksum off HiPerGator.",
            })
        print("Verifying every archived regular file...", flush=True)
        with tarfile.open(partial, "r:gz") as archive:
            expected = {r["archive_path"]: r["sha256"] for r in manifest if "sha256" in r}
            for member in archive:
                if member.name not in expected:
                    continue
                digest = hashlib.sha256()
                stream = archive.extractfile(member)
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    digest.update(block)
                if digest.hexdigest() != expected.pop(member.name):
                    raise RuntimeError(f"Archive verification failed: {member.name}")
            if expected:
                raise RuntimeError("Some selected files are missing from archive")
        partial.rename(final)
        checksum = sha256(final)
        (output / "SHA256SUMS.txt").write_text(f"{checksum}  {final.name}\n", encoding="ascii")
        report = {"status": "verified_on_hpg_not_yet_offsite", "files": len(manifest),
                  "archive_bytes": final.stat().st_size, "sha256": checksum,
                  "archive": str(final), "missing_optional_scopes": missing,
                  "symlinks": [r for r in manifest if r.get("kind") == "symlink"]}
        (output / "backup_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
        return report
    except Exception as exc:
        (output / "BACKUP_FAILED.json").write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--python", type=Path, required=True, help="Actual simulation venv/bin/python")
    args = parser.parse_args()
    info = environment_info(args.python)
    create_backup(args.root, args.output, Path(info["pythia_origin"]).parent, info)


if __name__ == "__main__":
    main()
