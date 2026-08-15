"""Read-only view of Jawaka's game library for offline pre-caching.

Automatic caching only covers games already played online, so a user who packs
the device for a trip finds nothing cached for anything they had not already
played. To pre-cache a game the pak needs its ROM path and system, and Jawaka
has no IPC that returns a game list -- its closest request, ``library-status``,
reports scan generation and progress, not games. Adding one would mean a new
generic contract in jawakad, which the plan's locked boundaries avoid.

So the pak reads ``$UMRK_INTERNAL_DATA_PATH/library.db`` directly. That is a
schema coupling to another repo, and the point of this module is to make it
**explicit and fail-closed** rather than silent:

* ``PRAGMA user_version`` is checked before any table is touched, and anything
  other than the version this was written against raises. A future launcher
  schema change disables pre-caching with a clear message instead of reading a
  table it no longer understands and pre-caching the wrong thing -- a failure
  that would look like success until the user is offline.
* The connection is opened read-only through a ``file:`` URI, so a bug here
  cannot write to the launcher's database.
* ``library.db`` is ``journal_mode=delete`` for FAT32 safety, which means a
  reader takes no persistent lock and cannot block the launcher's writes. The
  busy timeout is short because the launcher is the writer that matters: if it
  holds the file, we wait briefly and give up rather than stall the UI.

``rom_path`` is absolute and may point at **either** SD card -- on the
qualification device the sampled rows sit on the secondary card while the
launcher runs from the primary -- so nothing here may assume a root.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

#: The only ``PRAGMA user_version`` this module is known to understand. Bump
#: only together with a re-read of Jawaka's schema; see the module docstring
#: for why this fails closed rather than open.
LIBRARY_SCHEMA_VERSION = 6

#: Short: the launcher is the writer, and a stalled pak UI is worse than a
#: pre-cache action that reports the library as busy.
BUSY_TIMEOUT_SECONDS = 2.0

#: Jawaka's key for a user's own console rename, in ``system_settings``.
SYSTEM_DISPLAY_NAME_KEY = "display_name"

#: Console names mirroring Jawaka's ``kSystemDisplayNames``
#: (``internal/launcher/system_names.c``). Duplicated rather than shared
#: because there is no contract that exposes it, and inventing one would mean a
#: new generic surface in jawakad -- exactly what this plan's locked boundaries
#: avoid. The cost is drift: if Jawaka adds a console here, the pak shows the
#: folder id until this list catches up, which is the same thing it does for
#: any unknown system and is therefore safe rather than wrong.
#:
#: One deliberate divergence: Jawaka runs these through its i18n catalog, so a
#: user in a translated locale sees 街机 where this shows "Arcade". The pak
#: ships no translations. The other two tiers -- the user's own rename and the
#: raw folder id -- are byte-identical to Jawaka's, and those are the ones a
#: user has actually chosen.
SYSTEM_DISPLAY_NAMES: dict[str, str] = {
    "FC": "Nintendo Entertainment System",
    "NES": "Nintendo Entertainment System",
    "FDS": "Famicom Disk System",
    "SFC": "Super Nintendo",
    "SNES": "Super Nintendo",
    "SFC_JP": "Super Famicom",
    "N64": "Nintendo 64",
    "GB": "Game Boy",
    "GBC": "Game Boy Color",
    "GBA": "Game Boy Advance",
    "MGBA": "Game Boy Advance",
    "SGB": "Super Game Boy",
    "SUPA": "Super Nintendo",
    "NDS": "Nintendo DS",
    "VB": "Virtual Boy",
    "MD": "Sega Genesis",
    "GEN": "Sega Genesis",
    "GENESIS": "Sega Genesis",
    "MS": "Sega Master System",
    "GG": "Game Gear",
    "SEGACD": "Sega CD",
    "32X": "Sega 32X",
    "SATURN": "Sega Saturn",
    "DC": "Dreamcast",
    "SG1000": "SG-1000",
    "PCE": "TurboGrafx-16",
    "TG16": "TurboGrafx-16",
    "PCECD": "TurboGrafx-CD",
    "NEOGEO": "Neo Geo",
    "NGP": "Neo Geo Pocket",
    "NGPC": "Neo Geo Pocket Color",
    "WS": "WonderSwan",
    "WSC": "WonderSwan Color",
    "PS": "PlayStation",
    "PSX": "PlayStation",
    "PSP": "PlayStation Portable",
    "ATARI": "Atari 2600",
    "ATARI2600": "Atari 2600",
    "A2600": "Atari 2600",
    "A5200": "Atari 5200",
    "A7800": "Atari 7800",
    "PROSYSTEM": "Atari 7800",
    "SEVENTYEIGHTHUNDRED": "Atari 7800",
    "LYNX": "Atari Lynx",
    "JAGUAR": "Atari Jaguar",
    "COLECO": "ColecoVision",
    "INTV": "Intellivision",
    "VECTREX": "Vectrex",
    "C64": "Commodore 64",
    "AMIGA": "Amiga",
    "DOS": "MS-DOS",
    "MSX": "MSX",
    "ARCADE": "Arcade",
    "MAME": "Arcade",
    "FBNEO": "Arcade",
    "PORTS": "Ports",
}


class LibraryUnavailable(RuntimeError):
    """The library cannot be read safely. Carries a user-facing reason."""


@dataclass(frozen=True)
class LibraryGame:
    game_id: int
    name: str
    system: str
    rom_path: str
    #: The console as the user sees it in Jawaka. Same string, so the pak's
    #: picker and the launcher do not disagree about what a console is called.
    system_label: str = ""
    #: Why this game is in the set: "recent", "favourite", or "both". Purely
    #: for display -- the caching job treats every entry identically.
    source: str = ""

    @property
    def exists(self) -> bool:
        try:
            return Path(self.rom_path).is_file()
        except OSError:
            return False


def default_library_path() -> Path:
    root = os.environ.get("UMRK_INTERNAL_DATA_PATH")
    if not root:
        raise LibraryUnavailable(
            "UMRK_INTERNAL_DATA_PATH is not set; run inside the Leaf environment"
        )
    return Path(root) / "library.db"


class LibraryReader:
    """Opens library.db read-only, refusing any schema it was not built for."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path is not None else default_library_path()
        if not self._path.is_file():
            raise LibraryUnavailable(f"no game library at {self._path}")

        try:
            # immutable=0: the launcher may be writing. mode=ro keeps this a
            # reader no matter what the rest of this file does.
            self._db = sqlite3.connect(
                f"file:{self._path}?mode=ro",
                uri=True,
                timeout=BUSY_TIMEOUT_SECONDS,
            )
        except sqlite3.Error as exc:
            raise LibraryUnavailable(f"cannot open game library: {exc}") from exc
        self._db.row_factory = sqlite3.Row

        try:
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.Error, TypeError, ValueError) as exc:
            self.close()
            raise LibraryUnavailable(f"cannot read library schema version: {exc}") from exc

        if version != LIBRARY_SCHEMA_VERSION:
            self.close()
            raise LibraryUnavailable(
                f"game library schema is version {version}, this build understands "
                f"{LIBRARY_SCHEMA_VERSION}; update RAOfflineProxy"
            )
        self.schema_version = version

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        db = getattr(self, "_db", None)
        if db is not None:
            db.close()
            self._db = None  # type: ignore[assignment]

    def __enter__(self) -> "LibraryReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- console names ---------------------------------------------------
    def _display_overrides(self) -> dict[str, str]:
        """The user's own console renames, read once per reader.

        Jawaka resolves a console name in three tiers -- this override, then
        its built-in table, then the raw folder id -- and the pak follows the
        same order so the two never disagree about what a console is called.
        """
        cached = getattr(self, "_overrides_cache", None)
        if cached is not None:
            return cached
        overrides: dict[str, str] = {}
        try:
            rows = self._db.execute(
                "SELECT system, value FROM system_settings WHERE key = ?",
                (SYSTEM_DISPLAY_NAME_KEY,),
            )
            for row in rows:
                system = str(row["system"] or "")
                value = str(row["value"] or "")
                if system and value:
                    overrides[system.upper()] = value
        except sqlite3.Error:
            # An older or newer launcher may not have this table. A missing
            # rename is a cosmetic loss, not a reason to refuse the feature --
            # unlike a schema mismatch on games, which would cache the wrong
            # thing and is why that one fails closed.
            overrides = {}
        self._overrides_cache = overrides
        return overrides

    def display_name(self, system: str) -> str:
        key = (system or "").upper()
        override = self._display_overrides().get(key)
        if override:
            return override
        return SYSTEM_DISPLAY_NAMES.get(key, system or "")

    # -- queries ---------------------------------------------------------
    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        try:
            return list(self._db.execute(sql, params))
        except sqlite3.Error as exc:
            raise LibraryUnavailable(f"game library read failed: {exc}") from exc

    def game_count(self) -> int:
        return int(self._query("SELECT count(*) AS n FROM games")[0]["n"])

    def recents_and_favourites(self) -> list[LibraryGame]:
        """The bulk pre-cache set: everything recent or favourited.

        Both tables are keyed (kind, target_id) and hold apps as well as games,
        so ``kind='game'`` is required -- without it an app id would collide
        with an unrelated game id and pre-cache the wrong title.

        Ordered most-recently-opened first so an interrupted run has done the
        most useful work: favourites that were never opened sort last rather
        than being dropped.
        """
        rows = self._query(
            """
            SELECT g.id, g.name, g.system, g.rom_path,
                   MAX(r.last_opened) AS last_opened,
                   MAX(CASE WHEN r.target_id IS NOT NULL THEN 1 ELSE 0 END) AS is_recent,
                   MAX(CASE WHEN f.target_id IS NOT NULL THEN 1 ELSE 0 END) AS is_favourite
              FROM games g
              LEFT JOIN recents   r ON r.kind = 'game' AND r.target_id = g.id
              LEFT JOIN favorites f ON f.kind = 'game' AND f.target_id = g.id
             WHERE r.target_id IS NOT NULL OR f.target_id IS NOT NULL
             GROUP BY g.id
             ORDER BY last_opened IS NULL, last_opened DESC, g.name COLLATE NOCASE
            """
        )
        return [self._to_game(row) for row in rows]

    def systems(self) -> list[tuple[str, str, int]]:
        """(system, game count), most populous first.

        The picker leads with this because scrolling 1,968 rows to reach one
        game is not a usable way to choose: the device's largest system alone
        holds 375. Filtering to a console first is how Leaf's own scraper
        presents the same library.
        """
        rows = self._query(
            """
            SELECT system, count(*) AS n
              FROM games
             GROUP BY system
             ORDER BY n DESC, system COLLATE NOCASE
            """
        )
        return [
            (str(row["system"]), self.display_name(str(row["system"])), int(row["n"]))
            for row in rows
        ]

    def all_games(
        self, system: str | None = None, query: str | None = None, limit: int = 0
    ) -> list[LibraryGame]:
        """Games for the per-game picker, optionally narrowed.

        Filtering happens here rather than in the UI so the list that crosses
        the wire is already the one being shown -- the picker was unusable when
        it had to hold and scroll every row.

        This is a picker source, not a bulk pre-cache set: the caller still
        sends one game at a time. See the plan's rate-limit note.
        """
        where = []
        params: list = []
        if system:
            where.append("system = ? COLLATE NOCASE")
            params.append(system)
        if query:
            # LIKE with an escaped pattern: a user searching for "100%" must
            # not accidentally match everything.
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("name LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")

        sql = (
            "SELECT id, name, system, rom_path, NULL AS last_opened, "
            "0 AS is_recent, 0 AS is_favourite FROM games"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY system COLLATE NOCASE, name COLLATE NOCASE"
        if limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))

        return [self._to_game(row) for row in self._query(sql, tuple(params))]

    def game_by_id(self, game_id: int) -> LibraryGame | None:
        rows = self._query(
            """
            SELECT id, name, system, rom_path, NULL AS last_opened,
                   0 AS is_recent, 0 AS is_favourite
              FROM games WHERE id = ?
            """,
            (int(game_id),),
        )
        return self._to_game(rows[0]) if rows else None

    def _to_game(self, row: sqlite3.Row) -> LibraryGame:
        recent = bool(row["is_recent"])
        favourite = bool(row["is_favourite"])
        if recent and favourite:
            source = "both"
        elif favourite:
            source = "favourite"
        elif recent:
            source = "recent"
        else:
            source = ""
        system = str(row["system"])
        return LibraryGame(
            game_id=int(row["id"]),
            name=str(row["name"]),
            system=system,
            system_label=self.display_name(system),
            rom_path=str(row["rom_path"]),
            source=source,
        )
