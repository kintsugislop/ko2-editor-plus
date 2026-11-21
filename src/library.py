"""Your sample library on this Mac: an index you can search like Splice.

Roots default to ~/Splice/sounds (what the Splice app keeps on disk); more
folders can be added. Nothing is fetched from Splice -- only files already
here are indexed. BPM, key and type come from the file name, length and format from the file header.

The index is a SQLite file next to the station, rebuilt incrementally by mtime.
Your stars and tags live in the same file and survive rescans.
"""
import os, re, sqlite3, struct, threading, time, json, sys
from pathlib import Path
from paths import LIBRARY_DIR, SOURCE_DIR

AUDIO_EXT = {".wav", ".wave", ".aif", ".aiff", ".flac", ".mp3", ".ogg", ".m4a"}
TOKEN_SPLIT = re.compile(r"[_\-\s.()\[\]]+")
BPM_SUFFIX = re.compile(r"^(\d{2,3})\s?bpm$", re.I)
BPM_PREFIX = re.compile(r"^bpm(\d{2,3})$", re.I)
KEY_RE = re.compile(r"^([A-Ga-g])(#|b|sharp|flat)?(maj|major|min|minor|m)?$")
FLAT_TO_SHARP = {"Ab": "G#", "Bb": "A#", "Cb": "B", "Db": "C#", "Eb": "D#", "Fb": "E", "Gb": "F#"}
TYPE_RULES = [(t, re.compile(rx)) for t, rx in [
    ("vocal",  r"vocal|vox|acapella|a_?cappella|adlib|ad_lib|chant|choir|spoken|sing|hum(?![a-z])|oohs|aahs"),
    ("drums",  r"drum|kick|snare|hihat|hi_hat|(?<![a-z])hat(?![a-z])|clap|(?<![a-z])rim(?![a-z])"
               r"|cymbal|(?<![a-z])ride(?![a-z])|crash|tom(?![a-z])|break|(?<![a-z])fill(?![a-z])|top_?loop"),
    ("perc",   r"perc|shaker|tamb|conga|bongo|cajon|djembe|cowbell|clave|timbale|woodblock|triangle"),
    ("bass",   r"bass|808|sub_?bass"),
    ("guitar", r"guitar|gtr|ukulele|banjo|mandolin"),
    ("strings", r"strings|violin|viola|cello|orchestra"),
    ("winds",  r"horn|brass|trumpet|trombone|sax|flute|clarinet|woodwind"),
    ("synth",  r"synth|pad(?![a-z])|lead|arp(?![a-z])|pluck|moog|juno"),
    ("keys",   r"piano|keys|rhodes|wurli|organ|e_?piano|clav|kalimba|mallet|marimba|vibraphone|bell|celeste"),
    ("fx",     r"fx|riser|sweep|impact|downlifter|uplifter|whoosh|foley|texture|ambien|atmos|noise|vinyl|drone|glitch"),
    ("melodic", r"melody|melodic|chord|harmony|riff|progression|lick|motif"),
]]
DRUM_PART = [("kick", r"kick|(?<![a-z])bd(?![a-z])|808_?kick"), ("snare", r"snare|(?<![a-z])sd(?![a-z])|rim"),
             ("clap", r"clap|snap"), ("hat", r"hat|hh(?![a-z])|cymbal|ride|crash|shaker"),
             ("tom", r"(?<![a-z])tom")]
DRUM_PART = [(t, re.compile(rx)) for t, rx in DRUM_PART]


def parse_bpm(tokens):
    for tok in tokens:
        if tok.isdigit() and 40 <= int(tok) <= 220:
            return int(tok)
        m = BPM_SUFFIX.match(tok) or BPM_PREFIX.match(tok)
        if m and 40 <= int(m.group(1)) <= 220:
            return int(m.group(1))
    return None


def parse_key(tokens):
    last = len(tokens) - 1
    for i, tok in enumerate(tokens):
        m = KEY_RE.match(tok)
        if not m:
            continue
        note, acc, qual = m.group(1).upper(), m.group(2), m.group(3)
        if not qual and i != last:
            continue
        if acc in ("sharp", "#"):
            note += "#"
        elif acc in ("flat", "b"):
            note = FLAT_TO_SHARP.get(note + "b", note + "b")
        mode = ("min" if qual.lower() in ("min", "minor", "m") else "maj") if qual else ""
        return note + mode
    return None


def classify(s):
    for t, rx in TYPE_RULES:
        if rx.search(s):
            return t
    return "other"


def audio_info(path):
    """(seconds, channels, rate) from a WAV or AIFF header; Nones if unknown."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
            if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
                ch = rate = bits = None
                while True:
                    h = fh.read(8)
                    if len(h) < 8:
                        return None, None, None
                    cid, size = h[:4], struct.unpack("<I", h[4:])[0]
                    if cid == b"fmt ":
                        f = fh.read(size)
                        _t, ch, rate, _b, _a, bits = struct.unpack("<HHIIHH", f[:16])
                    elif cid == b"data":
                        if ch and rate and bits:
                            return size / (ch * bits / 8) / rate, ch, rate
                        return None, None, None
                    else:
                        fh.seek(size + (size & 1), 1)
            if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
                while True:
                    h = fh.read(8)
                    if len(h) < 8:
                        return None, None, None
                    cid, size = h[:4], struct.unpack(">I", h[4:])[0]
                    if cid == b"COMM":
                        c = fh.read(size)
                        ch, frames = struct.unpack(">hI", c[:6])
                        exp, mant = struct.unpack(">HQ", c[8:18])
                        rate = mant * 2.0 ** ((exp & 0x7FFF) - 16383 - 63) if mant else 0
                        return (frames / rate if rate else None), ch, int(round(rate)) or None
                    fh.seek(size + (size & 1), 1)
    except Exception:
        pass
    return None, None, None


def te_name(stem):
    """The name the device shows for a file, as the TE Sample Tool makes it:
    ASCII, lower case, 16 characters; longer ones keep the first 7 and last 8
    around a dot (a 'OS_FIRE_..._atred_bm' file shows as 'os_fire.atred_bm')."""
    import unicodedata
    s = unicodedata.normalize("NFD", stem)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if 32 <= ord(c) < 127 else "?" for c in s).replace("/", "").replace("\\", "").replace('"', "").strip().lower()
    return s if len(s) <= 16 else s[:7] + "." + s[-8:]


def te_names(stem):
    """Both forms a device name can take: the Sample Tool's head.tail and a plain
    16-character cut (what this station and some other tools write)."""
    full = te_name(stem)
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFD", stem) if not unicodedata.combining(c)).strip().lower()
    return {full, s[:16], s.replace(".", "_")[:16]}


def device_slots(t1, t2, dup2, secs, dev, twins=None):
    """Slots on the device that hold this library file.
    dev: device name -> [(slot, seconds)]. The Sample Tool's head.tail name is
    nearly unique; a plain 16-character cut is shared by whole families
    ('oliver_kick_clea…'), so that one only counts when no other file shares
    it or the length agrees to the millisecond (a straight send keeps it)."""
    hits = set()
    for slot, s in dev.get(t1, []):
        if s is None or secs is None or s <= secs + 0.15:        # a trimmed copy is shorter
            hits.add(slot)
    if t2 and t2 != t1:
        for slot, s in dev.get(t2, []):
            if dup2 <= 1 and (s is None or secs is None or s <= secs + 0.15):
                hits.add(slot)
            elif s is not None and secs is not None and abs(s - secs) < 0.02:
                # only when no sibling of the same family has that length too
                if twins is None or twins(t2, s) == 1:
                    hits.add(slot)
    return sorted(hits)


class _Twins:
    """How many files share a 16-character cut and a length (to the 20 ms)."""
    def __init__(self, con):
        self.c, self.memo = con, {}

    def __call__(self, t2, s):
        k = (t2, round(s, 3))
        if k not in self.memo:
            self.memo[k] = self.c.execute("SELECT COUNT(*) FROM files WHERE tename2=? AND ABS(secs-?)<0.02", (t2, s)).fetchone()[0]
        return self.memo[k]


class Library:
    def __init__(self, db_path, roots_path):
        self.db_path, self.roots_path = db_path, roots_path
        self.lock = threading.RLock()
        self.rescan_pending = False
        self.scan = {"running": False, "done": 0, "total": 0, "added": 0, "error": None, "at": None}
        with self.con() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS files(
              id INTEGER PRIMARY KEY, path TEXT UNIQUE, root TEXT, rel TEXT, name TEXT, pack TEXT,
              whole INTEGER DEFAULT 0, type TEXT, part TEXT, kind TEXT, bpm INTEGER, key TEXT,
              secs REAL, ch INTEGER, rate INTEGER, bytes INTEGER, mtime REAL, seen REAL);
            CREATE INDEX IF NOT EXISTS i_type ON files(type);
            CREATE INDEX IF NOT EXISTS i_pack ON files(pack);
            CREATE TABLE IF NOT EXISTS marks(path TEXT PRIMARY KEY, fav INTEGER DEFAULT 0, tags TEXT DEFAULT '',
              sent TEXT DEFAULT '');
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, words, tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS packs(pack TEXT PRIMARY KEY, n INTEGER, whole INTEGER);
            CREATE TABLE IF NOT EXISTS feats(id INTEGER PRIMARY KEY, vec TEXT);
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS spacks(pack TEXT PRIMARY KEY, uuid TEXT, title TEXT, cover TEXT, genre TEXT,
              label TEXT, descr TEXT, link TEXT, bought TEXT);
            CREATE TABLE IF NOT EXISTS ssamples(id INTEGER PRIMARY KEY, tags TEXT, stype TEXT, bpm INTEGER, key TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS stags USING fts5(tags, tokenize='unicode61');
            """)
            # Older indexes omitted the original terms, so a deleted file could
            # still match searches when SQLite reused its row id for a new file.
            schema = c.execute("SELECT sql FROM sqlite_master WHERE name='fts'").fetchone()[0]
            if "content=''" in schema:
                c.execute("BEGIN IMMEDIATE")
                c.execute("DROP TABLE fts")
                c.execute("CREATE VIRTUAL TABLE fts USING fts5(name, words, tokenize='unicode61')")
                for row in c.execute("SELECT id,name,rel,type,part,kind FROM files"):
                    self._index_terms(c, *row)
            cols = {r[1] for r in c.execute("PRAGMA table_info(files)")}
            if "tename2" not in cols:
                c.execute("ALTER TABLE files ADD COLUMN tename2 TEXT")
                for fid, name in c.execute("SELECT id, name FROM files").fetchall():
                    c.execute("UPDATE files SET tename2=? WHERE id=?", (name.strip().lower()[:16], fid))
                c.execute("CREATE INDEX IF NOT EXISTS i_te2 ON files(tename2)")
            if "tename" not in cols:
                c.execute("ALTER TABLE files ADD COLUMN tename TEXT")
                for fid, name in c.execute("SELECT id, name FROM files").fetchall():
                    c.execute("UPDATE files SET tename=? WHERE id=?", (te_name(name), fid))
                c.execute("CREATE INDEX IF NOT EXISTS i_te ON files(tename)")
            # a pack bought in full: vendor folder with categories inside AND at least 20 files
            if not c.execute("SELECT COUNT(*) FROM packs").fetchone()[0]:
                self._packstats(c)

    @staticmethod
    def _packstats(c):
        c.execute("DELETE FROM packs")
        c.execute("INSERT INTO packs SELECT pack, COUNT(*), (MAX(whole) = 1 AND COUNT(*) >= 20) FROM files GROUP BY pack")

    def con(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    # ---------- roots ----------
    def roots(self):
        try:
            r = json.loads(Path(self.roots_path).read_text())
        except Exception:
            r = ["~/Splice/sounds"] if os.path.isdir(os.path.expanduser("~/Splice/sounds")) else []
        return [x for x in r if isinstance(x, str) and x.strip()]

    def set_roots(self, roots):
        clean = []
        existing = self.roots()
        for raw in roots:
            root = str(raw).strip()
            if not root or root in clean:
                continue
            path = os.path.expanduser(root)
            if not os.path.isabs(path) or (not os.path.isdir(path) and root not in existing):
                raise ValueError(f"Folder not found: {root}")
            clean.append(root)
        with self.lock:
            target = Path(self.roots_path)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(clean, indent=2))
            tmp.replace(target)
            if self.scan["running"]:
                self.rescan_pending = True
        return clean

    # ---------- scanning ----------
    @staticmethod
    def _pack_of(root, rel):
        parts = rel.split(os.sep)
        # Splice: <root>/packs/<Pack name>/...; elsewhere: the first folder
        if len(parts) > 2 and parts[0].lower() == "packs":
            return parts[1]
        return parts[0] if len(parts) > 1 else os.path.basename(root)

    # ---------- the Splice app's own catalogue ----------
    @staticmethod
    def splice_db():
        """The Splice desktop app keeps what it knows about your sounds in a SQLite
        file: tags, loop/one-shot, key, and every pack's cover, genre and label."""
        import glob
        c = []
        for app in ("com.splice.Splice", "com.splice.SpliceBeta"):
            c += glob.glob(os.path.expanduser(f"~/Library/Application Support/{app}/users/*/*/sounds.db"))
        return max(c, key=os.path.getmtime) if c else None

    def splice_sync(self, force=False):
        path = self.splice_db()
        if not path:
            return {"db": None}
        stamp = str(os.stat(path).st_mtime_ns)
        if os.path.exists(path + "-wal"):
            stamp += ":" + str(os.stat(path + "-wal").st_mtime_ns)
        with self.con() as c:
            row = c.execute("SELECT v FROM kv WHERE k='splice_mtime'").fetchone()
            if not force and row and row[0] == stamp and c.execute("SELECT COUNT(*) FROM ssamples").fetchone()[0]:
                return {"db": path, "fresh": True}
        src = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=10)
        src.row_factory = sqlite3.Row
        packs = {r["uuid"]: dict(r) for r in src.execute(
            "SELECT uuid, name, description, cover_url, genre, permalink, provider_name FROM packs")}
        rows = src.execute("SELECT local_path, pack_uuid, tags, sample_type, bpm, audio_key, chord_type, purchased_at "
                           "FROM samples WHERE local_path IS NOT NULL").fetchall()
        src.close()
        with self.con() as c:
            ids = {p: (i, pk) for i, p, pk in c.execute("SELECT id, path, pack FROM files")}
            c.execute("DELETE FROM ssamples"); c.execute("DELETE FROM stags")
            votes, bought = {}, {}
            for r in rows:
                hit = ids.get(r["local_path"])
                if not hit:
                    continue
                fid, ourpack = hit
                key = None
                if r["audio_key"]:
                    k = r["audio_key"].strip()
                    key = k[:1].upper() + k[1:] + {"minor": "min", "major": "maj"}.get(r["chord_type"] or "", "")
                tags = (r["tags"] or "").replace(",", " , ")
                c.execute("INSERT INTO ssamples VALUES(?,?,?,?,?)", (fid, r["tags"] or "", r["sample_type"] or "",
                                                                    r["bpm"] or None, key))
                if tags.strip():
                    c.execute("INSERT INTO stags(rowid, tags) VALUES(?,?)", (fid, tags))
                if r["pack_uuid"]:
                    v = votes.setdefault(ourpack, {})
                    v[r["pack_uuid"]] = v.get(r["pack_uuid"], 0) + 1
                    if r["purchased_at"]:
                        bought[ourpack] = max(bought.get(ourpack, ""), str(r["purchased_at"]))
            c.execute("DELETE FROM spacks")
            for ourpack, v in votes.items():
                uuid = max(v, key=v.get)
                p = packs.get(uuid) or {}
                c.execute("INSERT INTO spacks VALUES(?,?,?,?,?,?,?,?,?)",
                          (ourpack, uuid, p.get("name"), p.get("cover_url"), p.get("genre"), p.get("provider_name"),
                           p.get("description"), p.get("permalink"), bought.get(ourpack)))
            self._apply_splice(c)
            c.execute("INSERT OR REPLACE INTO kv VALUES('splice_mtime', ?)", (stamp,))
            n = c.execute("SELECT COUNT(*) FROM ssamples").fetchone()[0]
        return {"db": path, "samples": n, "packs": len(votes)}

    @staticmethod
    def _apply_splice(c):
        """Splice knows loop vs one-shot, tempo and key for its own files: fill what the name didn't say."""
        c.execute("UPDATE files SET kind=(SELECT stype FROM ssamples s WHERE s.id=files.id) "
                  "WHERE (kind IS NULL OR kind='') AND id IN (SELECT id FROM ssamples WHERE stype IN ('loop','oneshot'))")
        c.execute("UPDATE files SET bpm=(SELECT bpm FROM ssamples s WHERE s.id=files.id) "
                  "WHERE bpm IS NULL AND id IN (SELECT id FROM ssamples WHERE bpm > 0) "
                  "AND (SELECT stype FROM ssamples s WHERE s.id=files.id)='loop'")
        c.execute("UPDATE files SET key=(SELECT key FROM ssamples s WHERE s.id=files.id) "
                  "WHERE key IS NULL AND id IN (SELECT id FROM ssamples WHERE key IS NOT NULL)")

    @staticmethod
    def _index_terms(c, fid, name, rel, typ, part, kind):
        words = TOKEN_SPLIT.split(rel.replace(os.sep, " ")) + [typ, part, kind]
        c.execute("INSERT OR REPLACE INTO fts(rowid,name,words) VALUES(?,?,?)",
                  (fid, name, " ".join(w for w in words if w)))

    def start_scan(self):
        with self.lock:
            if self.scan["running"]:
                return dict(self.scan)
            self.scan = {"running": True, "done": 0, "total": 0, "added": 0, "error": None,
                         "at": self.scan["at"]}
            threading.Thread(target=self._scan, daemon=True).start()
            return dict(self.scan)

    def auto_scan(self, stop, interval=300):
        while not stop.is_set():
            if self.roots():
                self.start_scan()
            stop.wait(interval)

    def _scan(self):
        S = self.scan
        try:
            found = []
            unavailable = set()
            for root in self.roots():
                base = os.path.expanduser(root)
                if not os.path.isdir(base):
                    unavailable.add(root)
                    continue
                for dp, dns, fns in os.walk(base, onerror=lambda error: unavailable.add(root)):
                    dns[:] = [d for d in dns if not d.startswith(".")]
                    for fn in fns:
                        if os.path.splitext(fn)[1].lower() in AUDIO_EXT and not fn.startswith("._"):
                            found.append((root, base, os.path.join(dp, fn)))
                    S["total"] = len(found)
            now = time.time()
            with self.con() as c:
                known = {r["path"]: r for r in c.execute("SELECT id, root, path, mtime FROM files")}
                seen = set()
                # a pack is "whole" (bought in full) when it has a vendor folder with categories inside
                whole_cache = {}
                for i, (root, base, path) in enumerate(found, 1):
                    S["done"] = i
                    try:
                        st = os.stat(path)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        unavailable.add(root)
                        continue
                    seen.add(path)
                    prev = known.get(path)
                    if prev and prev["mtime"] == st.st_mtime and prev["root"] == root:
                        continue
                    rel = os.path.relpath(path, base)
                    fn = os.path.basename(path)
                    stem = os.path.splitext(fn)[0]
                    toks = [t for t in TOKEN_SPLIT.split(stem) if t]
                    low, rlow = stem.lower(), rel.lower()
                    typ = classify(low)
                    if typ == "other":
                        typ = classify(rlow)
                    part = next((p for p, rx in DRUM_PART if rx.search(low)), "") if typ in ("drums", "perc") else ""
                    kind = "loop" if re.search(r"loop", rlow) else ("oneshot" if re.search(r"one[_\s-]?shot|oneshot|hit", rlow) else "")
                    pack = self._pack_of(root, rel)
                    pdir = os.path.join(base, "packs", pack) if rel.lower().startswith("packs" + os.sep) else os.path.join(base, rel.split(os.sep)[0])
                    if pdir not in whole_cache:
                        try:
                            subs = [x for x in os.listdir(pdir) if os.path.isdir(os.path.join(pdir, x))]
                            whole_cache[pdir] = int(any(any(os.path.isdir(os.path.join(pdir, s, x)) for x in os.listdir(os.path.join(pdir, s))) for s in subs))
                        except Exception:
                            whole_cache[pdir] = 0
                    secs, ch, rate = audio_info(path)
                    row = (path, root, rel, stem, pack, whole_cache[pdir], typ, part, kind, parse_bpm(toks),
                           parse_key(toks), secs, ch, rate, st.st_size, st.st_mtime, now)
                    cur = c.execute("""INSERT INTO files(path, root, rel, name, pack, whole, type, part, kind, bpm, key,
                                       secs, ch, rate, bytes, mtime, seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                       ON CONFLICT(path) DO UPDATE SET root=excluded.root, rel=excluded.rel, name=excluded.name,
                                       pack=excluded.pack, whole=excluded.whole, type=excluded.type, part=excluded.part,
                                       kind=excluded.kind, bpm=excluded.bpm, key=excluded.key, secs=excluded.secs, ch=excluded.ch,
                                       rate=excluded.rate, bytes=excluded.bytes, mtime=excluded.mtime, seen=excluded.seen
                                       RETURNING id""", row)
                    fid = cur.fetchone()[0]
                    c.execute("UPDATE files SET tename=?, tename2=? WHERE id=?", (te_name(stem), stem.strip().lower()[:16], fid))
                    c.execute("DELETE FROM feats WHERE id=?", (fid,))
                    self._index_terms(c, fid, stem, rel, typ, part, kind)
                    S["added"] += 1
                    if S["added"] % 500 == 0:
                        c.commit()
                gone = [r["id"] for path, r in known.items() if path not in seen and r["root"] not in unavailable]
                for fid in gone:
                    c.execute("DELETE FROM files WHERE id=?", (fid,))
                    c.execute("DELETE FROM fts WHERE rowid=?", (fid,))
                    c.execute("DELETE FROM feats WHERE id=?", (fid,))
                    c.execute("DELETE FROM stags WHERE rowid=?", (fid,))
                    c.execute("DELETE FROM ssamples WHERE id=?", (fid,))
                if gone or S["added"]:
                    self._packstats(c)
            try:
                S["splice"] = self.splice_sync(force=bool(S["added"] or gone))
            except Exception as e:
                S["splice"] = {"error": str(e)}
            S["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            S["error"] = str(e)
        finally:
            with self.lock:
                S["running"] = False
                if self.rescan_pending:
                    self.rescan_pending = False
                    self.start_scan()

    # ---------- queries ----------
    def search(self, q="", type_="", kind="", part="", bpm_min=None, bpm_max=None, key="", pack="",
               whole=False, fav=False, tag="", sort="name", limit=100, offset=0, device=None, ids=None):
        where, args = [], []
        if ids is not None:
            where.append("f.id IN (%s)" % ",".join(str(int(i)) for i in ids) if ids else "0")
        if q.strip():
            terms = [re.sub(r'[^\w#]', '', t) for t in q.split()]
            terms = [t for t in terms if t]
            for t in terms:   # every word must hit the name/path or Splice's own tags
                where.append("f.id IN (SELECT rowid FROM fts WHERE fts MATCH ? UNION SELECT rowid FROM stags WHERE stags MATCH ?)")
                args += [f'"{t}"*', f'"{t}"*']
        for col, val in (("f.type", type_), ("f.kind", kind), ("f.part", part), ("f.pack", pack)):
            if val:
                where.append(f"{col} = ?"); args.append(val)
        if key:
            where.append("f.key = ?"); args.append(key)
        if bpm_min:
            where.append("f.bpm >= ?"); args.append(int(bpm_min))
        if bpm_max:
            where.append("f.bpm <= ?"); args.append(int(bpm_max))
        if whole:
            where.append("p.whole = 1")
        if fav:
            where.append("m.fav = 1")
        if tag:
            where.append("(' ' || m.tags || ' ') LIKE ?"); args.append(f"% {tag} %")
        order = {"name": "f.name COLLATE NOCASE", "bpm": "f.bpm IS NULL, f.bpm", "len": "f.secs IS NULL, f.secs",
                 "pack": "f.pack COLLATE NOCASE, f.name COLLATE NOCASE", "new": "f.mtime DESC"}.get(sort, "f.name COLLATE NOCASE")
        sql = ("SELECT f.id, f.name, f.tename, f.tename2, "
               "(SELECT COUNT(*) FROM files x WHERE x.tename2=f.tename2) dup2, f.pack, COALESCE(p.whole,0) whole, f.type, f.part, f.kind, f.bpm, f.key, f.secs, f.ch, f.rate, f.bytes, f.rel,"
               " COALESCE(m.fav,0) fav, COALESCE(m.tags,'') tags, COALESCE(m.sent,'') sent,"
               " COALESCE((SELECT tags FROM ssamples s WHERE s.id=f.id),'') stags"
               " FROM files f LEFT JOIN marks m ON m.path = f.path LEFT JOIN packs p ON p.pack = f.pack"
               + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {order} LIMIT ? OFFSET ?")
        with self.con() as c:
            rows = [dict(r) for r in c.execute(sql, args + [int(limit), int(offset)])]
            total = c.execute("SELECT COUNT(*) FROM files f LEFT JOIN marks m ON m.path = f.path LEFT JOIN packs p ON p.pack = f.pack"
                              + (" WHERE " + " AND ".join(where) if where else ""), args).fetchone()[0]
        dev = device or {}
        with self.con() as c:
            tw = _Twins(c)
            for r in rows:
                r["on"] = device_slots(r.pop("tename") or "", r.pop("tename2") or "", r.pop("dup2"), r.get("secs"), dev, tw)
        return {"rows": rows, "total": total}

    def device_matches(self, device_sounds):
        """For each sound on the device, the library file it most likely came from."""
        out = {}
        with self.con() as c:
            for snd in device_sounds:
                n = (snd.get("name") or "").strip().lower()
                if not n:
                    continue
                dev = {n: [(snd["slot"], snd.get("seconds"))]}
                for r in c.execute("SELECT f.id, f.name, f.pack, f.secs, f.tename, f.tename2, COALESCE(p.whole,0) whole, "
                                   "(SELECT COUNT(*) FROM files x WHERE x.tename2=f.tename2) dup2 FROM files f "
                                   "LEFT JOIN packs p ON p.pack=f.pack WHERE f.tename=? OR f.tename2=? "
                                   "ORDER BY whole DESC", (n, n)):
                    if device_slots(r["tename"], r["tename2"], r["dup2"], r["secs"], dev, _Twins(c)):
                        out[snd["slot"]] = {k: r[k] for k in ("id", "name", "pack", "secs", "whole")}
                        break
        return out

    # ---------- similar sounds ----------
    feat = {"running": False, "done": 0, "total": 0, "error": None, "python": None}

    @staticmethod
    def feature_python(calibration=False):
        for p in (str(SOURCE_DIR.parent / ".venv/bin/python"), str(LIBRARY_DIR / "analysis-env/bin/python"), sys.executable,
                  "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
            if os.path.exists(p):
                try:
                    import subprocess
                    imports = "import numpy" if calibration else "import librosa, numpy"
                    r = subprocess.run([p, "-c", imports], capture_output=True, timeout=60)
                    if r.returncode == 0:
                        return p
                except Exception:
                    pass
        return None

    def start_features(self, here):
        if self.feat["running"]:
            return self.feat
        self.feat = {"running": True, "done": 0, "total": 0, "error": None, "python": None}
        threading.Thread(target=self._features, args=(here,), daemon=True).start()
        return self.feat

    def _features(self, here):
        import subprocess
        F = self.feat
        try:
            py = self.feature_python()
            F["python"] = py
            if not py:
                raise RuntimeError("Install librosa and numpy in the project .venv to analyse samples")
            with self.con() as c:
                todo = c.execute("SELECT f.id, f.path FROM files f LEFT JOIN feats x ON x.id=f.id WHERE x.id IS NULL "
                                 "ORDER BY f.secs IS NULL, f.secs").fetchall()
            F["total"] = len(todo)
            proc = subprocess.Popen([py, os.path.join(here, "features.py")], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            def feed():
                for fid, path in todo:
                    if not F["running"]:
                        break
                    proc.stdin.write(json.dumps([fid, path]) + "\n")
                proc.stdin.close()
            threading.Thread(target=feed, daemon=True).start()
            con = self.con()
            for line in proc.stdout:
                try:
                    fid, vec = json.loads(line)
                except Exception:
                    continue
                con.execute("INSERT OR REPLACE INTO feats(id, vec) VALUES(?,?)", (fid, json.dumps(vec) if vec else ""))
                F["done"] += 1
                if F["done"] % 200 == 0:
                    con.commit()
            con.commit(); con.close()
            self._norm = None
        except Exception as e:
            F["error"] = str(e)
        finally:
            F["running"] = False

    _norm = None

    def similar(self, fid, n=60):
        """Nearest files by sound, cosine on z-scored features."""
        with self.con() as c:
            count = c.execute("SELECT COUNT(*) FROM feats WHERE vec != ''").fetchone()[0]
            if not count:
                return []
            if not self._norm or self._norm[0] != count:       # z-scored unit vectors, kept until the set changes
                rows = [(r[0], json.loads(r[1])) for r in c.execute("SELECT id, vec FROM feats WHERE vec != ''")]
                dims = len(rows[0][1])
                mean = [sum(v[i] for _, v in rows) / len(rows) for i in range(dims)]
                sd = [max(1e-6, (sum((v[i] - mean[i]) ** 2 for _, v in rows) / len(rows)) ** 0.5) for i in range(dims)]
                z = {}
                for i, v in rows:
                    zz = [(x - mean[k]) / sd[k] for k, x in enumerate(v)]
                    nn = sum(x * x for x in zz) ** 0.5 or 1
                    z[i] = [x / nn for x in zz]
                self._norm = (count, z)
        z = self._norm[1]
        if fid not in z:
            return []
        q = z[fid]
        scores = [(sum(a * b for a, b in zip(q, v)), i) for i, v in z.items() if i != fid]
        scores.sort(reverse=True)
        return [(i, round(s, 3)) for s, i in scores[:n]]

    def facets(self):
        with self.con() as c:
            return {
                "count": c.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                "types": dict(c.execute("SELECT type, COUNT(*) FROM files GROUP BY type ORDER BY 2 DESC").fetchall()),
                "packs": [dict(r) for r in c.execute("SELECT pack, whole, n FROM packs ORDER BY whole DESC, n DESC")],
                "keys": [r[0] for r in c.execute("SELECT DISTINCT key FROM files WHERE key IS NOT NULL ORDER BY key")],
                "tags": sorted({t for (s,) in c.execute("SELECT tags FROM marks WHERE tags != ''") for t in s.split()}),
                "roots": self.roots(), "scan": self.scan, "feat": self.feat,
                "feats": c.execute("SELECT COUNT(*) FROM feats WHERE vec != ''").fetchone()[0],
                "feats_tried": c.execute("SELECT COUNT(*) FROM feats").fetchone()[0],
            }

    def packs_detail(self, device=None):
        """Whole packs with what is inside: types, tempo span, keys, loops vs one-shots,
        stars, and how many of its files already sit on the device."""
        dev = device or {}
        out = []
        with self.con() as c:
            tw = _Twins(c)
            for p in c.execute("SELECT pack, n FROM packs WHERE whole=1 ORDER BY n DESC").fetchall():
                name = p["pack"]
                types = c.execute("SELECT type, COUNT(*) FROM files WHERE pack=? GROUP BY type ORDER BY 2 DESC", (name,)).fetchall()
                kinds = dict(c.execute("SELECT COALESCE(kind,''), COUNT(*) FROM files WHERE pack=? GROUP BY 1", (name,)).fetchall())
                bpm = c.execute("SELECT MIN(bpm), MAX(bpm) FROM files WHERE pack=? AND bpm>0", (name,)).fetchone()
                keys = [r[0] for r in c.execute("SELECT key FROM files WHERE pack=? AND key IS NOT NULL GROUP BY key "
                                                 "ORDER BY COUNT(*) DESC LIMIT 4", (name,))]
                fav, sent = c.execute("SELECT COALESCE(SUM(m.fav),0), COALESCE(SUM(m.sent != ''),0) FROM files f "
                                      "JOIN marks m ON m.path=f.path WHERE f.pack=?", (name,)).fetchone()
                on = 0
                if dev:
                    on = sum(1 for t1, t2, d2, secs in c.execute(
                        "SELECT tename, tename2, (SELECT COUNT(*) FROM files x WHERE x.tename2=f.tename2), secs "
                        "FROM files f WHERE pack=?", (name,)) if (t1 in dev or t2 in dev) and device_slots(t1, t2, d2, secs, dev, tw))
                mt = c.execute("SELECT MAX(mtime) FROM files WHERE pack=?", (name,)).fetchone()[0]
                sp = c.execute("SELECT * FROM spacks WHERE pack=?", (name,)).fetchone()
                sp = dict(sp) if sp else {}
                out.append({"pack": name, "n": p["n"], "types": [[t or "other", k] for t, k in types],
                            "title": sp.get("title") or name, "cover": sp.get("cover"), "genre": sp.get("genre"),
                            "label": sp.get("label"), "descr": sp.get("descr"), "link": sp.get("link"),
                            "bought": sp.get("bought"),
                            "loops": kinds.get("loop", 0), "oneshots": kinds.get("oneshot", 0),
                            "bpm": [bpm[0], bpm[1]] if bpm[0] else None, "keys": keys,
                            "fav": fav, "sent": sent, "on": on, "mtime": mt})
        return out

    def cover_url(self, pack):
        with self.con() as c:
            r = c.execute("SELECT cover FROM spacks WHERE pack=?", (pack,)).fetchone()
        return r[0] if r and r[0] else None

    def path_of(self, fid):
        with self.con() as c:
            r = c.execute("SELECT path FROM files WHERE id=?", (int(fid),)).fetchone()
        return r[0] if r else None

    def mark(self, fid, fav=None, tags=None, sent=None):
        path = self.path_of(fid)
        if not path:
            raise KeyError("no such file")
        with self.con() as c:
            c.execute("INSERT OR IGNORE INTO marks(path) VALUES(?)", (path,))
            if fav is not None:
                c.execute("UPDATE marks SET fav=? WHERE path=?", (1 if fav else 0, path))
            if tags is not None:
                clean = " ".join(sorted({re.sub(r"[^\w#+-]", "", t.lower()) for t in str(tags).split()} - {""}))
                c.execute("UPDATE marks SET tags=? WHERE path=?", (clean, path))
            if sent is not None:
                c.execute("UPDATE marks SET sent=? WHERE path=?", (str(sent)[:40], path))
            r = c.execute("SELECT fav, tags, sent FROM marks WHERE path=?", (path,)).fetchone()
        return dict(r)
