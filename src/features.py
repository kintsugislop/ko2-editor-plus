"""Sound features for 'similar sounds' -- runs in a Python that has librosa.

stdin: one JSON [id, path] per line. stdout: one JSON [id, [floats]] or [id, null].
The vector: 20 MFCC means + 20 stds, spectral centroid / bandwidth / rolloff /
flatness, zero-crossing rate, loudness mean and spread, onset rate, length --
enough to tell a dusty kick from a clean one, or a pad from a pluck.
"""
import sys, json, warnings
warnings.filterwarnings("ignore")
import numpy as np
import librosa

for line in sys.stdin:
    try:
        fid, path = json.loads(line)
    except Exception:
        continue
    try:
        y, sr = librosa.load(path, sr=22050, mono=True, duration=6.0)
        if y.size < 2048 or not np.any(y):
            raise ValueError("silent or too short")
        m = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=20)
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
        cen = librosa.feature.spectral_centroid(S=S, sr=sr)[0]
        bw = librosa.feature.spectral_bandwidth(S=S, sr=sr)[0]
        ro = librosa.feature.spectral_rolloff(S=S, sr=sr)[0]
        fl = librosa.feature.spectral_flatness(S=S)[0]
        zc = librosa.feature.zero_crossing_rate(y)[0]
        rms = librosa.feature.rms(S=S)[0]
        on = librosa.onset.onset_detect(y=y, sr=sr, units="time")
        dur = len(y) / sr
        v = list(m.mean(axis=1)) + list(m.std(axis=1)) + [
            float(np.log1p(cen.mean())), float(np.log1p(bw.mean())), float(np.log1p(ro.mean())),
            float(np.log10(fl.mean() + 1e-6)), float(zc.mean()),
            float(20 * np.log10(rms.mean() + 1e-6)), float(20 * np.log10(rms.std() + 1e-6)),
            float(len(on) / max(dur, 0.1)), float(np.log1p(dur))]
        out = [fid, [round(float(x), 4) for x in v]]
    except Exception:
        out = [fid, None]
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
