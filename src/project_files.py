"""Build movable project folders and publish complete, aligned stem exports."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import struct
import tempfile
import threading
import time

import ep133proj
import ep133write


def publish(stage, parent, label):
    dest = Path(parent) / (label + time.strftime(" %H%M%S ") + secrets.token_hex(3))
    Path(stage).rename(dest)
    return dest


class StemExport:
    def __init__(self, parent, label, midi_name, midi):
        self.parent, self.label = parent, label
        self.temp = tempfile.TemporaryDirectory(prefix=".render-", dir=parent)
        self.stage = Path(self.temp.name)
        (self.stage / midi_name).write_bytes(midi)
        self.files = {}
        self.lock = threading.Lock()

    def add(self, name, body, size):
        if Path(name).name != name or not name.endswith(".wav") or not name.startswith("P"):
            raise ValueError("Invalid stem name")
        head = body.read(58)
        if len(head) != 58 or head[:4] != b"RIFF" or head[8:16] != b"WAVEfmt " or head[50:54] != b"data":
            raise ValueError("Invalid stem header")
        frames = struct.unpack_from("<I", head, 46)[0]
        if size != 58 + frames * 8 or struct.unpack_from("<HHI", head, 20) != (3, 2, 48000):
            raise ValueError("Expected a complete stereo float WAV at 48 kHz")
        with self.lock:
            partial = self.stage / (name + ".part")
            try:
                with partial.open("wb") as f:
                    f.write(head)
                    remaining = size - len(head)
                    while remaining:
                        block = body.read(min(remaining, 256 * 1024))
                        if not block:
                            raise ValueError("Incomplete stem")
                        f.write(block)
                        remaining -= len(block)
                partial.replace(self.stage / name)
                self.files[name] = frames
            finally:
                partial.unlink(missing_ok=True)

    def finish(self, expected, reference=None):
        with self.lock:
            if not self.files or len(self.files) != expected:
                raise ValueError("Export is missing stems")
            frames = max(self.files.values())
            for name, length in self.files.items():
                if length == frames:
                    continue
                with (self.stage / name).open("r+b") as f:
                    for pos, value in ((4, 50 + frames*8), (46, frames), (54, frames*8)):
                        f.seek(pos)
                        f.write(struct.pack("<I", value))
                    f.truncate(58 + frames*8)
            if reference:
                shutil.copyfile(reference, self.stage / "REF - K.O. II (live, unaligned).wav")
            return publish(self.stage, self.parent, self.label)


def collect(parent, project, raw, patch, settings, read_sound):
    edited = ep133write.apply_patch(raw, patch) if patch else raw
    arr = ep133proj.parse_archive(edited)
    slots = sorted({p["sym"] for group in arr["pads"].values() for p in group.values() if p.get("sym")})
    with tempfile.TemporaryDirectory(prefix=".collect-", dir=parent) as tmp:
        stage = Path(tmp)
        (stage / "Samples").mkdir()
        (stage / "Project.tar").write_bytes(edited)
        manifest = dict(settings, version=1, project=project, samples={})
        pad_names = {}
        for slot in slots:
            wav, metadata = read_sound(slot)
            rel = f"Samples/{slot:03d}.wav"
            (stage / rel).write_bytes(wav)
            manifest["samples"][slot] = {"file": rel, "metadata": metadata,
                                         "sha256": hashlib.sha256(wav).hexdigest()}
        for group, pads in arr["pads"].items():
            for pad, info in pads.items():
                sample = manifest["samples"].get(info.get("sym"))
                if sample:
                    pad_names[group + pad] = sample["metadata"].get("name", "")
        scenes = {s["n"]: s for s in arr["scenes"]}
        order = settings.get("songOrder") if settings.get("mode") == "song" else list(scenes)
        sequence = []
        for n in order or []:
            if n not in scenes:
                continue
            scene = dict(scenes[n], label=(settings.get("labels") or {}).get(str(n), ""))
            count = int((settings.get("repeats") or {}).get(str(n), 1))
            sequence.extend([scene] * max(1, min(4, count)))
        midi = ep133proj.to_midi(dict(arr, project=project, scenes=sequence), pad_names,
                                 bpm=settings.get("bpm") or arr["bpm"], pad_cc=True)
        (stage / "Arrangement.mid").write_bytes(midi)
        manifest["song"] = arr["song"]
        (stage / "Station.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        return publish(stage, parent, "Collection"), len(slots)
