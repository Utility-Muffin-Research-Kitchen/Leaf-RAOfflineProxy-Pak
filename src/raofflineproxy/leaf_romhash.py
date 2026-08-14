"""RetroAchievements ROM hashing for games that have not been launched.

The offline pre-cache needs a game's RA hash before the user ever plays it, so
it can fetch and store the achievement data while there is still a network. The
hash must be byte-identical to the one rcheevos computes inside RetroArch: the
proxy caches keyed on it, so a hash that disagrees stores data under a key
nothing ever asks for -- a failure that looks exactly like success until the
user is offline with nothing cached.

Two rules make that agreement work, and neither is obvious:

1. **Archives are extracted before hashing.** Handed a ``.zip``, rc_hash's
   untargeted iterator returns the *Arcade* hash, and Arcade hashes the
   filename rather than the content. A zipped Mega Drive ROM then hashes to
   something RetroArch never sends. RetroArch extracts and hashes the resulting
   buffer; so do we.
2. **The console id is always supplied.** Hashing is per-console: NES skips the
   iNES header, Mega Drive is a plain file MD5, Arcade hashes a name. Leaf's
   ``system`` column is the source of that id.

Both were verified against hashes RetroArch really sent through this proxy; see
``docs/`` and ``tests`` for the known-good pairs.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger("raofflineproxy")

HASH_LEN = 33  # 32 hex chars + NUL, matching rc_hash's char[33]

#: Leaf ``games.system`` -> RetroAchievements console id (rc_consoles.h).
#: Only systems Leaf actually presents are listed; anything absent is treated
#: as unsupported rather than guessed, because a wrong console id yields a
#: confidently wrong hash.
SYSTEM_CONSOLE_IDS: dict[str, int] = {
    "MD": 1,          # Mega Drive / Genesis
    "SFC": 3,         # Super Famicom / SNES
    "GB": 4,
    "GBA": 5,
    "GBC": 6,
    "FC": 7,          # Famicom / NES
    "PCE": 8,
    "SEGACD": 9,
    "32X": 10,
    "MS": 11,         # Master System
    "PS": 12,         # PlayStation
    "GG": 15,         # Game Gear
    "NDS": 18,
    "ATARI2600": 25,
    "ARCADE": 27,
    "DC": 40,         # Dreamcast
    "PSP": 41,
    # Leaf's NEOGEO is arcade romsets (MVS/AES), not Neo Geo CD: console 56
    # fails outright on androdun.zip while 27 hashes it. Arcade hashes the
    # archive NAME, so these must not be extracted first -- see hash_rom.
    "NEOGEO": 27,
    "PCECD": 76,
}

#: Archive containers Leaf hands to cores. Everything else is hashed in place.
ARCHIVE_SUFFIXES = {".zip"}

#: Above this, an extracted ROM is spilled to a temp file and hashed by path
#: rather than held in memory. MLP1 has 955 MB of RAM and a 128 MB DS ROM is
#: ordinary, so buffering everything would be a memory bug waiting for a big
#: cartridge; buffering nothing would write tens of MB to a 7.3 MB/s card for
#: every Game Boy title.
MAX_BUFFER_BYTES = 32 * 1024 * 1024


@dataclass
class HashResult:
    hash: str | None
    error: str | None = None


class RomHasher:
    """Binds libraproxy_rchash.so and hashes ROMs for a given Leaf system."""

    def __init__(self, library_path: str | os.PathLike[str] | None = None) -> None:
        self._lib = None
        self._error: str | None = None
        self._version: str | None = None
        self._load(library_path)

    # -- binding ---------------------------------------------------------
    def _candidate_paths(self, override) -> list[Path]:
        if override:
            return [Path(override)]
        env = os.environ.get("RAOFFLINEPROXY_RCHASH_LIB")
        if env:
            return [Path(env)]
        # <pak>/app/raofflineproxy/leaf_romhash.py -> <pak>/lib/
        here = Path(__file__).resolve().parent
        return [here.parent.parent / "lib" / "libraproxy_rchash.so"]

    def _load(self, override) -> None:
        for candidate in self._candidate_paths(override):
            if not candidate.is_file():
                self._error = f"libraproxy_rchash not found at {candidate}"
                continue
            try:
                lib = ctypes.CDLL(str(candidate))
                lib.raproxy_hash_file.argtypes = [
                    ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p]
                lib.raproxy_hash_file.restype = ctypes.c_int
                lib.raproxy_hash_buffer.argtypes = [
                    ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32,
                    ctypes.c_char_p, ctypes.c_char_p]
                lib.raproxy_hash_buffer.restype = ctypes.c_int
                lib.raproxy_rchash_version.restype = ctypes.c_char_p
            except (OSError, AttributeError) as exc:
                self._error = f"libraproxy_rchash unusable ({candidate}): {exc}"
                continue
            self._lib = lib
            self._version = lib.raproxy_rchash_version().decode("ascii", "replace")
            LOGGER.info("rc_hash ready (rcheevos %s) from %s", self._version, candidate)
            return

    @property
    def available(self) -> bool:
        return self._lib is not None

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def rcheevos_version(self) -> str | None:
        return self._version

    # -- hashing ---------------------------------------------------------
    @staticmethod
    def console_id(system: str) -> int | None:
        return SYSTEM_CONSOLE_IDS.get((system or "").upper())

    def hash_rom(self, path: str | os.PathLike[str], system: str) -> HashResult:
        if not self.available:
            return HashResult(None, self._error or "rc_hash unavailable")

        console = self.console_id(system)
        if console is None:
            return HashResult(None, f"no RetroAchievements console id for system {system!r}")

        rom = Path(path)
        if not rom.is_file():
            return HashResult(None, f"ROM not found: {rom}")

        out = ctypes.create_string_buffer(HASH_LEN)
        suffix = rom.suffix.lower()

        # Arcade is the exception that proves the rule: its hash IS derived
        # from the archive's name, so it must NOT be extracted first.
        if suffix in ARCHIVE_SUFFIXES and console != SYSTEM_CONSOLE_IDS["ARCADE"]:
            extracted = self._extract_rom(rom)
            if extracted.error:
                return HashResult(None, extracted.error)
            data, inner_name, spill_path = extracted.payload
            try:
                if spill_path is not None:
                    ok = self._lib.raproxy_hash_file(
                        str(spill_path).encode("utf-8"), console, out)
                else:
                    ok = self._lib.raproxy_hash_buffer(
                        data, len(data), console, inner_name.encode("utf-8"), out)
            finally:
                if spill_path is not None:
                    spill_path.unlink(missing_ok=True)
        else:
            ok = self._lib.raproxy_hash_file(str(rom).encode("utf-8"), console, out)

        if not ok:
            return HashResult(None, f"rc_hash could not hash {rom.name} as console {console}")
        value = out.value.decode("ascii", "ignore").strip().lower()
        if len(value) != 32:
            return HashResult(None, f"rc_hash returned a malformed hash for {rom.name}")
        return HashResult(value)

    # -- archives --------------------------------------------------------
    @dataclass
    class _Extracted:
        #: (bytes, inner_name, spill_path). Exactly one of bytes / spill_path
        #: carries the ROM; spill_path is set for members too large to buffer.
        payload: tuple[bytes, str, "Path | None"] = (b"", "", None)
        error: str | None = None

    def _extract_rom(self, archive_path: Path) -> "RomHasher._Extracted":
        """Return the bytes of the ROM inside a zip, plus its inner name.

        Leaf's cores load the first usable member, so a multi-ROM archive is
        ambiguous; the largest regular member is the ROM in practice, and
        directory entries and metadata files are skipped.
        """
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = [
                    info for info in archive.infolist()
                    if not info.is_dir() and info.file_size > 0
                    and not info.filename.startswith("__MACOSX/")
                ]
                if not members:
                    return self._Extracted(error=f"archive has no ROM member: {archive_path.name}")
                member = max(members, key=lambda info: info.file_size)
                inner_name = Path(member.filename).name
                if member.file_size > MAX_BUFFER_BYTES:
                    # A 128 MB DS ROM is normal; spill it rather than hold it.
                    spill_dir = Path(
                        os.environ.get("UMRK_RUNTIME_PATH") or tempfile.gettempdir())
                    spill_dir.mkdir(parents=True, exist_ok=True)
                    handle, spill_name = tempfile.mkstemp(
                        prefix="raop-hash-", suffix=Path(inner_name).suffix or ".rom",
                        dir=str(spill_dir))
                    spill_path = Path(spill_name)
                    try:
                        with archive.open(member) as source, os.fdopen(handle, "wb") as target:
                            shutil.copyfileobj(source, target, 1024 * 1024)
                    except BaseException:
                        spill_path.unlink(missing_ok=True)
                        raise
                    return self._Extracted(payload=(b"", inner_name, spill_path))
                with archive.open(member) as handle:
                    return self._Extracted(payload=(handle.read(), inner_name, None))
        except (zipfile.BadZipFile, OSError) as exc:
            return self._Extracted(error=f"could not read {archive_path.name}: {exc}")
