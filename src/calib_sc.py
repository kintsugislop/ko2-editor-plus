"""Measure the K.O. II's sidechain (the duck) on the device itself.

fx_settings, as read off the device (Oct 2026, project 03):
  @4      master FX type
  @8..135 32 floats: the FX knobs
  @136    f32 master compressor drive (knob X), @140 f32 speed (knob Y)
  @144    f32 sidechain length (knob X),         @148 f32 shape (knob Y)
  @152    4 x u16, one per group A..D: bit 15 = this group ducks,
          bits 0..11 = the pads (pad file 1..12) whose hits trigger the duck

The test: pad B01 holds a steady noise, pad A01 is silent; MIDI hits on A01 duck
group B. One write per (length, shape) setting, the station records the K.O.'s
USB output, and this module reads the duck's depth and recovery curve.

  python3 calib_sc.py analyze take.wav plan.json   -> JSON {measured, model, summary}
Only numpy is needed for the analysis."""
import json, random, struct, sys

SR = 46875
LENGTHS = [0.0, 0.25, 0.5, 0.75, 1.0]
SHAPES = [0.0, 0.5, 1.0]


def _pcm(sec, fn):
    n = int(round(sec * SR))
    out = bytearray()
    fade = 0.003 * SR
    for i in range(n):
        g = min(1.0, i / fade, (n - 1 - i) / fade) if fade else 1.0
        out += struct.pack("<h", max(-32767, min(32767, int(round(fn(i) * g * 32767)))))
    return bytes(out)


def sounds():
    rnd = random.Random(4242)
    noise = _pcm(9.0, lambda i: 0.35 * (rnd.random() * 2 - 1))
    trig = _pcm(0.03, lambda i: 0.0)
    return [("cal sc noise", noise), ("cal sc trig", trig)]


def fx_bytes(base, length, shape, sources=None, dest="B", drive=None, speed=None):
    b = bytearray(base) if base and len(base) >= 160 else bytearray(160)
    if not (base and len(base) >= 160):
        b[4] = 1
        for i in range(32):
            struct.pack_into("<f", b, 8 + 4 * i, 0.5)
        struct.pack_into("<2f", b, 136, 0.5, 0.5)
    if drive is not None:
        struct.pack_into("<f", b, 136, drive)
    if speed is not None:
        struct.pack_into("<f", b, 140, speed)
    struct.pack_into("<2f", b, 144, length, shape)
    sources = sources if sources is not None else {"A": [1]}
    for gi, g in enumerate("ABCD"):
        v = 0
        for p in sources.get(g, []):
            v |= 1 << (p - 1)
        if g in dest:
            v |= 0x8000
        struct.pack_into("<H", b, 152 + 2 * gi, v)
    return bytes(b)


def parse_fx(b):
    """the master compressor and the sidechain out of fx_settings"""
    if not b or len(b) < 160:
        return None
    drive, speed, length, shape = struct.unpack_from("<4f", b, 136)
    groups = struct.unpack_from("<4H", b, 152)
    src = {g: [p + 1 for p in range(12) if v >> p & 1] for g, v in zip("ABCD", groups)}
    return {"fx": b[4], "comp": {"drive": round(drive, 4), "speed": round(speed, 4)},
            "sc": {"length": round(length, 4), "shape": round(shape, 4),
                   "sources": {g: v for g, v in src.items() if v},
                   "dest": [g for g, v in zip("ABCD", groups) if v & 0x8000]}}


def build(blank, template, slots, frames, length, shape):
    import ep133write as W
    noise, trig = slots
    mem = W.read_members(blank)
    tpl = {m["name"]: m for m in W.read_members(template)}

    def pad(sym, end):
        b = bytearray(27)
        struct.pack_into("<H", b, 1, sym); struct.pack_into("<I", b, 8, end)
        struct.pack_into("<f", b, 12, 120.0); b[16] = 100; b[20] = 255; b[24] = 60
        return bytes(b)
    want = {("a", 1): pad(trig, frames[1]), ("b", 1): pad(noise, frames[0])}
    pads = []
    for m in mem:
        if m["name"].startswith("pads"):
            if m["type"] == "0":
                _, g, p = m["name"].split("/")
                k = (g, int(p[1:]))
                m = dict(m, data=want.get(k, m["data"]))
            pads.append(m)
    pats = [m for m in mem if m["name"] == "patterns" or m["name"].startswith("patterns/")]
    if not any(m["name"] == "patterns" for m in pats):
        pats = [{"name": "patterns", "type": "5", "mode": b"0000755\0", "data": b""}]
    base = tpl.get("fx_settings", {}).get("data")
    fx = {"name": "fx_settings", "type": "0", "mode": b"0000644\0",
          "data": fx_bytes(base, length, shape, drive=0.5, speed=0.5)}
    scenes = dict(tpl["scenes"]); settings = dict(tpl["settings"])
    st = bytearray(settings["data"])
    for gi in range(4):                                 # faders resting neutral, LEVEL up
        for i in range(12):
            struct.pack_into("<f", st, 24 + 4 * (gi * 12 + i), {0: 1.0, 1: 0.5, 3: 1.0, 4: 0.0, 5: 0.0, 8: 0.5, 9: 0.5}.get(i, -1.0))
    settings["data"] = bytes(st)
    return W.write_members([fx] + pads + pats + [scenes, settings])


# ---------------- reading the recording ----------------
def load(path):
    import wave
    import numpy as np
    with wave.open(path) as w:
        sr, ch, n, sw = w.getframerate(), w.getnchannels(), w.getnframes(), w.getsampwidth()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float64) / 32768
    elif sw == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        x = (((b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)) << 8) >> 8) / 8388608.0
    else:
        x = np.frombuffer(raw, "<f4").astype(np.float64)
    return sr, x.reshape(-1, ch)


def analyze(wav, plan):
    import numpy as np
    sr, x = load(wav)
    mono = np.abs(x).mean(axis=1)
    hop = int(sr * 0.005)
    k = len(mono) // hop
    env = np.sqrt((x[:k * hop].mean(axis=1) ** 2).reshape(k, hop).mean(axis=1))
    out = []
    for run in plan["runs"]:
        # the noise starts at the B hit: find it near where the log says
        t_b = run["t_noise"]
        i0 = int(max(0, t_b - 1.5) / 0.005); i1 = int((t_b + 1.5) / 0.005)
        seg = env[i0:i1]
        on = np.where(seg > 0.02)[0]
        if not len(on):
            out.append(dict(run, error="no noise heard")); continue
        noise_at = (i0 + on[0]) * 0.005
        shift = noise_at - t_b                          # log time -> recording time
        r = dict(run, shift=round(shift, 4), ducks=[])
        for t in run["t_trig"]:
            tt = t + shift
            a = int((tt - 0.4) / 0.005); b0 = int((tt - 0.05) / 0.005)
            base = float(np.median(env[a:b0])) + 1e-9
            c = int(tt / 0.005) - 4
            curve = env[c:c + int(1.6 / 0.005)] / base
            lo = int(np.argmin(curve[:int(0.25 / 0.005)]))
            depth = float(20 * np.log10(max(1e-4, curve[lo])))
            def back_to(db):
                w = np.where(curve[lo:] >= 10 ** (db / 20))[0]
                return round(float((lo + w[0]) * 0.005 - 0.02), 4) if len(w) else None
            r["ducks"].append({"depth_db": round(depth, 2), "t_min": round(lo * 0.005 - 0.02, 4),
                               "to_-12": back_to(-12), "to_-6": back_to(-6), "to_-3": back_to(-3), "to_-1": back_to(-1),
                               "curve": [round(float(v), 3) for v in curve[::2]]})
        out.append(r)
    return {"sr": sr, "runs": out}


def fit(meas):
    rows = []
    for r in meas["runs"]:
        if not r.get("ducks"):
            continue
        d = r["ducks"]
        n = min(len(q["curve"]) for q in d)
        avg = [round(sum(q["curve"][i] for q in d) / len(d), 3) for i in range(n)]
        rows.append({"length": r["length"], "shape": r["shape"], "step": 0.01, "start": -0.02, "gain": avg,
                     "depth_db": round(sum(q["depth_db"] for q in d) / len(d), 2),
                     "to_-3": d[0]["to_-3"], "to_-1": d[0]["to_-1"]})
    return {"curves": rows}


# the default measuring grid: three lengths across the shapes the duck changes most over, and one "off" check
CAL_RUNS = [(L, S) for L in (0.25, 0.5, 1.0) for S in (0.05, 0.1, 0.25, 0.5, 0.75, 1.0)] + [(0.0, 0.5)]


def fit_model(model):
    """gain = (t / T)^p after the hit: T = k * length, p per shape -- what the browser plays (SC_P, scTime)"""
    import numpy as np
    ps = np.exp(np.linspace(np.log(3e-4), np.log(120), 140))
    def err(g, t, T, p):
        m = np.clip(t / T, 0, 1) ** p
        return float(np.mean((m - np.clip(g, 0, 1.2)) ** 2))
    per = []
    for c in model["curves"]:
        if c["length"] <= 0 or c["shape"] <= 0:
            continue
        g = np.array(c["gain"], float); t = c["start"] + np.arange(len(g)) * c["step"]
        keep = t >= 0; g, t = g[keep], t[keep]
        best = None
        for T in np.arange(0.02, 1.2, 0.01):
            for p in ps:
                e = err(g, t, T, p)
                if best is None or e < best[0]:
                    best = (e, T, p)
        per.append({"length": c["length"], "shape": c["shape"], "T": float(best[1]), "p": float(best[2]), "err": best[0]})
    # one T per length for all shapes: the k that lets every curve fit best, each with its own p
    cur = []
    for c in model["curves"]:
        if c["length"] <= 0 or c["shape"] <= 0:
            continue
        g = np.array(c["gain"], float); t = c["start"] + np.arange(len(g)) * c["step"]
        keep = t >= 0
        cur.append((c, g[keep], t[keep]))
    if not cur:
        return None
    best_k = None
    for k in np.arange(0.40, 1.21, 0.01):
        tot = sum(min(err(g, t, k * c["length"], p) for p in ps) for c, g, t in cur)
        if best_k is None or tot < best_k[0]:
            best_k = (tot, float(k))
    k = best_k[1]
    shapes = {}
    for c, g, t in cur:
        p = min(ps, key=lambda p: err(g, t, k * c["length"], p))
        shapes.setdefault(c["shape"], []).append(float(p))
    sp, top = [], 0.0
    for S, v in sorted(shapes.items()):                  # a higher shape never lets go sooner
        top = max(top, float(np.exp(np.median(np.log(v)))))
        sp.append([S, round(top, 5)])
    return {"t_per_length": round(k, 4), "shape_p": sp,
            "curves_fit": per}


def summary(m):
    out = []
    for r in m["curves"]:
        out.append(f"len {r['length']:.2f} shape {r['shape']:.1f}: {r['depth_db']:+.0f} dB, back to -3 dB in "
                   + (f"{r['to_-3'] * 1000:.0f} ms" if r["to_-3"] is not None else "—"))
    return " · ".join(out)


if __name__ == "__main__":
    if sys.argv[1] == "analyze":
        plan = json.load(open(sys.argv[3]))
        meas = analyze(sys.argv[2], plan)
        model = fit(meas)
        model["fit"] = fit_model(model)
        json.dump({"measured": meas, "model": model, "summary": summary(model)}, sys.stdout)
