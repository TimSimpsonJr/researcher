"""
vault_index.py — SQLite FTS5 index for the Obsidian vault.

Provides fast full-text search over vault notes. Built incrementally
by checking file mtimes. Used by classify, write, and thread-discover
stages instead of globbing the entire vault.
"""

import re
import sqlite3
from pathlib import Path

CONFIG_DIR = ".researcher"
DB_NAME = "vault_index.db"
EXCERPT_LENGTH = 500


def _db_path(vault_root: Path) -> Path:
    return vault_root / CONFIG_DIR / DB_NAME


def _connect(vault_root: Path) -> sqlite3.Connection:
    db = _db_path(vault_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            path TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '',
            excerpt TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            mtime REAL NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
            title, tags, body, content=notes, content_rowid=rowid
        );
        CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
            INSERT INTO notes_fts(rowid, title, tags, body)
            VALUES (new.rowid, new.title, new.tags, new.body);
        END;
        CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
            INSERT INTO notes_fts(notes_fts, rowid, title, tags, body)
            VALUES ('delete', old.rowid, old.title, old.tags, old.body);
        END;
        CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
            INSERT INTO notes_fts(notes_fts, rowid, title, tags, body)
            VALUES ('delete', old.rowid, old.title, old.tags, old.body);
            INSERT INTO notes_fts(rowid, title, tags, body)
            VALUES (new.rowid, new.title, new.tags, new.body);
        END;
    """)


def _parse_frontmatter(text: str) -> tuple[str, str]:
    """Extract title and tags from YAML frontmatter. Returns (title, tags_csv)."""
    title = ""
    tags = ""
    fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not fm_match:
        return title, tags
    fm = fm_match.group(1)
    for line in fm.splitlines():
        if line.startswith("title:"):
            title = line.split(":", 1)[1].strip().strip("'\"")
        if line.startswith("tags:"):
            rest = line.split(":", 1)[1].strip()
            if rest.startswith("["):
                tags = rest.strip("[]").replace(",", ", ")
            else:
                tags = rest
    return title, tags


def _body_text(text: str) -> str:
    """Strip frontmatter, return body text."""
    stripped = re.sub(r"^---\n.*?\n---\n?", "", text, count=1, flags=re.DOTALL)
    return stripped.strip()


def _index_file(conn: sqlite3.Connection, vault_root: Path, rel_path: str, mtime: float) -> None:
    """Index or update a single file."""
    full = vault_root / rel_path
    try:
        text = full.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    title, tags = _parse_frontmatter(text)
    if not title:
        title = full.stem
    body = _body_text(text)
    excerpt = body[:EXCERPT_LENGTH]
    conn.execute(
        "INSERT OR REPLACE INTO notes (path, title, tags, excerpt, body, mtime) VALUES (?, ?, ?, ?, ?, ?)",
        (rel_path, title, tags, excerpt, body, mtime),
    )


def _should_skip(rel_path: str) -> bool:
    """Skip hidden dirs, .researcher, and non-.md files."""
    parts = Path(rel_path).parts
    for part in parts:
        if part.startswith("."):
            return True
    return not rel_path.endswith(".md")


def build_index(vault_root: Path) -> Path:
    """Build the full index from scratch. Returns the db path."""
    db = _db_path(vault_root)
    if db.exists():
        db.unlink()
    conn = _connect(vault_root)
    _init_tables(conn)
    for md in vault_root.rglob("*.md"):
        rel = str(md.relative_to(vault_root)).replace("\\", "/")
        if _should_skip(rel):
            continue
        _index_file(conn, vault_root, rel, md.stat().st_mtime)
    conn.commit()
    conn.close()
    return db


def update_index(vault_root: Path) -> dict:
    """Incrementally update the index. Returns stats dict."""
    conn = _connect(vault_root)
    _init_tables(conn)
    stats = {"added": 0, "updated": 0, "removed": 0}
    indexed = {}
    for row in conn.execute("SELECT path, mtime FROM notes"):
        indexed[row["path"]] = row["mtime"]
    on_disk = set()
    for md in vault_root.rglob("*.md"):
        rel = str(md.relative_to(vault_root)).replace("\\", "/")
        if _should_skip(rel):
            continue
        on_disk.add(rel)
        mtime = md.stat().st_mtime
        if rel not in indexed:
            _index_file(conn, vault_root, rel, mtime)
            stats["added"] += 1
        elif mtime > indexed[rel]:
            _index_file(conn, vault_root, rel, mtime)
            stats["updated"] += 1
    for path in indexed:
        if path not in on_disk:
            conn.execute("DELETE FROM notes WHERE path = ?", (path,))
            stats["removed"] += 1
    conn.commit()
    conn.close()
    return stats


def _prepare_query(query: str) -> str:
    """Add prefix matching to each term for broader recall."""
    tokens = query.strip().split()
    return " ".join(f"{t}*" for t in tokens if t)


def search(vault_root: Path, query: str, limit: int = 20) -> list[dict]:
    """Full-text search. Returns list of {path, title, tags, excerpt, rank}."""
    prepared = _prepare_query(query)
    if not prepared:
        return []
    conn = _connect(vault_root)
    rows = conn.execute(
        """SELECT n.path, n.title, n.tags, n.excerpt,
                  bm25(notes_fts, 10.0, 5.0, 1.0) AS rank
           FROM notes_fts f
           JOIN notes n ON n.rowid = f.rowid
           WHERE notes_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
        (prepared, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_notes(vault_root: Path) -> list[dict]:
    """List all indexed notes."""
    conn = _connect(vault_root)
    rows = conn.execute("SELECT path, title, tags FROM notes ORDER BY path").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def note_exists(vault_root: Path, title: str) -> bool:
    """Check if a note with this title exists in the index."""
    conn = _connect(vault_root)
    row = conn.execute("SELECT 1 FROM notes WHERE title = ? LIMIT 1", (title,)).fetchone()
    conn.close()
    return row is not None


_MOC_NAME_HINTS = ("MOC", "Index", "Hub", "Overview", "Dashboard")
_INLINE_TAG = re.compile(r"(?<!\w)#[a-zA-Z][a-zA-Z0-9_/-]+")


def scan_conventions(vault_root: Path, sample: int = 50) -> dict:
    """Detect folder, tag-format, frontmatter and MOC conventions (setup Step 2).

    Counts all non-hidden .md files; inspects the first `sample` of them.
    """
    top_folders = sorted(
        d.name for d in vault_root.iterdir() if d.is_dir() and not d.name.startswith(".")
    )
    md_files = [
        f for f in vault_root.rglob("*.md")
        if not any(p.startswith(".") for p in f.relative_to(vault_root).parts)
    ]
    tag_formats = {"list": 0, "string": 0, "inline": 0, "none": 0}
    fm_fields: dict[str, int] = {}
    moc_files: list[str] = []
    for f in md_files[:sample]:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if f.stem.startswith("_") or any(k in f.stem for k in _MOC_NAME_HINTS):
            moc_files.append(f.relative_to(vault_root).as_posix())
        fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if fm_match:
            for line in fm_match.group(1).splitlines():
                if ":" in line:
                    key = line.split(":")[0].strip()
                    fm_fields[key] = fm_fields.get(key, 0) + 1
                if line.strip().startswith("tags:"):
                    rest = line.split(":", 1)[1].strip()
                    if rest.startswith("[") or rest.startswith("-") or rest == "":
                        tag_formats["list"] += 1
                    else:
                        tag_formats["string"] += 1
        elif _INLINE_TAG.search(text):
            tag_formats["inline"] += 1
    return {
        "total_notes": len(md_files),
        "top_folders": top_folders,
        "tag_format_counts": tag_formats,
        "dominant_tag_format": (
            max(tag_formats, key=tag_formats.get) if any(tag_formats.values()) else "list"
        ),
        "frontmatter_fields": sorted(fm_fields, key=lambda k: -fm_fields[k])[:10],
        "moc_files": moc_files[:10],
        "moc_pattern": "^_|MOC|Index|Hub" if moc_files else "^_|MOC|Index",
    }
