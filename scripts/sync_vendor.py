"""Synchronise the Home Assistant integration's vendored copy of ``hvsl``/``pipeline``.

The integration must be installable through HACS or by copying
``custom_components/home_voice_safety/`` alone, so it cannot rely on a
``pip install hvsl``.  We therefore keep a byte-for-byte copy of the two source
packages under ``vendor/`` and let ``__init__.py`` put that directory on
``sys.path``.

Usage::

    python scripts/sync_vendor.py           # copy source -> vendor and rewrite MANIFEST.sha256
    python scripts/sync_vendor.py --check   # verify only; exit 1 when the vendor copy has drifted

The manifest is sorted and uses ``<sha256>  <relative/posix/path>`` lines so it
can be compared across Windows/Linux checkouts.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "home_voice_safety"
VENDOR = INTEGRATION / "vendor"
MANIFEST = VENDOR / "MANIFEST.sha256"
SOURCE_PACKAGES = ("hvsl", "pipeline")

#: Never vendor build/cache artefacts or local environments.
IGNORE_DIRS = {
    "__pycache__",
    ".venv",
    "venv",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
IGNORE_SUFFIXES = {".pyc", ".pyo"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_package_files(package: str) -> list[Path]:
    """Return every source file that belongs in the vendor copy, sorted."""
    root = ROOT / package
    if not root.is_dir():
        raise SystemExit(f"source package missing: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORE_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix.lower() in IGNORE_SUFFIXES:
            continue
        files.append(path)
    return files


def _expected_files() -> dict[str, tuple[Path, str]]:
    """Map ``hvsl/...`` / ``pipeline/...`` relative paths to (source, sha256)."""
    expected: dict[str, tuple[Path, str]] = {}
    for package in SOURCE_PACKAGES:
        for source in _iter_package_files(package):
            relative = source.relative_to(ROOT).as_posix()
            expected[relative] = (source, _sha256(source))
    return expected


def _manifest_text(expected: dict[str, tuple[Path, str]]) -> str:
    return "".join(f"{digest}  {relative}\n" for relative, (_, digest) in sorted(expected.items()))


def _prune_unexpected(expected: dict[str, tuple[Path, str]]) -> list[str]:
    """Delete files under vendor/ that no longer exist in the source packages."""
    removed: list[str] = []
    for package in SOURCE_PACKAGES:
        package_dir = VENDOR / package
        if not package_dir.is_dir():
            continue
        for path in sorted(package_dir.rglob("*"), reverse=True):
            if path.is_file():
                relative = path.relative_to(VENDOR).as_posix()
                if relative not in expected:
                    path.unlink()
                    removed.append(relative)
            elif path.is_dir():
                try:
                    path.rmdir()  # only succeeds when empty
                except OSError:
                    pass
    return removed


def sync() -> int:
    expected = _expected_files()
    VENDOR.mkdir(parents=True, exist_ok=True)
    for relative, (source, _) in expected.items():
        destination = VENDOR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    removed = _prune_unexpected(expected)
    MANIFEST.write_text(_manifest_text(expected), encoding="utf-8", newline="\n")

    print(f"synced {len(expected)} files into {VENDOR}")
    if removed:
        print(f"removed {len(removed)} stale vendor files: {', '.join(removed)}")
    print(f"manifest: {MANIFEST} ({len(expected)} entries)")
    return 0


def _is_runtime_artefact(relative: str) -> bool:
    """True for caches created by importing the vendored packages at runtime."""
    parts = Path(relative).parts
    return any(part in IGNORE_DIRS for part in parts) or Path(relative).suffix.lower() in IGNORE_SUFFIXES


def _actual_vendor_files() -> dict[str, Path]:
    actual: dict[str, Path] = {}
    for package in SOURCE_PACKAGES:
        package_dir = VENDOR / package
        if not package_dir.is_dir():
            continue
        for path in package_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(VENDOR).as_posix()
            if _is_runtime_artefact(relative):
                # HA creates __pycache__ on first import; that is not source drift.
                continue
            actual[relative] = path
    return actual


def check() -> int:
    if not VENDOR.is_dir():
        print(f"vendor directory missing: {VENDOR}", file=sys.stderr)
        print("run: python scripts/sync_vendor.py", file=sys.stderr)
        return 1

    expected = _expected_files()
    actual = _actual_vendor_files()
    problems: list[str] = []

    for relative, (_, digest) in sorted(expected.items()):
        path = actual.get(relative)
        if path is None:
            problems.append(f"missing: {relative}")
            continue
        actual_digest = _sha256(path)
        if actual_digest != digest:
            problems.append(f"changed: {relative} (source {digest[:12]} != vendor {actual_digest[:12]})")

    for relative in sorted(set(actual) - set(expected)):
        problems.append(f"unexpected: {relative}")

    expected_manifest = _manifest_text(expected)
    if not MANIFEST.is_file():
        problems.append(f"missing manifest: {MANIFEST.relative_to(ROOT).as_posix()}")
    else:
        actual_manifest = MANIFEST.read_text(encoding="utf-8")
        if actual_manifest != expected_manifest:
            problems.append("manifest does not match source-derived hashes (rewrite with sync)")

    if problems:
        print(f"vendor check FAILED ({len(problems)} issue(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("run: python scripts/sync_vendor.py", file=sys.stderr)
        return 1

    print(f"vendor check OK: {len(expected)} files match source and MANIFEST.sha256")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the vendor copy without writing anything; exit 1 on drift",
    )
    args = parser.parse_args(argv)
    return check() if args.check else sync()


if __name__ == "__main__":
    raise SystemExit(main())