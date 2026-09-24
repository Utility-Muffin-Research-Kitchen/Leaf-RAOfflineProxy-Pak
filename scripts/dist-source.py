#!/usr/bin/env python3
"""Write the corresponding source for the package this commit builds.

GPL-3.0 obliges whoever conveys the pak to offer the complete corresponding
source: this repository at the commit, every locked input the build consumes,
and the pinned Catastrophe tree the UI is compiled from. The archive holds
exactly those, verified against the locks, laid out so the package rebuilds
from it with no network and no other checkout:

  raofflineproxy-<version>-source/
    Leaf-RAOfflineProxy-Pak/                 this repo at HEAD (git archive)
      workdir/sources/<locked archives>      upstream, CPython, xz, CA, rcheevos, libchdr
    Catastrophe/                             the locked commit (git archive)
    corresponding-source.json                what is inside, with hashes

  cd raofflineproxy-<version>-source/Leaf-RAOfflineProxy-Pak
  make package-mlp1 package-floor-mlp1      # needs Docker and the pinned image

The build toolchain is the pinned mlp1-toolchain image, named by digest in
release-lock.json; it is a build tool, not part of the work, and is not copied
in. The archive itself is deterministic: sorted entries, fixed owner and
modes, and every mtime at the lock's SOURCE_DATE_EPOCH.

Run: python3 scripts/dist-source.py [--catastrophe DIR] [--output PATH]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import pathlib
import subprocess
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_sha256(archive: pathlib.Path, scope: str) -> str:
    """The upstream lock's content hash over the extracted shipped subtree."""
    digest = hashlib.sha256()
    with tarfile.open(archive) as source:
        members = {}
        for member in source.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) == 2 and parts[1].startswith(scope + "/") and member.isfile():
                members[parts[1][len(scope) + 1:]] = member
        for name in sorted(members):
            data = source.extractfile(members[name]).read()
            digest.update(name.encode() + b"\0")
            digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def git(*args: str, cwd: pathlib.Path) -> bytes:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True).stdout


def normalized_upstream(path: pathlib.Path, epoch: int) -> bytes:
    """Keep the complete source, discarding GitHub's tar/gzip metadata."""
    writer = Writer(epoch)
    with tarfile.open(path) as source:
        for member in source.getmembers():
            if member.isdir():
                continue
            parts = pathlib.PurePosixPath(member.name).parts
            if (not member.isfile() or len(parts) < 2 or
                    member.name.startswith("/") or ".." in parts):
                raise SystemExit(f"unsupported upstream source entry: {member.name}")
            writer.add("upstream/" + "/".join(parts[1:]),
                       source.extractfile(member).read(), bool(member.mode & 0o111))
    return writer.bytes()


def locked_inputs() -> list[tuple[dict, bytes]]:
    runtime = json.loads((ROOT / "locks" / "runtime.lock.json").read_text(encoding="utf-8"))
    upstream = json.loads((ROOT / "locks" / "upstream.lock.json").read_text(encoding="utf-8"))
    sources = ROOT / "workdir" / "sources"
    inputs = []
    for item in runtime["source_inputs"]:
        path = sources / item["filename"]
        if not path.is_file() or sha256_file(path) != item["sha256"]:
            raise SystemExit(f"{path} is missing or not the locked archive "
                             "(run scripts/fetch-sources.sh)")
        inputs.append(({"filename": item["filename"], "sha256": item["sha256"],
                        "url": item["url"], "lock": "locks/runtime.lock.json"},
                       path.read_bytes()))
    archive = upstream["archive"]
    path = sources / archive["filename"]
    if not path.is_file():
        raise SystemExit(f"{path} is missing (run scripts/fetch-sources.sh)")
    # GitHub re-encodes commit tarballs, so the byte hash is a receipt; the
    # extracted subtree is what the lock (and assemble-app.sh) gates on.
    if content_sha256(path, archive["content_scope"]) != archive["content_sha256"]:
        raise SystemExit(f"{path} does not carry the locked upstream content")
    data = normalized_upstream(path, int(runtime["source_date_epoch"]))
    inputs.append(({"filename": archive["filename"], "sha256": hashlib.sha256(data).hexdigest(),
                    "content_sha256": archive["content_sha256"],
                    "content_scope": archive["content_scope"], "url": archive["url"],
                    "commit": upstream["commit"], "lock": "locks/upstream.lock.json"}, data))
    return inputs


class Writer:
    def __init__(self, epoch: int) -> None:
        self.epoch = epoch
        self.entries: dict[str, tuple[bytes, int]] = {}

    def add(self, name: str, data: bytes, executable: bool) -> None:
        if name in self.entries:
            raise SystemExit(f"duplicate archive entry {name}")
        self.entries[name] = (data, 0o755 if executable else 0o644)

    def add_git_archive(self, prefix: str, repo: pathlib.Path, commit: str) -> None:
        blob = git("archive", "--format=tar", commit, cwd=repo)
        with tarfile.open(fileobj=io.BytesIO(blob)) as source:
            for member in source.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise SystemExit(f"{repo}@{commit}: {member.name} is not a regular "
                                     "file (FAT32-safe sources only)")
                self.add(f"{prefix}/{member.name}", source.extractfile(member).read(),
                         bool(member.mode & 0o111))

    def bytes(self) -> bytes:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name in sorted(self.entries):
                data, mode = self.entries[name]
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = mode
                info.mtime = self.epoch
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(data))
        compressed = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0,
                           compresslevel=9) as gz:
            gz.write(raw.getvalue())
        return compressed.getvalue()

    def write(self, output: pathlib.Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(self.bytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catastrophe", type=pathlib.Path,
                        default=ROOT.parent / "Catastrophe")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()

    lock = json.loads((ROOT / "release-lock.json").read_text(encoding="utf-8"))
    runtime = json.loads((ROOT / "locks" / "runtime.lock.json").read_text(encoding="utf-8"))
    version = lock["pak_version"]
    epoch = int(runtime["source_date_epoch"])
    output = args.output or ROOT / "build" / "dist" / f"raofflineproxy-{version}-source.tar.gz"

    dirty = git("status", "--porcelain", "--untracked-files=normal", cwd=ROOT).decode().strip()
    if dirty:
        raise SystemExit("the working tree has uncommitted changes; corresponding source "
                         "is cut from a commit, so commit or stash them first")
    head = git("rev-parse", "HEAD", cwd=ROOT).decode().strip()

    subprocess.run([str(ROOT / "scripts" / "verify-catastrophe.sh"), str(args.catastrophe)],
                   check=True, stdout=subprocess.DEVNULL)

    top = f"raofflineproxy-{version}-source"
    writer = Writer(epoch)
    writer.add_git_archive(f"{top}/Leaf-RAOfflineProxy-Pak", ROOT, head)
    inputs = []
    for item, data in locked_inputs():
        inputs.append(item)
        writer.add(f"{top}/Leaf-RAOfflineProxy-Pak/workdir/sources/{item['filename']}",
                   data, False)
    writer.add_git_archive(f"{top}/Catastrophe", args.catastrophe, lock["catastrophe_commit"])

    manifest = {
        "schema": 1,
        "product": lock["product"],
        "pak_version": version,
        "floor_version": lock["floor_version"],
        "commit": head,
        "catastrophe_commit": lock["catastrophe_commit"],
        "catastrophe_tree": lock["catastrophe_tree"],
        "toolchain_image": lock["mlp1_toolchain_image"],
        "source_date_epoch": epoch,
        "inputs": inputs,
        "rebuild": [
            f"cd {top}/Leaf-RAOfflineProxy-Pak",
            "make package-mlp1 package-floor-mlp1",
        ],
    }
    writer.add(f"{top}/corresponding-source.json",
               (json.dumps(manifest, indent=2) + "\n").encode(), False)
    writer.write(output)
    print(f"dist-source={output}")
    print(f"sha256={sha256_file(output)}")


if __name__ == "__main__":
    main()
