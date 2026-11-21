"""Record what the K.O. II's master effects do, on the device itself.

One project write per setting (FX type, knob X, knob Y, the group's FX send, the master
compressor), then MIDI hits on test pads in group B: a click (for the impulse response of
delay, reverb, filter), a sine in level steps (distortion, compressors), a held 440 Hz sine
(chorus) and a noise burst. Every run's audio is cut out and saved under calibration/fx/,
so the browser can play the measured impulse responses and the models can be fitted.

  python3 calib_fx.py analyze take.wav plan.json   -> JSON {runs: [...], summary}
"""
import json, math, os, random, struct, sys
from paths import DATA_DIR

SR = 46875
GRID5, GRID3 = [0.0, 0.25, 0.5, 0.75, 1.0], [0.0, 0.5, 1.0]
PADS = {"click": 1, "steps": 2, "sine": 3, "noise": 4, "drop": 5}      # pad files in group B
DROP_AT = 0.6                                                 # "drop": loud for 0.6 s, then quiet: the compressors let go
STEP_LEVELS = [0.03, 0.06, 0.125, 0.25, 0.5, 0.9]            # each 0.25 s, 110 Hz


def _pcm(sec, fn, fade=0.003):
    n = int(round(sec * SR))
    out = bytearray()
    f = fade * SR
    for i in range(n):
        g = min(1.0, i / f, (n - 1 - i) / f) if f else 1.0
        out += struct.pack("<h", max(-32767, min(32767, int(round(fn(i) * g * 32767)))))
    return bytes(out)


def sounds():
    rnd = random.Random(99)
    click = _pcm(0.05, lambda i: 0.9 if i == 0 else 0.0, fade=0)
    seg = int(0.25 * SR)
    steps = _pcm(0.25 * len(STEP_LEVELS), lambda i: STEP_LEVELS[min(len(STEP_LEVELS) - 1, i // seg)] *
                 math.sin(2 * math.pi * 110 * i / SR))
    sine = _pcm(3.0, lambda i: 0.4 * math.sin(2 * math.pi * 440 * i / SR))
    noise = _pcm(1.0, lambda i: 0.3 * (rnd.random() * 2 - 1))
    drop = _pcm(2.0, lambda i: (0.9 if i < DROP_AT * SR else 0.03) * math.sin(2 * math.pi * 110 * i / SR))
    return [("cal fx click", click), ("cal fx steps", steps), ("cal fx sine", sine), ("cal fx noise", noise),
            ("cal fx drop", drop)]


def runs(phaser=False):
    """(label, fx type, x, y, send, comp drive, comp speed, signals, seconds); phaser: the EP-40's seventh effect"""
    out = []
    for x in GRID5:
        for y in GRID3:
            out.append(("delay", 1, x, y, 1.0, 0.5, 0.5, ["click"], 4.0))
    for x in GRID5:
        for y in GRID3:
            out.append(("reverb", 2, x, y, 1.0, 0.5, 0.5, ["click"], 4.0))
    for x in GRID5:
        for y in GRID3:
            out.append(("filter", 5, x, y, 1.0, 0.5, 0.5, ["click", "noise"], 2.2))
    for x in GRID5:
        for y in GRID3:
            out.append(("distortion", 3, x, y, 1.0, 0.5, 0.5, ["steps", "click"], 2.4))
    for x in GRID3:
        for y in GRID3:
            out.append(("chorus", 4, x, y, 1.0, 0.5, 0.5, ["sine", "click"], 4.2))
    for x in GRID3:
        for y in GRID3:
            out.append(("fxcomp", 6, x, y, 1.0, 0.5, 0.5, ["steps"], 1.9))
    for d in GRID5:
        for s in GRID3:
            out.append(("mastercomp", 5, 1.0, 0.0, 0.0, d, s, ["steps"], 1.9))
    for send in GRID5:                                       # how the FX fader mixes it in
        out.append(("send-delay", 1, 0.5, 0.5, send, 0.5, 0.5, ["click"], 2.5))
    for send in GRID3:
        out.append(("send-filter", 5, 0.0, 0.0, send, 0.5, 0.5, ["noise"], 1.4))
    for s in GRID3:                                          # how fast the compressors let go (release)
        out.append(("mastercomp-rel", 5, 1.0, 0.0, 0.0, 1.0, s, ["drop"], 2.2))
    for y in GRID3:
        out.append(("fxcomp-rel", 6, 1.0, y, 1.0, 0.5, 0.5, ["drop"], 2.2))
    if phaser:
        for x in GRID3:
            for y in GRID3:
                out.append(("phaser", 7, x, y, 1.0, 0.5, 0.5, ["sine", "click"], 4.2))
    return [dict(zip(("label", "fx", "x", "y", "send", "drive", "speed", "signals", "secs"), r)) for r in out]


def fx_bytes(base, r):
    b = bytearray(base)
    b[4] = r["fx"]
    struct.pack_into("<f", b, 8 + 4 * r["fx"], r["x"])
    struct.pack_into("<f", b, 8 + 4 * (16 + r["fx"]), r["y"])
    struct.pack_into("<4f", b, 136, r["drive"], r["speed"], 0.0, 0.5)    # sidechain length 0: off
    struct.pack_into("<4H", b, 152, 0, 0, 0, 0)
    return bytes(b)


def build(blank, template, slots, frames, r):
    import ep133write as W
    mem = W.read_members(blank)
    tpl = {m["name"]: m for m in W.read_members(template)}

    def pad(sym, end):
        b = bytearray(27)
        struct.pack_into("<H", b, 1, sym); struct.pack_into("<I", b, 8, end)
        struct.pack_into("<f", b, 12, 120.0); b[16] = 100; b[20] = 255; b[24] = 60
        return bytes(b)
    want = {("b", i + 1): pad(s, f) for i, (s, f) in enumerate(zip(slots, frames))}
    pads = []
    for m in mem:
        if m["name"].startswith("pads"):
            if m["type"] == "0":
                _, g, p = m["name"].split("/")
                m = dict(m, data=want.get((g, int(p[1:])), m["data"]))
            pads.append(m)
    pats = [m for m in mem if m["name"] == "patterns" or m["name"].startswith("patterns/")] or \
        [{"name": "patterns", "type": "5", "mode": b"0000755\0", "data": b""}]
    fx = {"name": "fx_settings", "type": "0", "mode": b"0000644\0", "data": fx_bytes(tpl["fx_settings"]["data"], r)}
    settings = dict(tpl["settings"]); st = bytearray(settings["data"])
    for gi in range(4):
        for i in range(12):
            v = {0: 1.0, 1: 0.5, 3: 1.0, 4: 0.0, 5: 0.0, 8: 0.5, 9: 0.5}.get(i, -1.0)
            if gi == 1 and i == 5:
                v = r["send"]
            struct.pack_into("<f", st, 24 + 4 * (gi * 12 + i), v)
    settings["data"] = bytes(st)
    return W.write_members([fx] + pads + pats + [dict(tpl["scenes"]), settings])


# ---------------- reading the recording ----------------
def load(path):
    import wave
    import numpy as np
    with wave.open(path) as w:
        sr, ch, n, sw = w.getframerate(), w.getnchannels(), w.getnframes(), w.getsampwidth()
        raw = w.readframes(n)
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768 if sw == 2 else \
        np.frombuffer(raw, "<f4").astype(np.float32)
    return sr, x.reshape(-1, ch)


def save_wav(path, sr, x):
    import wave
    import numpy as np
    y = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(y.shape[1]); w.setsampwidth(2); w.setframerate(sr); w.writeframes(y.tobytes())


def analyze(wav, plan, outdir):
    import numpy as np
    sr, x = load(wav)
    os.makedirs(outdir, exist_ok=True)
    # the log's clock against the recording, hit by hit: the recorder can start a little
    # before its clock is read, and it can lose a moment of audio mid-way, so every hit is
    # found on its own (the first sound after 30 ms of silence) and the rest borrow the last offset
    m_all = np.abs(x).max(axis=1)
    def onset(t, guess):
        a = int(max(0, t + guess - 0.7) * sr); seg = m_all[a:a + int(1.4 * sr)]
        g = int(0.03 * sr)
        for c in np.where(seg > 0.05)[0]:
            if c > g and seg[c - g:c - int(0.002 * sr)].max() < 0.004:
                return (a + c) / sr - t
        return None
    guess = 0.0
    for r in plan["runs"]:                                # a first rough offset
        for sig, t in r["t_hits"]:
            o = onset(t, -0.3)
            if o is not None:
                guess = o; break
        else:
            continue
        break
    res = []
    for k, r in enumerate(plan["runs"]):
        rr = dict(r, hits=[])
        for sig, t in r["t_hits"]:
            o = onset(t, guess)
            if o is not None and abs(o - guess) < 0.6:
                guess = o
            shift = guess
            rr["shift"] = round(shift, 4)
            a = int(max(0, t + shift - 0.003) * sr)            # 3 ms before the hit
            dur = r["secs"] if sig == r["signals"][0] else 1.2
            seg = x[a:a + int(dur * sr)]
            if not len(seg):
                continue
            name = f"{k:03d} {r['label']} fx{r['fx']} x{r['x']:.2f} y{r['y']:.2f} s{r['send']:.2f} d{r['drive']:.2f} sp{r['speed']:.2f} {sig}.wav"
            save_wav(os.path.join(outdir, name), sr, seg)
            m = {"signal": sig, "file": name, "peak": float(np.abs(seg).max()),
                 "rms_db": float(20 * np.log10(np.sqrt((seg ** 2).mean()) + 1e-9))}
            if sig == "click":                                   # echoes / decay
                env = np.abs(seg).max(axis=1)
                hop = int(0.005 * sr); kk = len(env) // hop
                ev = env[:kk * hop].reshape(kk, hop).max(axis=1)
                lv = 20 * np.log10(ev / (ev.max() + 1e-9) + 1e-9)
                peaks = [i for i in range(2, kk - 2) if ev[i] == ev[i - 2:i + 3].max() and lv[i] > -50]
                m["echo_ms"] = [round(i * 5.0, 0) for i in peaks[:12]]
                m["echo_db"] = [round(float(lv[i]), 1) for i in peaks[:12]]
                below = np.where(lv > -60)[0]
                m["tail_ms"] = float(below[-1] * 5) if len(below) else 0.0
            if sig == "steps":                                   # level in, level out per step
                seg_n = int(0.25 * sr); outs = []
                for j in range(len(STEP_LEVELS)):
                    s = seg[j * seg_n + int(0.05 * sr):(j + 1) * seg_n - int(0.02 * sr)]
                    outs.append(round(float(20 * np.log10(np.sqrt((s ** 2).mean()) + 1e-9)), 2) if len(s) else None)
                m["steps_db"] = outs
            if sig == "drop":                                    # the level after the drop, 10 ms windows
                d0 = int(DROP_AT * sr); hop = int(0.01 * sr); s = seg[d0:]
                kk = len(s) // hop
                if kk:
                    w = s[:kk * hop].reshape(kk, hop, -1)
                    m["after_db"] = [round(float(20 * np.log10(np.sqrt((w[i] ** 2).mean()) + 1e-9)), 2) for i in range(kk)]
            rr["hits"].append(m)
        res.append(rr)
    return res


def summary(runs):
    out = {}
    for r in runs:
        out.setdefault(r["label"], 0); out[r["label"]] += 1
    return " · ".join(f"{k} {v}" for k, v in out.items())


if __name__ == "__main__":
    if sys.argv[1] == "analyze":
        plan = json.load(open(sys.argv[3]))
        outdir = sys.argv[4] if len(sys.argv) > 4 else DATA_DIR / "calibration" / "fx"
        runs_ = analyze(sys.argv[2], plan, outdir)
        json.dump({"runs": runs_, "summary": summary(runs_)}, sys.stdout)
