"""Turn the FX calibration recordings (calib_fx.py) into the model the browser plays.

  python3 calib_fx_fit.py calibration/<stamp> fx result.json   -> ep133_fxmodel.json + calibration/fx/ir/*.wav
numpy only."""
import json, math, os, sys, wave
import numpy as np

from paths import DATA_DIR
FXDIR = os.path.join(DATA_DIR, "calibration", "fx")
IRDIR = os.path.join(FXDIR, "ir")
OUT = os.path.join(DATA_DIR, "ep133_fxmodel.json")


def set_device(slug):
    """the K.O. II keeps calibration/fx and ep133_fxmodel.json; another device its own folder and file"""
    global FXDIR, IRDIR, OUT
    FXDIR = os.path.join(DATA_DIR, "calibration", "fx" if slug == "ep133" else f"fx-{slug}")
    IRDIR = os.path.join(FXDIR, "ir")
    OUT = os.path.join(DATA_DIR, f"{slug}_fxmodel.json")


def rd(name):
    with wave.open(os.path.join(FXDIR, name)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, w.getnchannels()).astype(np.float64) / 32768
    return sr, x


def save_ir(name, sr, h):
    os.makedirs(IRDIR, exist_ok=True)
    k = float(np.abs(h).max()) or 1.0
    y = np.round(h / k * 32767).astype("<i2")
    with wave.open(os.path.join(IRDIR, name), "wb") as w:
        w.setnchannels(h.shape[1]); w.setsampwidth(2); w.setframerate(sr); w.writeframes(y.tobytes())
    return k


def hit(runs, label, sig, **kw):
    for r in runs:
        if r["label"] == label and all(abs(r[k] - v) < 1e-6 for k, v in kw.items()):
            for h in r["hits"]:
                if h["signal"] == sig:
                    return h
    return None


def env_ms(x, sr, ms=1.0):
    m = np.abs(x).max(axis=1); hop = max(1, int(sr * ms / 1000)); k = len(m) // hop
    return m[:k * hop].reshape(k, hop).max(axis=1)


def peaks(ev, rel=0.03, merge=6):
    pk = [i for i in range(1, len(ev) - 1) if ev[i] >= ev[i - 1] and ev[i] > ev[i + 1] and ev[i] > rel * ev.max()]
    out = []
    for i in pk:
        if out and i - out[-1][0] < merge:
            if ev[i] > out[-1][1]:
                out[-1] = (i, ev[i])
        else:
            out.append((i, float(ev[i])))
    return out


def band_db(x, sr, n=4096):
    m = x.mean(axis=1)
    if len(m) < n:
        m = np.pad(m, (0, n - len(m)))
    win = np.hanning(n)
    segs = [m[i:i + n] * win for i in range(0, len(m) - n + 1, n // 2)]
    p = np.mean([np.abs(np.fft.rfft(s)) ** 2 for s in segs], axis=0)
    f = np.fft.rfftfreq(n, 1 / sr)
    edges = 25 * 2 ** (np.arange(0, 60) / 6)
    edges = edges[edges < sr / 2 * 0.9]
    cen = np.sqrt(edges[:-1] * edges[1:])
    v = np.array([p[(f >= a) & (f < b)].mean() if ((f >= a) & (f < b)).any() else np.nan for a, b in zip(edges[:-1], edges[1:])])
    return cen, 10 * np.log10(v + 1e-20)


def biquad_db(kind, f0, q, f, sr):
    w0 = 2 * math.pi * f0 / sr; al = math.sin(w0) / (2 * q); c = math.cos(w0)
    if kind == "lowpass":
        b = [(1 - c) / 2, 1 - c, (1 - c) / 2]
    elif kind == "highpass":
        b = [(1 + c) / 2, -(1 + c), (1 + c) / 2]
    else:
        b = [al, 0, -al]
    a = [1 + al, -2 * c, 1 - al]
    z = np.exp(-1j * 2 * np.pi * f / sr)
    H = (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    return 20 * np.log10(np.abs(H) + 1e-12)


def main(result_path):
    R = json.load(open(result_path))
    runs = R["runs"]
    model = {"measured_at": os.path.basename(result_path).split(" ")[0], "types": {}}

    # ---- the dry click: what a hit sounds like with the FX fader down
    h0 = hit(runs, "send-delay", "click", send=0.0)
    sr, d = rd(h0["file"])
    d = d[:int(0.03 * sr)]
    area = d.sum(axis=0)                                       # how much impulse the dry click carries (its integral)
    dry_peak = float(np.abs(d).max())

    # ---- how the FX fader mixes it in: delay (dry stays), filter (dry goes)
    law = []
    for s in (0.25, 0.5, 0.75, 1.0):
        h = hit(runs, "send-delay", "click", send=s)
        _, y = rd(h["file"]); ev = env_ms(y, sr)
        pk = [p for p in peaks(ev) if p[0] > 20]
        law.append([s, round(float(20 * np.log10(pk[0][1] / dry_peak)), 2) if pk else None,
                    round(float(20 * np.log10(ev[:10].max() / dry_peak)), 2)])
    model["send_delay"] = {"points": law, "note": "[send, first echo dB rel. dry hit, dry dB rel. dry hit]"}
    lv = {}
    for s in (0.0, 0.5, 1.0):
        h = hit(runs, "send-filter", "noise", send=s)
        lv[s] = h["rms_db"]
    model["send_filter"] = {"rms_db": lv}

    # ---- delay: time from X, repeat gain from Y, level of the first echo
    dl = []
    for x in (0.0, 0.25, 0.5, 0.75, 1.0):
        row = {"x": x}
        for y in (0.0, 0.5, 1.0):
            h = hit(runs, "delay", "click", x=x, y=y)
            _, s = rd(h["file"]); ev = env_ms(s, sr)
            pk = peaks(ev)
            dry = max([p[1] for p in pk if p[0] < 8] or [dry_peak])
            ech = [p for p in pk if p[0] >= 8]
            if y == 0.0 and ech:
                row["time_ms"] = ech[0][0]; row["echo_db"] = round(float(20 * np.log10(ech[0][1] / dry)), 2)
                for c in (0, 1):                                  # left / right first echo
                    evc = env_ms(s[:, c:c + 1], sr); pc = [p for p in peaks(evc) if p[0] >= 8]
                    row["time_ms_" + "LR"[c]] = pc[0][0] if pc else None
            if y == 0.5 and len(ech) >= 3:
                # gain per repeat: fit a line through the echo levels on the echo-time grid
                t0 = row.get("time_ms") or ech[0][0]
                lv_ = {}
                for i, v in ech:
                    n = round(i / max(1, t0))
                    if n >= 1:
                        lv_[n] = max(lv_.get(n, 0), v)
                ns = sorted(lv_)[:6]
                if len(ns) >= 3:
                    sl = np.polyfit(ns, [20 * np.log10(lv_[n]) for n in ns], 1)[0]
                    row["repeat_db_y05"] = round(float(sl), 2)
            if y == 1.0:
                row["tail_ms_y1"] = h.get("tail_ms")
        dl.append(row)
    model["types"]["1"] = {"name": "delay", "rows": dl}

    # ---- reverb: the measured impulse responses themselves (wet only: the dry hit goes)
    irs = {}
    for x in (0.0, 0.25, 0.5, 0.75, 1.0):
        for y in (0.0, 0.5, 1.0):
            h = hit(runs, "reverb", "click", x=x, y=y)
            _, s = rd(h["file"])
            ev = env_ms(s, sr, 10)
            lvl = 20 * np.log10(ev / ev.max() + 1e-12)
            keep = np.where(lvl > -72)[0]
            n = int((keep[-1] + 1) * 0.01 * sr) if len(keep) else len(s)
            ir = s[:n] / area                                   # per channel, so conv(dry, ir) ~ the recording
            name = f"reverb_x{x:.2f}_y{y:.2f}.wav"
            k = save_ir(name, sr, ir)
            irs[f"{x:.2f},{y:.2f}"] = {"file": name, "gain": k, "secs": round(n / sr, 3)}
    model["types"]["2"] = {"name": "reverb", "ir": irs, "sr": sr}

    # ---- filter: the noise against the dry noise, fitted to one biquad per setting
    _, dn = rd(hit(runs, "send-filter", "noise", send=0.0)["file"])
    f, dref = band_db(dn[:int(0.4 * sr)], sr)
    fl = []
    for x in (0.0, 0.25, 0.5, 0.75, 1.0):
        for y in (0.0, 0.5, 1.0):
            h = hit(runs, "filter", "noise", x=x, y=y)
            _, s = rd(h["file"])
            _, db = band_db(s[:int(0.4 * sr)], sr)
            resp = db - dref
            ok = np.isfinite(resp) & (f > 30) & (f < 16000)
            best = None
            for kind in ("lowpass", "highpass", "bandpass"):
                for f0 in 20 * 2 ** (np.arange(0, 100) / 10):
                    if f0 > sr / 2 * 0.9:
                        break
                    for q in (0.5, 0.707, 1.0, 1.5, 2.5, 4, 6, 9, 14):
                        m = biquad_db(kind, f0, q, f[ok], sr)
                        g = float(np.median(resp[ok] - m))
                        err = float(np.mean(np.abs(resp[ok] - m - g)))
                        if best is None or err < best[0]:
                            best = (err, kind, float(f0), q, g)
            fl.append({"x": x, "y": y, "kind": best[1], "f0": round(best[2], 1), "q": best[3], "gain_db": round(best[4], 2),
                       "err_db": round(best[0], 2), "resp": [round(float(v), 1) if np.isfinite(v) else None for v in resp]})
    model["types"]["5"] = {"name": "filter", "rows": fl, "bands_hz": [round(float(v), 1) for v in f]}

    # ---- the compressors: output level per input step
    from calib_fx import STEP_LEVELS
    inp = [round(20 * math.log10(a / math.sqrt(2)), 2) for a in STEP_LEVELS]
    mc = []
    for dr in (0.0, 0.25, 0.5, 0.75, 1.0):
        for sp in (0.0, 0.5, 1.0):
            h = hit(runs, "mastercomp", "steps", drive=dr, speed=sp)
            mc.append({"drive": dr, "speed": sp, "out_db": h["steps_db"]})
    model["mastercomp"] = {"in_db": inp, "rows": mc}
    fc = []
    for x in (0.0, 0.5, 1.0):
        for y in (0.0, 0.5, 1.0):
            h = hit(runs, "fxcomp", "steps", x=x, y=y)
            fc.append({"x": x, "y": y, "out_db": h["steps_db"]})
    model["types"]["6"] = {"name": "compressor", "in_db": inp, "rows": fc}
    ds = []
    for x in (0.0, 0.25, 0.5, 0.75, 1.0):
        for y in (0.0, 0.5, 1.0):
            h = hit(runs, "distortion", "steps", x=x, y=y)
            hc = hit(runs, "distortion", "click", x=x, y=y)
            _, s = rd(h["file"])
            seg = s[int(0.05 * sr):int(0.23 * sr)].mean(axis=1)       # the quietest step
            ds.append({"x": x, "y": y, "out_db": h["steps_db"], "click_peak": hc["peak"]})
            # its colour: the step-3 sine's spectrum (harmonics) saved for the shaper fit
            s3 = s[int(0.55 * sr):int(0.73 * sr)].mean(axis=1)
            sp = np.abs(np.fft.rfft(s3 * np.hanning(len(s3))))
            fr = np.fft.rfftfreq(len(s3), 1 / sr)
            harm = []
            for k in range(1, 9):
                b = (fr > 110 * k * 0.97) & (fr < 110 * k * 1.03)
                harm.append(round(float(20 * np.log10(sp[b].max() + 1e-9)), 1) if b.any() else None)
            ds[-1]["harm_db"] = harm
    model["types"]["3"] = {"name": "distortion", "in_db": inp, "rows": ds}
    ch = []
    for x in (0.0, 0.5, 1.0):
        for y in (0.0, 0.5, 1.0):
            h = hit(runs, "chorus", "sine", x=x, y=y)
            _, s = rd(h["file"])
            seg = s[int(0.3 * sr):int(2.8 * sr)]
            # amplitude wobble of the sum: its main rate = the chorus LFO (or twice it)
            env = np.abs(seg).max(axis=1); hop = int(0.005 * sr); k = len(env) // hop
            e = env[:k * hop].reshape(k, hop).max(axis=1); e = e - e.mean()
            spec = np.abs(np.fft.rfft(e * np.hanning(len(e)))); fr = np.fft.rfftfreq(len(e), 0.005)
            b = (fr > 0.1) & (fr < 20)
            rate = float(fr[b][np.argmax(spec[b])]) if b.any() else None
            depth = float(20 * np.log10((np.percentile(env, 95) + 1e-9) / (np.percentile(env, 5) + 1e-9)))
            lr = [float(np.sqrt((seg[:, c] ** 2).mean())) for c in (0, 1)]
            ch.append({"x": x, "y": y, "wobble_hz": round(rate, 3) if rate else None, "wobble_db": round(depth, 2),
                       "lr_db": [round(20 * math.log10(v + 1e-9), 2) for v in lr]})
    model["types"]["4"] = {"name": "chorus", "rows": ch}
    model["dry"] = {"peak": dry_peak, "area": [float(a) for a in area], "sr": sr}
    json.dump(model, open(OUT, "w"), indent=1)
    return model


def run(result_path):
    m = main(result_path)
    m["runs_raw"] = json.load(open(result_path))["runs"]
    m = engine(m)
    m.pop("runs_raw", None)
    json.dump(m, open(OUT, "w"), indent=1)
    return m


# ---------------------------------------------------------------- second pass: what the browser runs
def mc_inv(mc5_in, mc5_out, out):
    """the level that, sent through the master as it was during the FX runs (drive .5, speed .5), comes out at 'out'"""
    a, b = np.array(mc5_out), np.array(mc5_in)
    out = np.asarray(out, float)
    lo = b[0] + (out - a[0]) * (b[1] - b[0]) / (a[1] - a[0])
    hi = b[-1] + (out - a[-1]) * (b[-1] - b[-2]) / (a[-1] - a[-2])
    return np.where(out < a[0], lo, np.where(out > a[-1], hi, np.interp(out, a, b)))


def tanh_rms_db(pre_db, amps):
    t = np.linspace(0, 2 * np.pi, 512, endpoint=False)
    g = 10 ** (pre_db / 20)
    return np.array([20 * np.log10(np.sqrt(np.mean(np.tanh(g * a * np.sin(t)) ** 2)) + 1e-12) for a in amps])


def onepole_hp_db(f1, f, sr=48000):
    # the browser's one-pole (bilinear / TPT): |H| = t / sqrt(t² + g²), t = tan(pi f / sr), g = tan(pi f1 / sr)
    t, g = np.tan(np.pi * np.minimum(f, sr * 0.499) / sr), math.tan(math.pi * f1 / sr)
    return 20 * np.log10(t / np.sqrt(t * t + g * g))


def peak_db(fp, q, gdb, f, sr):
    A = 10 ** (gdb / 40); w0 = 2 * math.pi * fp / sr; al = math.sin(w0) / (2 * q); c = math.cos(w0)
    b = [1 + al * A, -2 * c, 1 - al * A]; a = [1 + al / A, -2 * c, 1 - al / A]
    z = np.exp(-1j * 2 * np.pi * f / sr)
    return 20 * np.log10(np.abs((b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)) + 1e-12)


def engine(model):
    from calib_fx import STEP_LEVELS
    sr = model["dry"]["sr"]
    mc = model["mastercomp"]; inp = np.array(mc["in_db"])
    ref = next(r for r in mc["rows"] if r["drive"] == 0.5 and r["speed"] == 0.5)
    inv = lambda out: mc_inv(inp, ref["out_db"], out)
    E = {}
    # master: how much louder (per input level) the stored drive/speed comes out than drive .5 — the browser's
    # fixed master (DEVICE_MASTER) stands for drive .5, so the difference goes in front of it as a level-dependent gain
    E["master"] = {"in_db": mc["in_db"], "ref": {"drive": 0.5, "speed": 0.5},
                   "rows": [{"drive": r["drive"], "speed": r["speed"],
                             "gain_db": [round(float(a - b), 2) for a, b in zip(r["out_db"], ref["out_db"])]} for r in mc["rows"]],
                   # the step-ups settle in ~20 ms at speed 0 and ~3 ms at 1; release is not in the recording
                   "attack_ms": [[0, 15], [0.5, 6], [1, 2]], "release_ms": [[0, 300], [0.5, 120], [1, 40]]}
    fc = model["types"]["6"]
    E["fxcomp"] = {"in_db": fc["in_db"], "rows": [{"x": r["x"], "y": r["y"],
                   "gain_db": [round(float(v), 2) for v in inv(r["out_db"]) - inp]} for r in fc["rows"]],
                   "attack_ms": [[0, 15], [0.5, 6], [1, 2]], "release_ms": [[0, 300], [0.5, 120], [1, 40]]}
    # release: how fast each compressor lets go after a loud tone drops by 30 dB ("drop" runs);
    # without those runs the guessed times above stay
    def release(label, key):
        pts = []
        for r in model.get("runs_raw", []):
            if r["label"] != label:
                continue
            h = next((h for h in r["hits"] if h["signal"] == "drop" and h.get("after_db")), None)
            if not h or len(h["after_db"]) < 60:
                continue
            a = np.array(h["after_db"], float)
            # the quiet tone lasts 1.4 s after the drop: settle from 0.9 to 1.3 s (the test sound ends after)
            tail = a[90:130]; tail = tail[tail > -150]
            if not len(tail):
                continue
            start, final = a[1], float(np.median(tail))
            if final - start < 1.0:                       # back within the first 10 ms window: faster than we can see
                pts.append([r[key], 5.0])
                continue
            goal = start + 0.632 * (final - start)
            i = 1 + int(np.argmax(a[1:] >= goal))            # from just after the drop (window 0 is still the loud tone)
            pts.append([r[key], round(max(5.0, i * 10.0), 1)])
        return sorted(pts) if len(pts) >= 2 else None
    rel = release("mastercomp-rel", "speed")
    if rel:
        E["master"]["release_ms"] = rel
    rel = release("fxcomp-rel", "y")
    if rel:
        E["fxcomp"]["release_ms"] = rel
    # distortion: pre-gain into tanh, then post-gain, fitted on the six steps (master undone)
    amps = np.array(STEP_LEVELS); dr = []
    for r in model["types"]["3"]["rows"]:
        tgt = inv(r["out_db"]); best = None
        for pre in np.arange(-12, 60.5, 0.5):
            m = tanh_rms_db(pre, amps); post = float(np.mean(tgt - m)); err = float(np.mean(np.abs(tgt - m - post)))
            if best is None or err < best[0]:
                best = (err, pre, post)
        dr.append({"x": r["x"], "y": r["y"], "pre_db": float(best[1]), "post_db": round(best[2], 2), "err_db": round(best[0], 2)})
    E["distortion"] = {"rows": dr}
    # filter: low-pass biquad below the middle, above it one-pole high-pass (one cutoff per X) + a resonance peak per Y
    F = model["types"]["5"]; f = np.array(F["bands_hz"]); fr = []
    rows = {(r["x"], r["y"]): r for r in F["rows"]}
    for r in F["rows"]:
        if r["x"] < 0.5:
            fr.append({"x": r["x"], "y": r["y"], "mode": "lp", "f": r["f0"], "q": r["q"], "g": r["gain_db"], "err": r["err_db"]})
        elif r["x"] == 0.5:
            fr.append({"x": 0.5, "y": r["y"], "mode": "off", "g": 0})
    for x in (0.75, 1.0):
        best = None
        for f1 in 100 * 2 ** (np.arange(0, 80) / 10):
            if f1 > sr * 0.45:
                break
            tot, per = 0, []
            for y in (0.0, 0.5, 1.0):
                resp = np.array([np.nan if v is None else v for v in rows[(x, y)]["resp"]]); ok = np.isfinite(resp) & (f > 30) & (f < 16000)
                base = onepole_hp_db(f1, f[ok]); b_ = None
                for fp in ([1000] if y == 0 else 1000 * 2 ** (np.arange(0, 45) / 10)):
                    if fp > sr * 0.45:
                        break
                    for q in ((0.7,) if y == 0 else (0.7, 1.5, 3, 6)):
                        for gdb in ((0,) if y == 0 else (0, 3, 6, 9, 12, 15, 18)):
                            mm = base + (peak_db(fp, q, gdb, f[ok], sr) if gdb else 0)
                            g = float(np.median(resp[ok] - mm)); e = float(np.mean(np.abs(resp[ok] - mm - g)))
                            if b_ is None or e < b_[0]:
                                b_ = (e, fp, q, gdb, g)
                tot += b_[0]; per.append((y,) + b_)
            if best is None or tot < best[0]:
                best = (tot, f1, per)
        for y, e, fp, q, gdb, g in best[2]:
            fr.append({"x": x, "y": y, "mode": "hp", "f": round(float(best[1]), 1), "pf": round(float(fp), 1), "pq": q, "pg": gdb,
                       "g": round(g, 2), "err": round(e, 2)})
    E["filter"] = {"rows": fr}
    # delay: wet level by the FX fader (dry stays), time by X, feedback by Y
    E["delay"] = {"send_db": [[0, -120], [0.25, -22.3], [0.5, -10.2], [0.75, -4.1], [1, 0]],
                  "time_ms": [[0, 12]] + [[r["x"], r["time_ms"]] for r in model["types"]["1"]["rows"] if r.get("time_ms")],
                  "feedback": [[0, 0], [0.5, 0.44], [1, 0.99]], "echo_db": -0.8, "spread": 1.02}
    E["reverb"] = {"ir": model["types"]["2"]["ir"], "sr": model["types"]["2"]["sr"]}
    # chorus: the rate is the wobble the K.O. makes on a sine; depth, copy level and base delay tuned in the browser
    # until its wobble per side and its level match the recordings (3…5 dB at Y ≤ ½, 8…11 at Y 1)
    E["chorus"] = {"rate_hz": [[0, 0.4], [0.5, 2.4], [1, 18]], "depth_ms": [[0, 0.25], [1, 0.5]],
                   "wet": [[0, 0.32], [0.5, 0.34], [1, 0.5]], "base_ms": 7.6}
    model["engine"] = E
    return model


if __name__ == "__main__":
    m = run(sys.argv[1])
    print(json.dumps({k: (v if k != "types" else {t: {kk: (vv if kk != "rows" else len(vv)) for kk, vv in tv.items() if kk != "ir"} for t, tv in v.items()}) for k, v in m.items() if k not in ("mastercomp",)}, indent=None)[:3000])
