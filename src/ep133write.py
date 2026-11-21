"""Write an EP-133 project archive back, in the device's own minimal tar dialect.

The device's tar is plain V7: name, mode, size in octal without zero padding,
no uid/gid/mtime/magic, checksum as octal + NUL + spaces, two zero blocks at
the end. TE's Sample Tool restores a project by PUTting such a tar onto the
project node (flags READ|DIR), so an edited archive goes back the same way.

Nothing here talks to the device; it only turns archives into members and back,
byte for byte (see selftest()).
"""
import struct

BLOCK = 512


def read_members(raw):
    """[{name, type, mode, data}] in archive order."""
    out, off = [], 0
    while off + BLOCK <= len(raw):
        h = raw[off:off + BLOCK]
        if not any(h):
            if any(raw[off:]):
                raise ValueError("Unexpected data after the project's end marker")
            return out
        checksum = int(h[148:156].split(b"\0", 1)[0].strip() or b"0", 8)
        if sum(h[:148]) + 8 * ord(" ") + sum(h[156:]) != checksum:
            raise ValueError("Damaged project header")
        name = h[:100].split(b"\0", 1)[0].decode()
        mode = h[100:108]
        sz = h[124:136].split(b"\0", 1)[0].strip()
        size = int(sz, 8) if sz else 0
        if size < 0 or off + BLOCK + size > len(raw):
            raise ValueError("Incomplete project member: " + name)
        typ = h[156:157].decode() or "0"
        data = raw[off + BLOCK: off + BLOCK + size]
        out.append({"name": name, "type": typ, "mode": mode, "data": data})
        off += BLOCK + ((size + BLOCK - 1) // BLOCK) * BLOCK
    raise ValueError("Incomplete project archive (missing end marker)")


def _header(m):
    h = bytearray(BLOCK)
    name = m["name"].encode()
    if len(name) > 99:
        raise ValueError("name too long: " + m["name"])
    h[:len(name)] = name
    h[100:108] = m.get("mode") or (b"0000755\0" if m["type"] == "5" else b"0000644\0")
    if m["type"] != "5":
        s = format(len(m["data"]), "o").encode()
        h[124:124 + len(s)] = s
    h[156:157] = m["type"].encode()
    h[148:156] = b" " * 8
    ck = format(sum(h), "o").encode() + b"\0"
    h[148:156] = ck.ljust(8, b" ")
    return bytes(h)


def write_members(members):
    out = bytearray()
    for m in members:
        out += _header(m)
        d = m["data"] if m["type"] != "5" else b""
        out += d + b"\0" * ((-len(d)) % BLOCK)
    out += b"\0" * (2 * BLOCK)
    return bytes(out)


# ---------- patterns ----------
def pattern_records(b):
    """header fields + the raw 8-byte records actually present (the stored count can run one past them)"""
    n = min(b[2] | (b[3] << 8), (len(b) - 4) // 8)
    return {"lead": b[0], "bars": b[1], "records": [bytes(b[4 + i * 8: 12 + i * 8]) for i in range(n)]}


def pattern_bytes(p):
    recs = sorted(p["records"], key=lambda r: r[0] | (r[1] << 8))          # the device keeps them in time order
    return bytes([p.get("lead", 0), p["bars"]]) + struct.pack("<H", len(recs)) + b"".join(recs)


def note_record(t, pad, note, vel, dur, flag=0):
    return struct.pack("<HBBBHB", t, (pad << 3) | 0, note, vel, dur, flag)


def auto_record(t, param, value, flag=0):
    return struct.pack("<HBBBHB", t, 1, param, 0, value, flag)


# ---------- scenes ----------
def scenes_rows(b):
    return {"head": bytes(b[:5]), "rows": [list(b[5 + i * 6: 11 + i * 6]) for i in range(99)], "tail": bytes(b[5 + 99 * 6:])}


def scenes_bytes(s):
    rows = [list(r) for r in s["rows"]][:99]
    rows += [[0] * 6 for _ in range(99 - len(rows))]
    return s["head"] + b"".join(bytes(r) for r in rows) + s["tail"]


# ---------- an edit, applied to the archive it was made on ----------
GROUPS = "abcd"


def _pattern_names(members):
    return {m["name"][len("patterns/"):] for m in members if m["name"].startswith("patterns/") and m["type"] != "5"}


def apply_patch(raw, patch):
    """patch = {
         "scenes":   [[num, den, A, B, C, D], ...]   up to 99 rows, the rest become empty,
         "cursor":   n                               the scene the device should sit on (1..99), optional,
         "song":     [1, 2, 1, 3]                    song mode: scene numbers in play order (1..99 of them),
         "patterns": {"c01": {"bars": 4, "records": ["hex16", ...]}, ...}   new or replaced,
         "drop_patterns": ["c07", ...],
         "bpm": 124.0,
         "output": {"comp": {"drive": 0..1, "speed": 0..1},
                    "sc": {"length": 0..1, "shape": 0..1, "sources": {"A": [10, 11]}, "dest": ["B", "C"]}} }
    Returns the new archive. Refuses anything the device could not hold."""
    mem = read_members(raw)
    byname = {m["name"]: m for m in mem}
    names = _pattern_names(mem)

    for name in patch.get("drop_patterns") or []:
        key = "patterns/" + name
        if key in byname:
            mem.remove(byname.pop(key))
            names.discard(name)

    for name, p in (patch.get("patterns") or {}).items():
        if len(name) != 3 or name[0] not in GROUPS or not name[1:].isdigit() or not 1 <= int(name[1:]) <= 99:
            raise ValueError(f"bad pattern name {name!r}")
        bars = int(p["bars"])
        if not 1 <= bars <= 99:
            raise ValueError(f"{name}: bars must be 1..99")
        recs = [bytes.fromhex(r) for r in p.get("records", [])]
        if any(len(r) != 8 for r in recs):
            raise ValueError(f"{name}: every event is 8 bytes")
        limit = bars * 384
        for r in recs:
            t = r[0] | (r[1] << 8)
            if t >= limit:
                raise ValueError(f"{name}: event at tick {t} is past the pattern's {bars} bars")
            if (r[2] & 7) not in (0, 1) or (r[2] >> 3) > 11:
                raise ValueError(f"{name}: unknown event kind / pad")
        if len(recs) > 4000:
            raise ValueError(f"{name}: too many events")
        data = pattern_bytes({"lead": 0, "bars": bars, "records": recs})
        key = "patterns/" + name
        if key in byname:
            byname[key]["data"] = data
        else:
            m = {"name": key, "type": "0", "mode": b"0000644\0", "data": data}
            # keep the directory sorted, as the device writes it
            after = [i for i, x in enumerate(mem) if x["name"].startswith("patterns/") and x["name"] < key]
            at = (after[-1] + 1) if after else next(i for i, x in enumerate(mem) if x["name"] == "patterns") + 1
            mem.insert(at, m); byname[key] = m; names.add(name)

    if "scenes" in patch:
        sc = byname.get("scenes")
        if not sc:
            raise ValueError("archive has no scenes file")
        cur = scenes_rows(sc["data"])
        rows = [list(map(int, r)) for r in patch["scenes"]]
        if len(rows) > 99:
            raise ValueError("99 scenes at most")
        for i, r in enumerate(rows, 1):
            if len(r) != 6 or not 1 <= r[0] <= 16 or r[1] not in (1, 2, 4, 8, 16):
                raise ValueError(f"scene {i}: bad time signature {r[:2]}")
            for gi, n in enumerate(r[2:]):
                # a pattern with nothing recorded has no file at all; the device plays it as silence
                if not 0 <= n <= 99:
                    raise ValueError(f"scene {i}: group {GROUPS[gi].upper()} pattern {n} is out of range")
        rows += [[4, 4, 0, 0, 0, 0] for _ in range(99 - len(rows))]
        tail = bytearray(cur["tail"])
        if patch.get("cursor") and len(tail) > 5:
            tail[5] = max(1, min(99, int(patch["cursor"])))
        sc["data"] = scenes_bytes({"head": cur["head"], "rows": rows, "tail": bytes(tail)})

    if "song" in patch:
        sc = byname.get("scenes")
        if not sc:
            raise ValueError("archive has no scenes file")
        song = [int(x) for x in patch["song"]]
        if not 1 <= len(song) <= 99:
            raise ValueError("a song holds 1..99 steps")
        cur = scenes_rows(sc["data"])
        for i, n in enumerate(song, 1):
            if not 1 <= n <= 99:
                raise ValueError(f"song step {i}: scene {n} is out of range")
            if not any(cur["rows"][n - 1][2:]):
                raise ValueError(f"song step {i}: scene {n} is empty")
        tail = bytearray(cur["tail"])
        if len(tail) < 14 + 99:
            raise ValueError("scenes file too short for a song")
        tail[13] = len(song)
        tail[14:14 + 99] = bytes(song) + bytes(99 - len(song))
        sc["data"] = scenes_bytes({"head": cur["head"], "rows": cur["rows"], "tail": bytes(tail)})

    if patch.get("output"):
        # fx_settings: @136 compressor drive, @140 speed, @144 sidechain length, @148 shape (f32 0..1),
        # @152 one u16 per group: bit 15 = ducks, bits 0..11 = the pads (files 1..12) that trigger it
        o = patch["output"]
        fx = byname.get("fx_settings")
        if not fx or len(fx["data"]) < 160:
            raise ValueError("this project has no fx_settings yet - touch the FX on the device once")
        b = bytearray(fx["data"])
        comp, sc = o.get("comp") or {}, o.get("sc") or {}
        # master FX: @4 the type (1 delay, 2 reverb, 3 distortion, 4 chorus, 5 filter, 6 compressor);
        # each type keeps its own knobs: X at float [t], Y at float [16 + t], floats from @8
        if o.get("fx") is not None:
            t = int(o["fx"])
            if not 1 <= t <= 6:
                raise ValueError("FX type is 1..6")
            b[4] = t
        for t, xy in (o.get("fxknobs") or {}).items():
            t = int(t)
            if not 1 <= t <= 6 or len(xy) != 2 or any(not 0.0 <= float(v) <= 1.0 for v in xy):
                raise ValueError("FX knobs: type 1..6, values 0..1")
            struct.pack_into("<f", b, 8 + 4 * t, float(xy[0]))
            struct.pack_into("<f", b, 8 + 4 * (16 + t), float(xy[1]))
        for key, off, src in (("drive", 136, comp), ("speed", 140, comp), ("length", 144, sc), ("shape", 148, sc)):
            if key in src:
                v = float(src[key])
                if not 0.0 <= v <= 1.0:
                    raise ValueError(f"{key} must be 0..1")
                struct.pack_into("<f", b, off, v)
        if "sources" in sc or "dest" in sc:
            cur = struct.unpack_from("<4H", b, 152)
            srcs, dest = sc.get("sources"), sc.get("dest")
            for gi, g in enumerate("ABCD"):
                v = cur[gi]
                if srcs is not None:
                    pads = srcs.get(g) or srcs.get(g.lower()) or []
                    if any(not 1 <= int(x) <= 12 for x in pads):
                        raise ValueError("sidechain source pads are 1..12")
                    v = (v & 0x8000) | sum(1 << (int(x) - 1) for x in set(pads))
                if dest is not None:
                    v = (v & 0x0FFF) | (0x8000 if g in [d.upper() for d in dest] else 0)
                struct.pack_into("<H", b, 152 + 2 * gi, v)
        fx["data"] = bytes(b)

    if patch.get("bpm") is not None:
        bpm = float(patch["bpm"])
        if not 1 <= bpm <= 300:
            raise ValueError("bpm out of range")
        st = byname.get("settings")
        b = bytearray(st["data"]); struct.pack_into("<f", b, 4, bpm); st["data"] = bytes(b)

    return write_members(mem)


def diff(a, b):
    """what differs between two archives, member by member"""
    ma = {m["name"]: m["data"] for m in read_members(a)}
    mb = {m["name"]: m["data"] for m in read_members(b)}
    return {"added": sorted(set(mb) - set(ma)), "removed": sorted(set(ma) - set(mb)),
            "changed": sorted(k for k in set(ma) & set(mb) if ma[k] != mb[k])}


def selftest(paths):
    """every archive must come back byte for byte"""
    bad = []
    for p in paths:
        raw = open(p, "rb").read()
        try:
            mem = read_members(raw)
        except ValueError as exc:
            bad.append((str(p), str(exc)))
            continue
        if write_members(mem) != raw:
            bad.append((p, "tar"))
            continue
        for m in mem:
            if m["name"].startswith("patterns/"):
                pr = pattern_records(m["data"])
                again = pattern_bytes(pr)
                n = (m["data"][2] | m["data"][3] << 8)
                if again != m["data"] and 4 + 8 * n == len(m["data"]):
                    bad.append((p, m["name"]))
            if m["name"] == "scenes" and scenes_bytes(scenes_rows(m["data"])) != m["data"]:
                bad.append((p, "scenes"))
    return bad


if __name__ == "__main__":
    import sys
    from paths import DATA_DIR
    files = sys.argv[1:] or sorted((DATA_DIR / "projects_dump").glob("*.tar"))
    bad = selftest(files)
    print(f"{len(files)} archives, {len(bad)} mismatches", bad[:10])
    sys.exit(bool(bad))
