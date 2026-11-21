"""Read an EP-133 K.O. II project archive (the .tar the TE backup pulls).

Layout, as found on OS 2.5.1:

  patterns/<g><nn>   header  [0x00, bars, count lo, count hi]
                     then `count` 8-byte events:
                       u16le  position in ticks (96 per quarter, 384 per 4/4 bar)
                       u8     (pad << 3) | kind      kind 0 = note, 1 = automation
                       u8     note (60 = the sample's own pitch) / param id
                       u8     velocity
                       u16le  duration in ticks
                       u8     flags (not decoded)
  scenes             5-byte header, then 99 x [num, den, A, B, C, D]
                     (time signature + pattern number per group, 0 = none),
                     then a 113-byte tail: [5] the scene the device sits on,
                     [13] the song's length, [14 ..] the song: scene numbers 1..99
                     in play order (a fresh project: length 1, scene 1)
  pads/<g>/pNN       u16le sample slot at 1, u8 amplitude at 16,
                     f32le time-stretch bpm at 12
  settings           f32le project bpm at 4; from 24, 4 x 12 f32le: where each
                     group's faders rest (0..1, -1 untouched), in fader-id order
"""
import io, struct, tarfile

TICKS_PER_BAR = 384

# the fader parameters as printed above the pads, read left to right, top to bottom
# (7 8 9 / 4 5 6 / 1 2 3 / . 0 ENTER). The automation id is the index here: the
# recorded values back it (LPF sits high, HPF low, PITCH and PAN around centre).
FADER_LABELS = ["LEVEL", "PITCH", "TIME", "LPF", "HPF", "FX", "ATK", "REL", "PAN", "TUNE", "VEL", "MOD"]
FADER_PADS = ["7", "8", "9", "4", "5", "6", "1", "2", "3", ".", "0", "ENTER"]


def fader_label(par, names=None):
    names = names or {}
    return names.get(str(par)) or (FADER_LABELS[par] if 0 <= par < 12 else f"param {par}")


def parse_pattern(b):
    if len(b) < 4:
        return {"bars": 0, "notes": [], "automation": 0}
    bars, count = b[1], b[2] | (b[3] << 8)
    notes, auto = [], []
    for i in range(count):
        r = b[4 + i * 8: 12 + i * 8]
        if len(r) < 8:
            break
        kind, t = r[2] & 7, r[0] | (r[1] << 8)
        if kind == 1:
            # fader automation for the whole group: [tick, param id, value 0..32767, flag byte, position]
            auto.append([t, r[3], r[5] | (r[6] << 8), r[7], i])
            continue
        if kind != 0:
            continue
        # f: the last byte, not decoded, kept as it was; i: position in the file (keeps same-tick order)
        notes.append({"t": t, "pad": r[2] >> 3, "note": r[3],
                      "vel": r[4], "dur": r[5] | (r[6] << 8), "f": r[7], "i": i})
    return {"bars": bars, "notes": notes, "auto": auto, "automation": len(auto)}


def parse_scenes(b):
    out = []
    for i in range(99):
        o = 5 + i * 6
        r = b[o:o + 6]
        if len(r) < 6:
            break
        num, den, a, bb, c, d = r
        if not (a or bb or c or d):
            continue
        out.append({"n": i + 1, "sig": [num, den], "A": a, "B": bb, "C": c, "D": d})
    return out


SONG_AT = 5 + 99 * 6 + 13                                   # song length, then the scene numbers


def parse_song(b):
    """the song mode list: scene numbers in play order (repeats allowed), [] if unreadable"""
    if len(b) < SONG_AT + 1:
        return []
    n = b[SONG_AT]
    return [x for x in b[SONG_AT + 1:SONG_AT + 1 + n] if 1 <= x <= 99] if 1 <= n <= 99 else []


PLAYMODES = {0: "oneshot", 1: "key", 2: "legato"}


def parse_pad(b):
    """The 27-byte pad file. Offsets checked against what the device reports for the
    same pads (its metadata names in brackets):
      1-2 sym, 4-7 start [sample.start], 8-11 end [sample.end], 12-15 f32 the sample's
      bpm for time stretch, 16 amplitude 0..200, 17 pitch semitones (signed) + 26 its
      hundredths [sound.pitch], 18 pan -16..16, 19 attack, 20 release (0..255),
      21 time.mode (1 = bpm), 22 mute group, 23 play mode (0 one-shot, 1 key, 2 legato),
      24 root note."""
    if len(b) < 17:
        return {}
    out = {"sym": b[1] | (b[2] << 8), "amp": b[16],
           "stretch_bpm": round(struct.unpack_from("<f", b, 12)[0], 2)}
    if len(b) >= 27:
        s8 = lambda v: v - 256 if v > 127 else v
        out.update({"start": struct.unpack_from("<I", b, 4)[0], "end": struct.unpack_from("<I", b, 8)[0],
                    "pitch": round(s8(b[17]) + b[26] / 100, 2), "pan": s8(b[18]),
                    "attack": b[19], "release": b[20], "stretch": b[21] == 1, "timemode": b[21],
                    "mute": bool(b[22]), "playmode": PLAYMODES.get(b[23], str(b[23])), "root": b[24] or 60})
    return out


def parse_archive(raw):
    t = tarfile.open(fileobj=io.BytesIO(raw))
    files = {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}
    patterns, pads = {}, {}
    for name, b in files.items():
        if name.startswith("patterns/"):
            patterns[name.split("/")[1]] = parse_pattern(b)
        elif name.startswith("pads/"):
            _, g, p = name.split("/")
            pads.setdefault(g.upper(), {})[p[1:]] = parse_pad(b)
    bpm = None
    faders = {}
    if "settings" in files and len(files["settings"]) >= 8:
        bpm = round(struct.unpack_from("<f", files["settings"], 4)[0], 2)
    if "settings" in files and len(files["settings"]) >= 24 + 48 * 4:
        # where each group's twelve faders rest (0..1, -1 = never touched), same order as the ids
        vals = struct.unpack_from("<48f", files["settings"], 24)
        for gi, g in enumerate("ABCD"):
            faders[g] = {str(i): round(vals[gi * 12 + i], 4) for i in range(12) if vals[gi * 12 + i] >= 0}
    output = None
    fxs = files.get("fx_settings", b"")
    if len(fxs) >= 160:
        drive, speed, length, shape = struct.unpack_from("<4f", fxs, 136)
        groups = struct.unpack_from("<4H", fxs, 152)
        knobs = struct.unpack_from("<32f", fxs, 8)          # FX type t: knob X at [t], knob Y at [16 + t]
        output = {"fx": fxs[4], "fxknobs": {str(t): [round(knobs[t], 4), round(knobs[16 + t], 4)] for t in range(1, 7)},
                  "comp": {"drive": round(drive, 4), "speed": round(speed, 4)},
                  "sc": {"length": round(length, 4), "shape": round(shape, 4),
                         "sources": {g: [p + 1 for p in range(12) if v >> p & 1] for g, v in zip("ABCD", groups) if v & 0xFFF},
                         "dest": [g for g, v in zip("ABCD", groups) if v & 0x8000]}}
    return {"bpm": bpm, "ticks_per_bar": TICKS_PER_BAR, "faders": faders, "output": output,
            "scenes": parse_scenes(files.get("scenes", b"")), "song": parse_song(files.get("scenes", b"")),
            "patterns": patterns, "pads": pads}


if __name__ == "__main__":
    import sys, json
    d = parse_archive(open(sys.argv[1], "rb").read())
    print(json.dumps({"bpm": d["bpm"], "scenes": d["scenes"][:5],
                      "patterns": {k: (v["bars"], len(v["notes"]), v["automation"]) for k, v in sorted(d["patterns"].items())},
                      "padsA": d["pads"].get("A")}, indent=None)[:3000])


# ---------------------------------------------------------------- song layout

GROUPS = "ABCD"


def layout(arr):
    """Scenes end to end: [{n, start (bars), bars, A..D}] and the total in bars."""
    out, bar = [], 0
    for sc in arr["scenes"]:
        bars = 0
        for g in GROUPS:
            p = arr["patterns"].get(f"{g.lower()}{sc[g]:02d}") if sc[g] else None
            if p and p["bars"] > bars:
                bars = p["bars"]
        bars = bars or 1
        out.append(dict(sc, start=bar, bars=bars))
        bar += bars
    return out, bar


def song_events(arr):
    """Every note and automation point of the song, patterns looped inside their scene.
    notes: (tick, group, pad, note, vel, dur); auto: (tick, group, param, value)"""
    scenes, total = layout(arr)
    notes, auto = [], []
    for sc in scenes:
        s0, s1 = sc["start"] * TICKS_PER_BAR, (sc["start"] + sc["bars"]) * TICKS_PER_BAR
        for g in GROUPS:
            if not sc[g]:
                continue
            p = arr["patterns"].get(f"{g.lower()}{sc[g]:02d}")
            if not p:
                continue
            plen = max(1, p["bars"]) * TICKS_PER_BAR
            rep = 0
            while rep * plen < s1 - s0:
                base = s0 + rep * plen
                for n in p["notes"]:
                    t = base + n["t"]
                    if t < s1:
                        notes.append((t, g, n["pad"], n["note"], n["vel"], max(1, n["dur"])))
                for t, par, val, *_ in p.get("auto", []):
                    if base + t < s1:
                        auto.append((base + t, g, par, val))
                rep += 1
    notes.sort(); auto.sort()
    return scenes, total, notes, auto


# ---------------------------------------------------------------- MIDI export

def _vlq(n):
    if n < 0:
        raise ValueError("negative MIDI time")
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F)); n >>= 7
    return bytes(reversed(out))


def _track(events, name, end_tick=0):
    """events: [(tick, order, bytes)] -> an MTrk chunk"""
    body = bytearray(b"\x00\xff\x03" + _vlq(len(name.encode())) + name.encode())
    last = 0
    for t, _o, data in sorted(events, key=lambda e: (e[0], e[1])):
        body += _vlq(t - last) + data
        last = t
    body += _vlq(max(0, end_tick - last)) + b"\xff\x2f\x00"
    return b"MTrk" + len(body).to_bytes(4, "big") + bytes(body)


def param_cc(param):
    """Fader parameter id -> (MSB cc, LSB cc). 20..31 are undefined controllers;
    +32 carries the low bits, so the device's 15-bit value survives as 14 bits."""
    return 20 + (param % 12), 52 + (param % 12)


def to_midi(arr, pad_names=None, param_names=None, bpm=None, pad_cc=False):
    """Format-1 MIDI at the device's own 96 ppq: a conductor track with tempo and a
    marker per scene, one bus track per group carrying its fader automation as CCs,
    then one track per pad that plays (note 60 = the sample's own pitch)."""
    pad_names = pad_names or {}
    param_names = param_names or {}
    bpm = bpm or arr.get("bpm") or 120
    scenes, total, notes, auto = song_events(arr)
    tracks = []
    tempo = int(round(60_000_000 / bpm))
    cond = [(0, 0, b"\xff\x51\x03" + tempo.to_bytes(3, "big")),
            (0, 0, b"\xff\x58\x04\x04\x02\x18\x08")]
    for sc in scenes:
        txt = f"Scene {sc['n']}  " + " ".join(f"{g}{sc[g]:02d}" if sc[g] else f"{g}--" for g in GROUPS)
        if sc.get("label"):
            txt = f"Scene {sc['n']:02d} · {sc['label']}"
        cond.append((sc["start"] * TICKS_PER_BAR, 1, b"\xff\x06" + _vlq(len(txt.encode())) + txt.encode()))
    tracks.append(_track(cond, f"EP-133 P{arr.get('project', '')} @ {bpm:g} bpm", total * TICKS_PER_BAR))
    for gi, g in enumerate(GROUPS):
        ch = gi
        ga = [a for a in auto if a[1] == g]
        bus_events = []
        if ga:
            ev = bus_events
            used = sorted({a[2] for a in ga})
            for par in used:
                label = fader_label(par, param_names)
                msb, lsb = param_cc(par)
                txt = f"{label} -> CC{msb} (+CC{lsb})"
                ev.append((0, 0, b"\xff\x01" + _vlq(len(txt.encode())) + txt.encode()))
            for t, _g, par, val in ga:
                v14 = min(16383, val >> 1)
                msb, lsb = param_cc(par)
                ev.append((t, 1, bytes([0xB0 | ch, msb, v14 >> 7])))
                ev.append((t, 2, bytes([0xB0 | ch, lsb, v14 & 0x7F])))
            if not pad_cc or not any(n[1] == g for n in notes):
                tracks.append(_track(ev, f"{g} bus - " + " ".join(fader_label(p, param_names) for p in used)))
        for pad in range(12):
            pn = [n for n in notes if n[1] == g and n[2] == pad]
            if not pn:
                continue
            ev = list(bus_events) if pad_cc else []
            for t, _g, _p, note, vel, dur in pn:
                ev.append((t, 2, bytes([0x90 | ch, note & 0x7F, max(1, min(127, vel))])))
                ev.append((t + dur, 1, bytes([0x80 | ch, note & 0x7F, 0])))
            label = pad_names.get(f"{g}{pad + 1:02d}", "")
            tracks.append(_track(ev, f"{g}{pad + 1:02d} {label}".strip()))
    head = b"MThd" + (6).to_bytes(4, "big") + (1).to_bytes(2, "big") + \
        len(tracks).to_bytes(2, "big") + (96).to_bytes(2, "big")
    return head + b"".join(tracks)
