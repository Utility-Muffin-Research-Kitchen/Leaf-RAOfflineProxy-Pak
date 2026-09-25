"""Byte-stable pak ZIPs, shared by package_mlp1.py and package_floor.py.

Every piece of entry metadata is pinned: sorted names, 1980-01-01
timestamps, Unix entries, and a mode chosen from the file's content rather
than from the host filesystem. The build tree's mode bits are not portable:
through Docker Desktop's macOS bind mount almost every file reads as
executable, while the same tree on a Linux CI runner keeps .py files at
0644, so identical contents still produced different archives.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import stat
import zipfile


def entry_mode(data: bytes) -> int:
    """0755 for ELF binaries and shared objects and #! scripts, else 0644."""
    return 0o755 if data.startswith((b"\x7fELF", b"#!")) else 0o644


def write_zip(package_dir: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.unlink(missing_ok=True)
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as output:
        for path in sorted(package_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = PurePosixPath(package_dir.name) / path.relative_to(package_dir)
            data = path.read_bytes()
            info = zipfile.ZipInfo(str(relative), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | entry_mode(data)) << 16
            info.create_system = 3
            output.writestr(
                info,
                data,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
