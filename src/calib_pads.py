"""Measure what a K.O. II pad's own settings do, and how its voices behave.

The fader calibration (calib.py) measured the group faders on a plain pad. This
second pass plays test pads whose own settings differ -- amplitude, pan, attack,
release, pitch, play mode (one-shot / key / legato), mute group, BPM time
stretch -- plus velocities, the ATK/REL faders on pads that have their own
attack/release, and retriggers that show when voices overlap or cut each other.
The station records the K.O.'s USB output; this module reads that recording.

  python3 calib_pads.py analyze take.wav plan.json   -> JSON {measured, model, summary}
Only numpy is needed for the analysis."""
import json, math, random, struct, sys

BPM = 120.0
TPS = 192                       # ticks per second at 120 bpm (96 per beat)
LEAD = 24                       # fader automation lands this many ticks before its note
SR = 46875
ATK, REL, TIME = 6, 7, 2
NEUTRAL = {0: 32767, 1: 16384, 3: 32767, 4: 0, 5: 0, 8: 16384, 9: 16384}   # LEVEL PITCH LPF HPF FX PAN TUNE
PLAYMODE = {"oneshot": 0, "key": 1, "legato": 2}


# ---------------- test sounds ----------------
def _pcm(sec, fn):
    n = int(round(sec * SR))
    out = bytearray()
    fade = 0.003 * SR
    for i in range(n):
        g = min(1.0, i / fade, (n - 1 - i) / fade)
        out += struct.pack("<h", max(-32767, min(32767, int(round(fn(i) * g * 32767)))))
    return bytes(out)


def sounds():
    rnd = random.Random(777)
    noise = _pcm(3.0, lambda i: 0.35 * (rnd.random() * 2 - 1))
    ph = [0.0]

    def two(i):                                   # 300 Hz for 0.5 s, then 600 Hz, phase-continuous
        ph[0] += 2 * math.pi * (300 if i < 0.5 * SR else 600) / SR
        return 0.4 * math.sin(ph[0])
    tone = _pcm(3.0, two)
    sine = _pcm(1.0, lambda i: 0.5 * math.sin(2 * math.pi * 440 * i / SR))
    burst = int(0.015 * SR)
    clicks = _pcm(2.0, lambda i: 0.6 * math.sin(2 * math.pi * 1000 * i / SR) if i % int(0.25 * SR) < burst else 0.0)
    return [("cal noise3", noise), ("cal tone2", tone), ("cal sine1", sine), ("cal clicks", clicks)]


# ---------------- the pad file (27 bytes) ----------------
def pad_bytes(sym, end, amp=100, pan=0, attack=0, release=255, mode="oneshot", pitch=0.0,
              mute=False, stretch=False, bpm=120.0):
    b = bytearray(27)
    struct.pack_into("<H", b, 1, sym)
    struct.pack_into("<I", b, 8, end)
    struct.pack_into("<f", b, 12, float(bpm))
    b[16] = amp
    whole = math.floor(round(pitch, 2))
    b[17] = whole & 0xFF                          # semitones (signed) ...
    b[26] = int(round((pitch - whole) * 100))     # ... plus hundredths
    b[18] = pan & 0xFF
    b[19] = attack
    b[20] = release
    b[21] = 1 if stretch else 0                   # time.mode bpm
    b[22] = 1 if mute else 0                      # mute group (a guess: read back after writing)
    b[23] = PLAYMODE[mode]
    b[24] = 60
    return bytes(b)


# ---------------- the project ----------------
def build(blank, template, slots, frames):
    import ep133write as W
    noise, tone, sine, clicks = slots
    fN, fT, fS, fC = frames
    sec = lambda s: int(round(s * TPS))
    groups = {g: {"notes": [], "auto": []} for g in "abcd"}
    pads = {}
    tests = []

    def pad(g, n, **kw):
        pads[(g, n)] = kw
        return n

    def note(g, n, t, dur=0.5, key=60, vel=100):
        groups[g]["notes"].append((sec(t), n - 1, key, vel, max(1, sec(dur))))

    def auto(g, t, param, raw):
        groups[g]["auto"].append((max(0, sec(t) - LEAD), param, raw))

    for g in "abcd":
        for p, v in NEUTRAL.items():
            groups[g]["auto"].append((0, p, v))
    short = int(0.6 * SR)
    t = 0.0
    # marker: a loud first hit marks time zero in the recording
    pad("a", 1, sym=noise, end=int(0.3 * SR))
    note("a", 1, 0.0)
    tests.append({"kind": "marker", "t": 0.0})
    t = 1.0
    for i, a in enumerate([0, 50, 100, 150, 200]):                    # pad amplitude
        n = pad("a", 2 + i, sym=noise, end=short, amp=a)
        note("a", n, t); tests.append({"kind": "amp", "t": t, "value": a}); t += 1
    for i, p in enumerate([-16, -8, 0, 8, 16]):                       # pad pan
        n = pad("a", 7 + i, sym=noise, end=short, pan=p)
        note("a", n, t); tests.append({"kind": "pan", "t": t, "value": p}); t += 1
    pad("a", 12, sym=noise, end=short)
    for v in [127, 100, 64, 32, 8]:                                   # velocity
        note("a", 12, t, vel=v); tests.append({"kind": "vel", "t": t, "value": v}); t += 1
    pad("b", 4, sym=sine, end=int(0.6 * SR))
    for i, p in enumerate([-12, -0.5, 7]):                            # pad pitch, with hundredths
        n = pad("b", 1 + i, sym=sine, end=int(0.6 * SR), pitch=p)
        note("b", n, t); tests.append({"kind": "pitch", "t": t, "value": p}); t += 1
    note("b", 4, t); tests.append({"kind": "pitch", "t": t, "value": 0}); t += 1
    for i, a in enumerate([0, 32, 64, 128, 192, 255]):                # pad attack (one-shot, 3 s noise)
        n = pad("b", 5 + i, sym=noise, end=fN, attack=a)
        note("b", n, t); tests.append({"kind": "attack", "t": t, "value": a, "len": 3.0}); t += 3
    for i, r in enumerate([0, 15, 32, 64, 128, 192, 255]):            # pad release (key mode, 1/8 s note)
        n = pad("c", 1 + i, sym=noise, end=fN, release=r, mode="key")
        note("c", n, t, dur=0.125); tests.append({"kind": "release", "t": t, "value": r, "note": 0.125}); t += 3.5
    for i, bp in enumerate([60, 120, 180]):                           # BPM time stretch (clicks every 1/4 s)
        n = pad("c", 8 + i, sym=clicks, end=fC, stretch=True, bpm=bp)
        note("c", n, t); tests.append({"kind": "stretch", "t": t, "value": bp}); t += 2.5
    for raw in [0, 16384, 32767]:                                     # TIME fader on a stretched pad
        auto("c", t, TIME, raw)
        note("c", 9, t); tests.append({"kind": "time_on_stretch", "t": t, "value": raw}); t += 2.5
    pad("d", 1, sym=noise, end=fN, attack=128)
    for raw in [0, 16384, 32767]:                                     # ATK fader on a pad with attack 128
        auto("d", t, ATK, raw)
        note("d", 1, t); tests.append({"kind": "atk_fader", "t": t, "value": raw, "len": 3.0}); t += 3
    auto("d", t, ATK, 16384)
    pad("d", 2, sym=noise, end=fN, release=64, mode="key")
    for raw in [0, 16384, 32767]:                                     # REL fader on a key pad with release 64
        auto("d", t, REL, raw)
        note("d", 2, t, dur=0.125); tests.append({"kind": "rel_fader", "t": t, "value": raw, "note": 0.125}); t += 3.5
    auto("d", t, REL, 16384)
    # voices, with the two-tone sound (300 Hz for 0.5 s, then 600 Hz)
    pad("d", 3, sym=tone, end=fT)
    note("d", 3, t); note("d", 3, t + 0.5)
    tests.append({"kind": "voice", "name": "same pad, one-shot, retrigger", "t": t}); t += 3
    pad("d", 4, sym=tone, end=fT); pad("d", 5, sym=tone, end=fT)
    note("d", 4, t); note("d", 5, t + 0.5)
    tests.append({"kind": "voice", "name": "two pads, one-shot", "t": t}); t += 3
    pad("d", 6, sym=tone, end=fT, mute=True); pad("d", 7, sym=tone, end=fT, mute=True)
    note("d", 6, t); note("d", 7, t + 0.5)
    tests.append({"kind": "voice", "name": "two pads, mute group", "t": t}); t += 3
    pad("d", 8, sym=tone, end=fT, mode="key")
    note("d", 8, t, dur=1.5); note("d", 8, t + 0.5, dur=1.0)
    tests.append({"kind": "voice", "name": "same pad, key, overlapping notes", "t": t}); t += 3
    pad("d", 9, sym=tone, end=fT, mode="legato")
    note("d", 9, t, dur=1.5); note("d", 9, t + 0.5, dur=1.0, key=67)
    tests.append({"kind": "legato", "name": "legato, second note +7 while the first holds", "t": t}); t += 3
    pad("d", 10, sym=tone, end=fT, mode="key")
    note("d", 10, t, dur=1.0); note("d", 10, t, dur=1.0, key=64)
    tests.append({"kind": "chord", "name": "same pad, key, two notes at once", "t": t}); t += 3
    total = t
    bars = min(99, int(math.ceil(sec(total) / 384)) + 2)
    plan = {"bpm": BPM, "total_s": total, "bars": bars, "tests": tests, "slots": slots, "frames": frames,
            "pads": {f"{g.upper()}{n:02d}": kw for (g, n), kw in pads.items()}}

    mem = W.read_members(blank)
    tpl = {m["name"]: m for m in W.read_members(template)}
    out_pads = []
    for m in mem:
        if m["name"].startswith("pads"):
            if m["type"] == "0":
                _, g, p = m["name"].split("/")
                kw = pads.get((g, int(p[1:])))
                if kw:
                    m = dict(m, data=pad_bytes(**kw))
            out_pads.append(m)
    pats = [{"name": "patterns", "type": "5", "mode": b"0000755\0", "data": b""}]
    for g in "abcd":
        recs = [W.auto_record(tt, p, v) for tt, p, v in groups[g]["auto"]]
        recs += [W.note_record(tt, pd, k, v, d) for tt, pd, k, v, d in groups[g]["notes"]]
        pats.append({"name": f"patterns/{g}01", "type": "0", "mode": b"0000644\0",
                     "data": W.pattern_bytes({"lead": 0, "bars": bars, "records": recs})})
    sc = W.scenes_rows(tpl["scenes"]["data"])
    rows = [[4, 4, 1, 1, 1, 1]] + [[4, 4, 0, 0, 0, 0]] * 98
    tail = bytearray(sc["tail"]); tail[5] = 1
    scenes = {"name": "scenes", "type": "0", "mode": b"0000644\0",
              "data": W.scenes_bytes({"head": sc["head"], "rows": rows, "tail": bytes(tail)})}
    st = bytearray(tpl["settings"]["data"])
    struct.pack_into("<f", st, 4, BPM)
    rest = {0: 1.0, 1: 0.5, 3: 1.0, 4: 0.0, 5: 0.0, 8: 0.5, 9: 0.5}
    for gi in range(4):
        for i in range(12):
            struct.pack_into("<f", st, 24 + 4 * (gi * 12 + i), rest.get(i, -1.0))
    settings = {"name": "settings", "type": "0", "mode": b"0000644\0", "data": bytes(st)}
    fx = [dict(tpl["fx_settings"])] if "fx_settings" in tpl else []
    return W.write_members(fx + out_pads + pats + [scenes, settings]), plan


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


def analyze(wav_path, plan):
    import numpy as np
    sr, x = load(wav_path)
    mono = x.mean(axis=1)
    hop = max(1, int(sr * 0.002))
    env0 = np.sqrt(np.convolve(mono ** 2, np.ones(hop) / hop, "same"))
    t0 = float(np.argmax(env0 > 10 ** (-40 / 20)) / sr)

    def seg(y, a, b):
        return y[max(0, int((t0 + a) * sr)):max(0, int((t0 + b) * sr))]

    def rms_db(y):
        return float(20 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-9))

    def envelope(y, step=0.005):
        h = max(1, int(sr * step))
        k = len(y) // h
        return np.sqrt(np.mean(y[:k * h].reshape(k, h) ** 2, axis=1)) if k else np.zeros(1)

    def freq(y):
        y = y * np.hanning(len(y))
        n = 1 << (int(np.ceil(np.log2(len(y)))) + 3)
        s = np.abs(np.fft.rfft(y, n))
        k = int(np.argmax(s[5:])) + 5
        a, b_, c = np.log(s[k - 1] + 1e-12), np.log(s[k] + 1e-12), np.log(s[k + 1] + 1e-12)
        d = 0.5 * (a - c) / (a - 2 * b_ + c) if (a - 2 * b_ + c) else 0
        return float((k + d) * sr / n)

    def tones(y, fs):
        """level of each frequency in dB relative to the strongest of them"""
        y = y * np.hanning(len(y))
        n = 1 << (int(np.ceil(np.log2(len(y)))) + 2)
        s = np.abs(np.fft.rfft(y, n)) ** 2
        f = np.fft.rfftfreq(n, 1 / sr)
        lv = [float(s[(f > q * 0.97) & (f < q * 1.03)].max()) for q in fs]
        top = max(lv) + 1e-20
        return [round(10 * np.log10(v / top + 1e-12), 1) for v in lv]

    def rise(e, step=0.005):
        """attack: time from the hit to 90 % of the level it settles at"""
        plateau = float(np.median(e[int(len(e) * 0.75):])) + 1e-9
        i10 = int(np.argmax(e > plateau * 0.1)); i90 = int(np.argmax(e > plateau * 0.9))
        return {"t10": i10 * step, "t90": i90 * step, "ramp": max(0.0, (i90 - i10) * step / 0.8),
                "curve": [round(float(v / plateau), 3) for v in e[:: max(1, len(e) // 60)]]}

    def fall(e, note, step=0.005):
        """release: from the note-off to -40 dB of the level it had; or how much is left at the end"""
        i0 = int(note / step)
        held = float(np.median(e[max(0, i0 - 8):i0])) + 1e-9
        after = e[i0:]
        gone = np.where(after < held * 0.01)[0]
        half = np.where(after < held * 0.5)[0]
        out = {"held": held, "curve": [round(float(v / held), 3) for v in after[:: max(1, len(after) // 60)]]}
        if len(gone):
            out["s"] = float(gone[0] * step)
        else:
            out["s_min"] = float(len(after) * step)
            out["left"] = float(after[-40:-20].mean() / held) if len(after) > 40 else 0.0
        out["half"] = float(half[0] * step) if len(half) else None
        return out

    res = {"t0": t0, "sr": sr, "tests": []}
    for tst in plan["tests"]:
        k, a = tst["kind"], tst["t"]
        r = dict(tst)
        if k == "amp" or k == "vel":
            r["db"] = rms_db(seg(mono, a + 0.1, a + 0.5))
        elif k == "pan":
            y = seg(x, a + 0.1, a + 0.5)
            r["l_db"], r["r_db"] = rms_db(y[:, 0]), rms_db(y[:, -1])
        elif k == "pitch":
            r["hz"] = freq(seg(mono, a + 0.05, a + 0.5))
        elif k in ("attack", "atk_fader"):
            r.update(rise(envelope(seg(mono, a, a + 2.95))))
        elif k in ("release", "rel_fader"):
            r.update(fall(envelope(seg(mono, a, a + 3.4)), tst["note"]))
        elif k in ("stretch", "time_on_stretch"):
            y = seg(mono, a, a + 2.4)
            e = envelope(y, 0.002)
            on = e > e.max() * 0.3
            starts = np.where(np.diff(on.astype(int)) == 1)[0]
            r["clicks"] = int(len(starts)) + (1 if on[0] else 0)
            r["gap"] = float(np.median(np.diff(starts)) * 0.002) if len(starts) > 2 else None
            r["hz"] = freq(seg(mono, a + 0.001, a + 0.013))
            idx = np.where(on)[0]
            r["dur"] = float((idx[-1] - idx[0]) * 0.002) if len(idx) else 0.0
        elif k == "voice":
            r["early"] = tones(seg(mono, a + 0.1, a + 0.45), [300, 600])      # before the second hit
            r["late"] = tones(seg(mono, a + 0.6, a + 0.95), [300, 600])       # after it: 300 = the new voice
            r["late_db"] = rms_db(seg(mono, a + 0.6, a + 0.95))
            r["early_db"] = rms_db(seg(mono, a + 0.1, a + 0.45))
        elif k == "legato":
            q = 2 ** (7 / 12)
            r["late"] = dict(zip(["restart_300x", "continue_600x", "old_600", "old_300"],
                                 tones(seg(mono, a + 0.6, a + 0.95), [300 * q, 600 * q, 600, 300])))
        elif k == "chord":
            r["tones"] = dict(zip(["300", "378"], tones(seg(mono, a + 0.1, a + 0.45), [300, 300 * 2 ** (4 / 12)])))
        res["tests"].append(r)
    return res


# ---------------- from measurements to a pad model ----------------
def fit(meas):
    T = meas["tests"]
    by = lambda k: [t for t in T if t["kind"] == k]
    m = {}
    amp = by("amp")
    ref = next((t["db"] for t in amp if t["value"] == 100), None)
    if ref is not None:
        m["amp_db"] = [[t["value"], round(t["db"] - ref, 2)] for t in amp]
    pan = by("pan")
    if pan:
        m["pan_db"] = [[t["value"], round(t["l_db"], 2), round(t["r_db"], 2)] for t in pan]
    vel = by("vel")
    top = next((t["db"] for t in vel if t["value"] == 127), None)
    if top is not None:
        m["vel_db"] = [[t["value"], round(t["db"] - top, 2)] for t in vel]
    pit = by("pitch")
    z = next((t["hz"] for t in pit if t["value"] == 0), None)
    if z:
        m["pitch_st"] = [[t["value"], round(12 * math.log2(t["hz"] / z), 3)] for t in pit]
    m["attack"] = [[t["value"], round(t["ramp"], 4), round(t["t90"], 4)] for t in by("attack")]
    def rel_row(t):
        """[value, seconds to -40 dB, seconds to half, censored?]; when the test sound ran out
        first (3 s sound, note-off at 1/8 s), extend the fade seen so far in a straight line"""
        s, half, cur = t.get("s"), t.get("half"), t.get("curve") or []
        dt = (3.4 - t.get("note", 0.125)) / max(1, len(cur)) if cur else 0.05
        end = 3.0 - t.get("note", 0.125)
        if s is None or s >= end - 0.1:
            k = max(1, int((end - 0.2) / dt))
            left = sum(cur[max(0, k - 4):k]) / max(1, len(cur[max(0, k - 4):k])) if cur else 0.5
            at = k * dt
            s = round(at / max(0.05, 1 - left), 3)
            if half is None or half >= end - 0.1:
                half = round(at * 0.5 / max(0.05, 1 - left), 3) if left > 0.5 else half
            return [t["value"], s, half, True]
        return [t["value"], round(s, 3), half, False]
    m["release"] = [rel_row(t) for t in by("release")]
    m["attack_curves"] = {str(t["value"]): t["curve"] for t in by("attack")}
    m["release_curves"] = {str(t["value"]): t["curve"] for t in by("release")}
    st = by("stretch")
    base = next((t for t in st if t["value"] == 120), None)
    if base and base.get("gap"):
        m["stretch"] = [[t["value"], round(base["gap"] / t["gap"], 3) if t.get("gap") else None,
                         round(t["hz"] / base["hz"], 3) if base.get("hz") else None] for t in st]
    ts = by("time_on_stretch")
    mid = next((t for t in ts if t["value"] == 16384), None)
    if mid and mid.get("gap"):
        m["time_on_stretch"] = [[t["value"], round(mid["gap"] / t["gap"], 3) if t.get("gap") else None,
                                 round(t["dur"] / mid["dur"], 3) if mid.get("dur") else None] for t in ts]
    m["atk_fader"] = [[t["value"], round(t["ramp"], 4)] for t in by("atk_fader")]
    m["rel_fader"] = [rel_row(t) for t in by("rel_fader")]
    v = {}
    for t in by("voice"):
        both = bool(t["late"][1] > -20 and t["late"][0] > -20)
        v[t["name"]] = {"late": t["late"], "overlap": both, "cut": bool(t["late"][1] < -30)}
    for t in by("legato"):
        L = t["late"]
        best = max(L, key=L.get)
        v[t["name"]] = {"tones": L, "heard": best}
    for t in by("chord"):
        v[t["name"]] = {"tones": t["tones"], "both": bool(min(t["tones"].values()) > -20)}
    m["voices"] = v
    return m


def summary(m):
    out = []
    if "amp_db" in m:
        out.append("pad level " + ", ".join(f"{a}→{d:+.1f} dB" for a, d in m["amp_db"]))
    if "vel_db" in m:
        out.append("velocity " + ", ".join(f"{a}→{d:+.1f} dB" for a, d in m["vel_db"]))
    if m.get("attack"):
        out.append("pad attack " + ", ".join(f"{a}→{r * 1000:.0f} ms" for a, r, _ in m["attack"]))
    if m.get("release"):
        out.append("pad release " + ", ".join(f"{a}→" + ("≈" if cen else "") + (f"{s * 1000:.0f} ms" if s < 1 else f"{s:.1f} s")
                                             for a, s, _h, cen in m["release"]))
    for name, r in (m.get("voices") or {}).items():
        if "overlap" in r:
            out.append(f"{name}: " + ("overlap" if r["overlap"] else "cut" if r["cut"] else "?"))
        elif "heard" in r:
            out.append(f"{name}: {r['heard']}")
        elif "both" in r:
            out.append(f"{name}: " + ("both sound" if r["both"] else "one only"))
    return " · ".join(out)


if __name__ == "__main__":
    if sys.argv[1] == "analyze":
        plan = json.load(open(sys.argv[3]))
        meas = analyze(sys.argv[2], plan)
        model = fit(meas)
        json.dump({"measured": meas, "model": model, "summary": summary(model)}, sys.stdout)
