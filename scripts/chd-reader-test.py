#!/usr/bin/env python3
"""Host tests for the CHD track reader behind the offline pre-cache hashes.

Two reader fixes were found on the device and, until now, only checked there:

- 619affb: every CHD track is stored padded up to a 4-frame boundary, GD-ROM
  included, while the LBA does not advance by that padding. Sonic Adventure 2
  (Europe) has tracks of 5574 and 39426 frames; its IP.BIN is at CHD frame
  45004 and LBA 45000.
- 324e26b: a pregap is stored in the CHD only when its PGTYPE carries the "V"
  prefix. Castlevania - Rondo of Blood (Translated) declares PREGAP:225
  PGTYPE:MODE1; its header is at frame 3669, sector 1 of a track that starts
  right after track 1's padded 3668 frames. A PC Engine CD whose track 2
  declares PGTYPE:VMODE1_RAW does store its pregap.

Each case is a small synthetic disc written as an uncompressed CHD v5 in pure
Python, hashed through the real exported entry point (raproxy_hash_file, the
one leaf_romhash calls) built natively from the locked rcheevos and libchdr
sources plus src/rchash. The expected hash is computed here from the bytes the
disc was built with, following RetroAchievements' published rules, and the
reader's track table is checked sector by sector.

With --chdman (or when chdman is on PATH) the same discs are also written as
cue/bin and gdi sheets and converted by chdman itself, so the writer's layout
is checked against the reference tool rather than against the reader it tests.

Run: python3 scripts/chd-reader-test.py [--lib PATH] [--chdman PATH]
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import pathlib
import platform
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = pathlib.Path(os.environ.get("RUNTIME_SOURCES_DIR", ROOT / "workdir" / "sources"))

FRAME_DATA = 2352
FRAME_SUB = 96
FRAME = FRAME_DATA + FRAME_SUB
FRAMES_PER_HUNK = 8
HUNK = FRAME * FRAMES_PER_HUNK
PADDING = 4

CONSOLE_DREAMCAST = 40
CONSOLE_PC_ENGINE_CD = 76

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


# --------------------------------------------------------------------------
# Native build of the reader from the locked sources.

def locked_archive(bucket: str) -> pathlib.Path:
    lock = json.loads((ROOT / "locks" / "runtime.lock.json").read_text(encoding="utf-8"))
    item = next(i for i in lock["source_inputs"] if i.get("bucket") == bucket)
    path = SOURCES / item["filename"]
    if not path.is_file():
        sys.exit(f"missing {path} (run scripts/fetch-sources.sh)")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != item["sha256"]:
        sys.exit(f"{bucket} archive hash mismatch: {digest}")
    return path


def build_library(work: pathlib.Path) -> pathlib.Path:
    """Compile the same source set build-rchash.sh does, for this host."""
    rc = work / "rcheevos"
    chd = work / "libchdr"
    for bucket, dest in (("rcheevos", rc), ("libchdr", chd)):
        dest.mkdir(parents=True)
        subprocess.run(["tar", "-xzf", str(locked_archive(bucket)), "-C", str(dest),
                        "--strip-components=1"], check=True)
    lzma = next((chd / "deps").glob("lzma-*"))
    miniz = next((chd / "deps").glob("miniz-*"))
    zstd = next((chd / "deps").glob("zstd-*"))
    sources = [
        ROOT / "src/rchash/rchash_glue.c", ROOT / "src/rchash/chd_cdreader.c",
        *(rc / "src/rhash" / name for name in
          ("hash.c", "hash_rom.c", "hash_zip.c", "hash_disc.c", "cdreader.c", "md5.c")),
        rc / "src/rc_compat.c",
        *sorted((chd / "src").glob("*.c")),
        *sorted((lzma / "src").glob("*.c")),
        *sorted(miniz.glob("*.c")),
        zstd / "zstddeclib.c",
    ]
    suffix = ".dylib" if platform.system() == "Darwin" else ".so"
    out = work / f"libraproxy_rchash_host{suffix}"
    cc = os.environ.get("CC", "cc")
    command = [
        cc, "-shared", "-fPIC", "-O1", "-w", "-DNDEBUG", "-DRC_HASH_NO_ENCRYPTED",
        "-DZSTD_DISABLE_ASM", "-o", str(out), *map(str, sources),
        f"-I{rc / 'include'}", f"-I{rc / 'src'}", f"-I{rc / 'src/rhash'}",
        f"-I{chd / 'include'}", f"-I{lzma / 'include'}", f"-I{miniz}", f"-I{zstd}",
    ]
    subprocess.run(command, check=True)
    return out


def load(lib_path: pathlib.Path) -> ctypes.CDLL:
    lib = ctypes.CDLL(str(lib_path))
    lib.raproxy_hash_file.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p]
    lib.raproxy_hash_file.restype = ctypes.c_int
    lib.raproxy_chd_open_track.argtypes = [ctypes.c_char_p, ctypes.c_uint32]
    lib.raproxy_chd_open_track.restype = ctypes.c_void_p
    lib.raproxy_chd_first_track_sector.argtypes = [ctypes.c_void_p]
    lib.raproxy_chd_first_track_sector.restype = ctypes.c_uint32
    lib.raproxy_chd_read_sector.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                            ctypes.c_void_p, ctypes.c_size_t]
    lib.raproxy_chd_read_sector.restype = ctypes.c_size_t
    lib.raproxy_chd_close_track.argtypes = [ctypes.c_void_p]
    return lib


def rc_hash(lib: ctypes.CDLL, path: pathlib.Path, console: int) -> str | None:
    out = ctypes.create_string_buffer(33)
    if not lib.raproxy_hash_file(str(path).encode(), console, out):
        return None
    return out.value.decode()


def track_view(lib: ctypes.CDLL, path: pathlib.Path, track: int,
               sector_offset: int, size: int) -> tuple[int | None, bytes]:
    handle = lib.raproxy_chd_open_track(str(path).encode(), track)
    if not handle:
        return None, b""
    try:
        first = lib.raproxy_chd_first_track_sector(handle)
        buffer = ctypes.create_string_buffer(size)
        got = lib.raproxy_chd_read_sector(handle, first + sector_offset, buffer, size)
        return first, buffer.raw[:got]
    finally:
        lib.raproxy_chd_close_track(handle)


# --------------------------------------------------------------------------
# Disc content.

def mode1_frame(lba: int, user: bytes) -> bytes:
    """A MODE1/2352 sector: sync, MSF header, 2048 user bytes, zeroed EDC/ECC."""
    assert len(user) <= 2048
    absolute = lba + 150
    msf = bytes(int(str(v), 16) for v in (absolute // 4500, absolute // 75 % 60, absolute % 75))
    sector = b"\x00" + b"\xff" * 10 + b"\x00" + msf + b"\x01" + user.ljust(2048, b"\x00")
    return sector.ljust(FRAME_DATA, b"\x00")


def audio_frame(seed: int) -> bytes:
    return bytes((seed * 7 + i) & 0xFF for i in range(FRAME_DATA))


class Track:
    def __init__(self, number: int, kind: str, sectors: list[bytes],
                 pregap: int = 0, pregap_stored: bool = False) -> None:
        self.number = number
        self.kind = kind            # MODE1_RAW or AUDIO
        self.sectors = sectors      # 2352-byte frames of the track proper
        self.pregap = pregap
        self.pregap_stored = pregap_stored

    @property
    def pgtype(self) -> str:
        base = "MODE1" if self.kind == "MODE1_RAW" else "AUDIO"
        return ("V" + self.kind if self.pregap_stored else base) if self.pregap else base


def layout(tracks: list[Track]) -> list[tuple[int, int]]:
    """(first CHD frame of the track's data, its LBA), chdman's rules.

    A track's frames are stored padded up to a multiple of 4; a pregap is
    stored ahead of the data only when it is a "V" pregap. The LBA advances by
    every pregap and every data frame, never by the padding.
    """
    placed = []
    chd_frame = 0
    lba = 0
    for track in tracks:
        stored = track.pregap if track.pregap_stored else 0
        placed.append((chd_frame + stored, lba + track.pregap))
        lba += track.pregap + len(track.sectors)
        chd_frame += stored + len(track.sectors)
        chd_frame = -(-chd_frame // PADDING) * PADDING
    return placed


def write_chd(path: pathlib.Path, tracks: list[Track], gdrom: bool) -> None:
    frames: list[bytes] = []
    for track in tracks:
        if track.pregap and track.pregap_stored:
            frames += [b"\x00" * FRAME_DATA] * track.pregap
        frames += track.sectors
        while len(frames) % PADDING:
            frames.append(b"\x00" * FRAME_DATA)
    body = b"".join(frame + b"\x00" * FRAME_SUB for frame in frames)
    hunks = [body[i:i + HUNK].ljust(HUNK, b"\x00") for i in range(0, len(body), HUNK)]

    metadata = []
    for track in tracks:
        if gdrom:
            text = (f"TRACK:{track.number} TYPE:{track.kind} SUBTYPE:NONE "
                    f"FRAMES:{len(track.sectors)} PAD:0 PREGAP:{track.pregap} "
                    f"PGTYPE:{track.pgtype} PGSUB:NONE POSTGAP:0")
            tag = b"CHGD"
        else:
            text = (f"TRACK:{track.number} TYPE:{track.kind} SUBTYPE:NONE "
                    f"FRAMES:{len(track.sectors)} PREGAP:{track.pregap} "
                    f"PGTYPE:{track.pgtype} PGSUB:NONE POSTGAP:0")
            tag = b"CHT2"
        metadata.append((tag, text.encode() + b"\x00"))

    header_size = 124
    map_offset = header_size
    meta_offset = map_offset + 4 * len(hunks)
    meta_blob = b""
    offset = meta_offset
    for index, (tag, data) in enumerate(metadata):
        entry_size = 16 + len(data)
        following = offset + entry_size if index + 1 < len(metadata) else 0
        meta_blob += tag + struct.pack(">IQ", (0x01 << 24) | len(data), following) + data
        offset += entry_size
    first_hunk = -(-(meta_offset + len(meta_blob)) // HUNK)
    header = (b"MComprHD" + struct.pack(">II", header_size, 5) + b"\x00" * 16
              + struct.pack(">QQQII", len(hunks) * HUNK, map_offset, meta_offset, HUNK, FRAME)
              + b"\x00" * 60)
    assert len(header) == header_size
    hunk_map = b"".join(struct.pack(">I", first_hunk + i) for i in range(len(hunks)))
    with path.open("wb") as out:
        out.write(header + hunk_map + meta_blob)
        out.write(b"\x00" * (first_hunk * HUNK - out.tell()))
        for hunk in hunks:
            out.write(hunk)


def write_sheet(directory: pathlib.Path, name: str, tracks: list[Track],
                gdrom: bool) -> pathlib.Path:
    """The same disc as chdman's input: a .cue with one bin per track, or a .gdi."""
    if gdrom:
        lines = [str(len(tracks))]
        for track, (_, lba) in zip(tracks, layout(tracks)):
            ext = "bin" if track.kind == "MODE1_RAW" else "raw"
            fname = f"{name}{track.number:02d}.{ext}"
            (directory / fname).write_bytes(b"".join(track.sectors))
            ctrl = 4 if track.kind == "MODE1_RAW" else 0
            lines.append(f"{track.number} {lba} {ctrl} 2352 {fname} 0")
        sheet = directory / f"{name}.gdi"
        sheet.write_text("\n".join(lines) + "\n")
        return sheet
    lines = []
    for track in tracks:
        fname = f"{name} (Track {track.number}).bin"
        stored = [b"\x00" * FRAME_DATA] * track.pregap if track.pregap_stored else []
        (directory / fname).write_bytes(b"".join(stored + track.sectors))
        mode = "MODE1/2352" if track.kind == "MODE1_RAW" else "AUDIO"
        lines += [f'FILE "{fname}" BINARY', f"  TRACK {track.number:02d} {mode}"]
        if track.pregap and track.pregap_stored:
            lines += ["    INDEX 00 00:00:00",
                      f"    INDEX 01 00:00:{track.pregap:02d}"]
        else:
            if track.pregap:
                lines.append(f"    PREGAP 00:00:{track.pregap:02d}")
            lines.append("    INDEX 01 00:00:00")
    sheet = directory / f"{name}.cue"
    sheet.write_text("\n".join(lines) + "\n")
    return sheet


# --------------------------------------------------------------------------
# The discs.

def iso_dir_record(extent: int, size: int, name: bytes, is_dir: bool) -> bytes:
    length = 33 + len(name) + (len(name) + 1) % 2
    record = bytearray(length)
    record[0] = length
    record[2:10] = struct.pack("<I", extent) + struct.pack(">I", extent)
    record[10:18] = struct.pack("<I", size) + struct.pack(">I", size)
    record[25] = 2 if is_dir else 0
    record[32] = len(name)
    record[33:33 + len(name)] = name
    return bytes(record)


def dreamcast_disc() -> tuple[list[Track], int, bytes]:
    """GD-ROM: low-density data and audio of 6 and 10 frames, then the
    high-density track 3. Neither count is a multiple of 4, which is the
    Sonic Adventure 2 shape (5574 and 39426)."""
    low = Track(1, "MODE1_RAW", [mode1_frame(i, b"LOW DENSITY") for i in range(6)])
    audio = Track(2, "AUDIO", [audio_frame(i) for i in range(10)])
    hd_lba = len(low.sectors) + len(audio.sectors)

    boot = bytes((i * 31 + 5) & 0xFF for i in range(3000))
    ip = bytearray(2048)
    ip[0:16] = b"SEGA SEGAKATANA "
    ip[0x40:0x50] = b"T-00000   V1.000"
    ip[0x60:0x70] = b"1ST_READ.BIN    "
    ip[0x80:0x90] = b"LEAF CHD FIXTURE"
    pvd = bytearray(2048)
    pvd[0:6] = b"\x01CD001"
    pvd[128:130] = struct.pack("<H", 2048)
    pvd[156:156 + 34] = iso_dir_record(hd_lba + 17, 2048, b"\x00", True)
    root = (iso_dir_record(hd_lba + 17, 2048, b"\x00", True)
            + iso_dir_record(hd_lba + 17, 2048, b"\x01", True)
            + iso_dir_record(hd_lba + 18, len(boot), b"1ST_READ.BIN;1", False))
    user = [bytes(ip)] + [b""] * 15 + [bytes(pvd), root, boot[:2048], boot[2048:]]
    hd = Track(3, "MODE1_RAW", [mode1_frame(hd_lba + i, u) for i, u in enumerate(user)])
    expected = hashlib.md5(bytes(ip[:256]) + boot).hexdigest()
    return [low, audio, hd], hd_lba, expected


def pce_disc(stored_pregap: bool) -> tuple[list[Track], int, bytes]:
    """PC Engine CD: an audio track of 7 frames (padded to 8), then the data
    track with a 3-frame pregap -- plain MODE1 (Rondo) or VMODE1_RAW."""
    audio = Track(1, "AUDIO", [audio_frame(i) for i in range(7)])
    data_lba = len(audio.sectors) + 3
    program = [bytes(((s + 1) * 13 + i) & 0xFF for i in range(2048)) for s in range(2)]
    header = bytearray(2048)
    header[0:4] = bytes([0, 0, 2, 2])  # program at track sector 2, 2 sectors
    header[32:55] = b"PC Engine CD-ROM SYSTEM"
    header[106:128] = b"LEAF PGTYPE FIXTURE   "
    user = [b"", bytes(header), *program, b"TAIL", b"TAIL"]
    data = Track(2, "MODE1_RAW", [mode1_frame(data_lba + i, u) for i, u in enumerate(user)],
                 pregap=3, pregap_stored=stored_pregap)
    expected = hashlib.md5(bytes(header[106:128]) + b"".join(program)).hexdigest()
    return [audio, data], data_lba, expected


# --------------------------------------------------------------------------

def run_cases(lib: ctypes.CDLL, work: pathlib.Path, chdman: str | None) -> None:
    cases = [
        ("gdrom-unpadded-lengths", *dreamcast_disc(), True, 3, CONSOLE_DREAMCAST,
         0, b"SEGA SEGAKATANA "),
        ("cd-pregap-not-stored", *pce_disc(False), False, 2, CONSOLE_PC_ENGINE_CD,
         1, b"\x00\x00\x02\x02"),
        ("cd-pregap-stored-V", *pce_disc(True), False, 2, CONSOLE_PC_ENGINE_CD,
         1, b"\x00\x00\x02\x02"),
    ]
    for name, tracks, lba, expected, gdrom, number, console, sector, marker in cases:
        images = [("python", work / f"{name}.chd")]
        write_chd(images[0][1], tracks, gdrom)
        if chdman:
            source = work / f"{name}-sheet"
            source.mkdir()
            sheet = write_sheet(source, name, tracks, gdrom)
            out = work / f"{name}-chdman.chd"
            subprocess.run([chdman, "createcd", "-i", str(sheet), "-o", str(out)],
                           check=True, stdout=subprocess.DEVNULL)
            images.append(("chdman", out))
        chd_start = layout(tracks)[number - 1][0]
        for origin, image in images:
            first, data = track_view(lib, image, number, sector, 2048)
            check(first == lba, f"{name} [{origin}]: track {number} starts at LBA {lba} "
                                f"(CHD frame {chd_start}), reader says {first}")
            check(data[:len(marker)] == marker,
                  f"{name} [{origin}]: sector {sector} of track {number} holds the header")
            got = rc_hash(lib, image, console)
            check(got == expected, f"{name} [{origin}]: rc_hash {got} == {expected}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lib", type=pathlib.Path, help="prebuilt host reader library")
    parser.add_argument("--chdman", help="also convert each disc with this chdman")
    args = parser.parse_args()
    chdman = args.chdman or shutil.which("chdman")
    if os.environ.get("RAOP_REQUIRE_CHDMAN") and not chdman:
        sys.exit("RAOP_REQUIRE_CHDMAN is set but chdman is not on PATH")

    work = pathlib.Path(tempfile.mkdtemp(prefix="raop-chd-"))
    try:
        lib = load(args.lib or build_library(work / "build"))
        print(f"chdman cross-check: {chdman or 'not available (pure Python CHDs only)'}")
        run_cases(lib, work, chdman)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print("\nchd-reader-test: all checks passed")


if __name__ == "__main__":
    main()
