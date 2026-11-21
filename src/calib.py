"""Measure what the K.O. II's faders really do, on the device itself.

A calibration project plays test sounds (noise, a 440 Hz sine, a gated sine)
while the group's fader automation steps through known raw values, one value a
second. The station records the device's USB output, and this module turns the
recording into scales: for every raw value, the cutoff in Hz, the gain in dB, the
pitch in semitones... Playback, stems and the editor then use those numbers.

Only numpy is needed for the analysis (run with a Python that has it)."""
import json, math, random, struct, sys

BPM = 120.0
STEP = 192                      # ticks per step: half a bar at 120 bpm = 1.0 s
LEAD = 24                       # automation lands this many ticks before its note
SR = 46875
LEVEL, PITCH, TIME, LPF, HPF, FX, ATK, REL, PAN, TUNE = 0, 1, 2, 3, 4, 5, 6, 7, 8, 9
NAMES = ["LEVEL", "PITCH", "TIME", "LPF", "HPF", "FX", "ATK", "REL", "PAN", "TUNE", "VEL", "MOD"]
NEUTRAL = {LEVEL: 32767, PITCH: 16384, LPF: 32767, HPF: 0, PAN: 16384, TUNE: 16384, FX: 0}


# ---------------- test sounds ----------------
def _pcm(sec, fn):
    n = int(round(sec * SR))
    out = bytearray()
    fade = 0.004 * SR
    for i in range(n):
        g = min(1.0, i / fade, (n - 1 - i) / fade)
        out += struct.pack("<h", max(-32767, min(32767, int(round(fn(i) * g * 32767)))))
    return bytes(out)


def sounds():
    rnd = random.Random(12345)
    noise = _pcm(0.8, lambda i: 0.35 * (rnd.random() * 2 - 1))
    sine = _pcm(0.4, lambda i: 0.5 * math.sin(2 * math.pi * 440 * i / SR))
    gate = _pcm(0.5, lambda i: 0.5 * math.sin(2 * math.pi * 440 * i / SR) if (i // int(0.0625 * SR)) % 2 == 0 else 0.0)
    return [("cal noise", noise), ("cal sine", sine), ("cal gate", gate)]


# ---------------- the project ----------------
def raws(n):
    return [round(k * 32767 / (n - 1)) for k in range(n)]


def build(blank, template, slots, frames):
    import ep133write as W
    noise, sine, gate = slots
    groups = {g: {"notes": [], "auto": []} for g in "abcd"}
    plan = {"bpm": BPM, "step_s": STEP / 96 * 60 / BPM, "steps": []}
    step = 0

    def put(g, pad, param, raw, dur, what, sig):
        nonlocal step
        t = step * STEP
        groups[g]["auto"].append((max(0, t - LEAD), param, raw))
        groups[g]["notes"].append((t, pad, dur))
        plan["steps"].append({"step": step, "group": g, "param": param, "raw": raw, "what": what, "signal": sig})
        step += 1

    for g in "abcd":
        for p, v in NEUTRAL.items():
            groups[g]["auto"].append((0, p, v))
    put("a", 0, LPF, 32767, 96, "marker", "noise")                   # a loud first step marks time zero
    for param, n, pad, sig in ((LPF, 17, 0, "noise"), (HPF, 17, 0, "noise"), (LEVEL, 17, 0, "noise"),
                               (PAN, 9, 0, "noise"), (PITCH, 17, 1, "sine"), (TUNE, 25, 1, "sine")):
        for r in raws(n):
            put("a", pad, param, r, 96, NAMES[param], sig)
        groups["a"]["auto"].append((step * STEP - LEAD, param, NEUTRAL[param]))
    for r in raws(17):
        put("b", 0, TIME, r, 96, "TIME", "gate")
    for r in raws(9):
        put("c", 0, ATK, r, 96, "ATK", "noise")
    for r in raws(9):
        put("d", 0, REL, r, 24, "REL", "noise")                        # short note: the release is what's left
    bars = step * STEP // 384 + 2
    plan["bars"] = bars

    mem = W.read_members(blank)
    tpl = {m["name"]: m for m in W.read_members(template)}

    def pad_bytes(sym, end, key_mode):
        b = bytearray.fromhex("0000000000000000000000000000f04264000000ff0000003c0000")
        struct.pack_into("<H", b, 1, sym)
        struct.pack_into("<I", b, 8, end)
        if key_mode:
            b[23] = 1                                  # 'key': the note length gates the sound
        return bytes(b)

    padmap = {("a", 1): (noise, frames[0], False), ("a", 2): (sine, frames[1], False),
              ("b", 1): (gate, frames[2], False), ("c", 1): (noise, frames[0], True), ("d", 1): (noise, frames[0], True)}
    pads = []
    for m in mem:
        if m["name"].startswith("pads"):
            if m["type"] == "0":
                _, g, p = m["name"].split("/")
                if (g, int(p[1:])) in padmap:
                    m = dict(m, data=pad_bytes(*padmap[(g, int(p[1:]))]))
            pads.append(m)
    pats = [{"name": "patterns", "type": "5", "mode": b"0000755\0", "data": b""}]
    for g in "abcd":
        recs = [W.auto_record(t, p, v) for t, p, v in groups[g]["auto"]]
        recs += [W.note_record(t, pad, 60, 100, dur) for t, pad, dur in groups[g]["notes"]]
        pats.append({"name": f"patterns/{g}01", "type": "0", "mode": b"0000644\0",
                     "data": W.pattern_bytes({"lead": 0, "bars": bars, "records": recs})})
    sc = W.scenes_rows(tpl["scenes"]["data"])
    rows = [[4, 4, 1, 1, 1, 1]] + [[4, 4, 0, 0, 0, 0]] * 98
    tail = bytearray(sc["tail"]); tail[5] = 1
    scenes = {"name": "scenes", "type": "0", "mode": b"0000644\0",
              "data": W.scenes_bytes({"head": sc["head"], "rows": rows, "tail": bytes(tail)})}
    st = bytearray(tpl["settings"]["data"])
    struct.pack_into("<f", st, 4, BPM)
    rest = {LEVEL: 1.0, PITCH: 0.5, LPF: 1.0, HPF: 0.0, FX: 0.0, PAN: 0.5, TUNE: 0.5}
    for gi in range(4):
        for i in range(12):
            struct.pack_into("<f", st, 24 + 4 * (gi * 12 + i), rest.get(i, -1.0))
    settings = {"name": "settings", "type": "0", "mode": b"0000644\0", "data": bytes(st)}
    fx = [dict(tpl["fx_settings"])] if "fx_settings" in tpl else []
    return W.write_members(fx + pads + pats + [scenes, settings]), plan


# ---------------- analysis (numpy) ----------------
def analyze(wav_path, plan):
    import wave
    import numpy as np
    with wave.open(wav_path) as w:
        sr, ch, n, sw = w.getframerate(), w.getnchannels(), w.getnframes(), w.getsampwidth()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float64) / 32768
    elif sw == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        x = (((b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)) << 8) >> 8) / 8388608.0
    else:
        x = np.frombuffer(raw, "<f4").astype(np.float64)
    x = x.reshape(-1, ch)
    mono = x.mean(axis=1)
    hop = max(1, int(sr * 0.002))
    env = np.sqrt(np.convolve(mono ** 2, np.ones(hop) / hop, "same"))
    t0 = float(np.argmax(env > 10 ** (-40 / 20)) / sr)
    st = plan["step_s"]

    def seg(y, a, b):
        return y[max(0, int((t0 + a) * sr)):max(0, int((t0 + b) * sr))]

    def welch(y, nper=8192):
        if len(y) < nper:
            nper = 1 << int(math.log2(max(256, len(y))))
        win = np.hanning(nper)
        segs = [y[i:i + nper] * win for i in range(0, len(y) - nper + 1, nper // 2)] or [np.resize(y, nper) * win]
        p = np.mean([np.abs(np.fft.rfft(s)) ** 2 for s in segs], axis=0)
        return np.fft.rfftfreq(nper, 1 / sr), p

    edges = 20 * 2 ** (np.arange(0, 10 * 12 + 1) / 12)             # 1/12-octave bands, 20 Hz .. 20 kHz
    edges = edges[edges < sr / 2 * 0.95]
    centres = np.sqrt(edges[:-1] * edges[1:])

    def bands(f, p):
        return np.array([p[(f >= a) & (f < b)].mean() if ((f >= a) & (f < b)).any() else np.nan for a, b in zip(edges[:-1], edges[1:])])

    def nbins(f):
        return np.array([((f >= a) & (f < b)).sum() for a, b in zip(edges[:-1], edges[1:])])

    def freq(y):
        y = y * np.hanning(len(y))
        n = 1 << (int(np.ceil(np.log2(len(y)))) + 3)
        s = np.abs(np.fft.rfft(y, n))
        k = int(np.argmax(s[5:])) + 5
        a, b_, c = np.log(s[k - 1] + 1e-12), np.log(s[k] + 1e-12), np.log(s[k + 1] + 1e-12)
        d = 0.5 * (a - c) / (a - 2 * b_ + c) if (a - 2 * b_ + c) else 0
        return float((k + d) * sr / n)

    def cross(r, low_pass, nbins):
        """fit an n-pole Butterworth-like response: corner, order (slope = 6n dB/oct), peak above the model"""
        ok = ~np.isnan(r) & (nbins >= 2)
        c, r, w = centres[ok], np.clip(r[ok], -60, 20), np.sqrt(nbins[ok])
        best = None
        grid = 2 ** np.linspace(math.log2(12), math.log2(24000), 400)
        for order in (1, 2, 3, 4):
            ratio = (c[None, :] / grid[:, None]) if low_pass else (grid[:, None] / c[None, :])
            model = np.clip(-10 * np.log10(1 + ratio ** (2 * order)), -60, 0)
            err = (((model - r[None, :]) ** 2) * w[None, :]).sum(axis=1)
            k = int(np.argmin(err))
            if best is None or err[k] < best[0] * 0.9:        # a higher order must fit clearly better
                best = (err[k], float(grid[k]), order, model[k])
        _, fc, order, model = best
        near = (c > fc / 1.41) & (c < fc * 1.41)              # half an octave either side of the corner
        peak = float(np.mean((r - model)[near])) if near.sum() >= 2 else 0.0
        return fc, 6.0 * order, peak

    steps = plan["steps"]
    ref_s = next(s for s in steps if s["what"] == "LPF" and s["raw"] == 32767)
    f, pref = welch(seg(mono, ref_s["step"] * st + 0.1, ref_s["step"] * st + 0.7))
    bref = bands(f, pref)
    sref = next(s for s in steps if s["what"] == "PITCH" and s["raw"] == 16384)
    f_ref = freq(seg(mono, sref["step"] * st + 0.03, sref["step"] * st + 0.3))
    out = {"t0": t0, "sr": sr, "f_ref": f_ref, "params": {}}
    for s in steps:
        name, a = s["what"], s["step"] * st
        if name == "marker":
            continue
        res = {"raw": s["raw"]}
        if name in ("LPF", "HPF"):
            f, p = welch(seg(mono, a + 0.1, a + 0.7))
            with np.errstate(divide="ignore", invalid="ignore"):
                r = 10 * np.log10(bands(f, p) / bref)
            fc, slope, peak = cross(r, name == "LPF", nbins(f))
            res.update({"hz": fc, "slope": slope, "peak": peak})
        elif name == "LEVEL":
            y = seg(mono, a + 0.1, a + 0.7)
            res["db"] = float(20 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-9))
        elif name == "PAN":
            y = seg(x, a + 0.1, a + 0.7)
            res["l"] = float(np.sqrt(np.mean(y[:, 0] ** 2)))
            res["r"] = float(np.sqrt(np.mean(y[:, -1] ** 2)))
        elif name in ("PITCH", "TUNE"):
            fr = freq(seg(mono, a + 0.03, a + 0.25))
            res.update({"hz": fr, "st": float(12 * math.log2(fr / f_ref))})
        elif name == "TIME":
            y = seg(mono, a, a + 0.98)
            h = max(1, int(sr * 0.004))
            e = np.sqrt(np.convolve(y ** 2, np.ones(h) / h, "same"))
            on = e > e.max() * 0.3
            ix = np.where(on)[0]
            rise = np.where(np.diff(on.astype(int)) == 1)[0]
            res.update({"dur": float((ix[-1] - ix[0]) / sr) if len(ix) else 0.0,
                        "gate": float(np.median(np.diff(rise)) / sr) if len(rise) > 2 else None,
                        "hz": freq(seg(mono, a + 0.005, a + 0.055))})
        elif name in ("ATK", "REL"):
            y = seg(mono, a, a + 0.98)
            h = max(1, int(sr * 0.002))
            e = np.sqrt(np.convolve(y ** 2, np.ones(h) / h, "same"))
            pk = e.max() + 1e-12
            if name == "ATK":
                res["s"] = float((np.argmax(e > pk * 0.9) - np.argmax(e > pk * 0.1)) / sr)
            else:
                ipk = int(np.argmax(e > pk * 0.5))
                gone = np.where(e[ipk:] < pk * 0.01)[0]
                res["s"] = float(gone[0] / sr) if len(gone) else float((len(e) - ipk) / sr)
                # how loud it still was just before the test sound ran out (0.8 s in)
                k = int(sr * 0.75)
                res["rest"] = float(e[max(0, k - int(sr * 0.03)):k].mean() / pk) if len(e) > k else 0.0
        out["params"].setdefault(name, []).append(res)
    return out


# ---------------- from measurements to scales ----------------
def fit(meas):
    P = meas["params"]
    sc = {}

    def pts(key, rows, val):
        return [[r["raw"], round(val(r), 4)] for r in rows if val(r) is not None]

    if "LPF" in P:
        good = [r for r in P["LPF"] if r.get("hz")]
        sl = [r["slope"] for r in good if r.get("slope")]
        pk = [r["peak"] for r in good if r.get("peak") is not None and 200 < r["hz"] < 8000]
        sc["3"] = {"unit": "Hz", "curve": "pts", "pts": pts("hz", good, lambda r: r["hz"]),
                   "slope": round(float(sorted(sl)[len(sl) // 2]), 1) if sl else None,
                   "peak": round(float(sorted(pk)[len(pk) // 2]), 1) if pk else None}
    if "HPF" in P:
        good = [r for r in P["HPF"] if r.get("hz")]
        sl = [r["slope"] for r in good if r.get("slope")]
        pk = [r["peak"] for r in good if r.get("peak") is not None and 200 < r["hz"] < 8000]
        sc["4"] = {"unit": "Hz", "curve": "pts", "pts": pts("hz", good, lambda r: r["hz"]),
                   "slope": round(float(sorted(sl)[len(sl) // 2]), 1) if sl else None,
                   "peak": round(float(sorted(pk)[len(pk) // 2]), 1) if pk else None}
    if "LEVEL" in P:
        top = max(r["db"] for r in P["LEVEL"])
        sc["0"] = {"unit": "dB", "curve": "pts", "pts": pts("db", P["LEVEL"], lambda r: max(-90.0, r["db"] - top))}
    if "PAN" in P:
        def pan(r):
            if r["l"] + r["r"] < 1e-6:
                return None
            return 4 / math.pi * math.atan2(r["r"], r["l"]) - 1       # equal-power position, -1 .. +1
        sc["8"] = {"unit": "pan", "curve": "pts", "pts": pts("p", P["PAN"], pan)}
    for key, name in (("1", "PITCH"), ("9", "TUNE")):
        if name in P:
            sc[key] = {"unit": "st", "curve": "pts", "pts": pts("st", P[name], lambda r: r["st"])}
    if "TIME" in P:
        rows = P["TIME"]
        mid = rows[len(rows) // 2]
        speed = [r["hz"] / mid["hz"] if mid.get("hz") else None for r in rows]
        durs = [r["dur"] / mid["dur"] if mid.get("dur") else None for r in rows]
        pitch_moves = max(abs(math.log2(s)) for s in speed if s) > 0.05
        kind = "speed" if pitch_moves else "length"
        sc["2"] = {"unit": "×", "curve": "pts", "kind": kind,
                   "pts": [[r["raw"], round(s if kind == "speed" else d, 4)] for r, s, d in zip(rows, speed, durs) if (s if kind == "speed" else d)]}
    # ATK: measured 10-90 % rise; the K.O. ramps linearly, so the whole ramp is that / 0.8
    if "ATK" in P:
        sc["6"] = {"unit": "s", "curve": "pts", "pts": pts("s", P["ATK"], lambda r: r["s"] / 0.8)}
    # REL: measured from the note start; the note itself is 24 ticks (0.125 s at 120 bpm).
    # When the sound ran out before the release did, the release is longer than we can
    # see: store it as 'long' (10 s = plays out to the end of the sample, as the device did)
    if "REL" in P:
        note = 24 / 96 * 60 / 120
        seen = 0.8 - 0.02                        # the noise test sound is 0.8 s long

        plateau = max([r.get("rest", 0) for r in P["REL"]] + [0.05])   # no fade at all reads this

        def rel_s(r):
            if r["s"] < seen:
                return max(0.0, r["s"] - note)
            rest = r.get("rest", 1.0) / plateau  # cut off by the end of the sound: a linear fade
            if rest > 0.6:                       # that had this much left at 0.75 s
                return 10.0                      # barely fading: plays out to the end of the sample
            return round((0.75 - note) / max(0.05, 1 - rest), 3)
        sc["7"] = {"unit": "s", "curve": "pts",
                   "pts": pts("s", P["REL"], rel_s)}
    for v in sc.values():
        v["checked"] = True
        v["measured"] = True
        if v.get("pts"):
            vals = [p[1] for p in v["pts"]]
            v["min"], v["max"] = vals[0], vals[-1]
    return sc


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "analyze":
        m = analyze(sys.argv[2], json.load(open(sys.argv[3])))
        json.dump({"measured": m, "scales": fit(m)}, sys.stdout)
