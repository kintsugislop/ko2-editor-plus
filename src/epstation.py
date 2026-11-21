#!/usr/bin/env python3
"""
epstation - a local workstation UI for a connected Teenage Engineering EP-133 K.O. II (also EP-40 / EP-1320).

Owns the MIDI port and exposes it to a browser UI on http://127.0.0.1:8800.
Record captures USB audio and lists takes. Project edits scenes, notes and automation. Storage manages device sounds, and Library indexes local samples.

Levels run whenever the UI is watching, not just while recording: ./usbrec --meter
is held open by Monitor and shut down again once nobody polls /api/levels.

  python3 epstation.py [--port 8800] [--device EP-133]     # K.O. II (default)
  python3 epstation.py --device EP-40                     # riddim

Each device keeps its own inventory / project cache: <slug>_sounds.json, <slug>_projects.json.
"""
import project_files
import secrets
import tempfile
from pathlib import Path

import json, os, re, sys, time, threading, traceback, zipfile, io, argparse, subprocess, signal
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote

from ep import EP, ok as ok_status

from paths import SOURCE_DIR, DATA_DIR, LIBRARY_DIR

CACHE = os.path.join(DATA_DIR, "cache")
BACKUP = os.path.join(DATA_DIR, "backup")
LOG = os.path.join(DATA_DIR, "station.log")
RECDIR = os.path.join(DATA_DIR, "recordings")
USBREC = os.path.join(SOURCE_DIR, "usbrec")
SOUNDS_NODE = 1000
PROJECTS_NODE = 2000

# set in main() from --device, so an EP-40 and a K.O. II never share caches
INVENTORY = PROJECTS = None
SLUG = "ep133"


def model_path(filename):
    local = DATA_DIR / filename
    return local if local.is_file() else SOURCE_DIR / "defaults" / filename


def set_device_paths(device):
    global INVENTORY, PROJECTS, CACHE, SLUG
    SLUG = re.sub(r"[^a-z0-9]", "", device.lower()) or "ep"
    INVENTORY = os.path.join(DATA_DIR, f"{SLUG}_sounds.json")
    PROJECTS = os.path.join(DATA_DIR, f"{SLUG}_projects.json")
    # the EP-40 cache predates per-device folders -- keep using it for that device
    CACHE = os.path.join(DATA_DIR, "cache") if SLUG == "ep40" else os.path.join(DATA_DIR, "cache", SLUG)
    os.makedirs(CACHE, exist_ok=True)


os.makedirs(BACKUP, exist_ok=True)
os.makedirs(RECDIR, exist_ok=True)


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def wav_header(nch, sr, nbytes, bits=16):
    block = nch * bits // 8
    return (b"RIFF" + (36 + nbytes).to_bytes(4, "little") + b"WAVEfmt "
            + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
            + nch.to_bytes(2, "little") + sr.to_bytes(4, "little")
            + (sr * block).to_bytes(4, "little") + block.to_bytes(2, "little")
            + bits.to_bytes(2, "little") + b"data" + nbytes.to_bytes(4, "little"))


def to_wav(raw, nch, sr):
    """Wrap device bytes as a WAV. Handles raw PCM, an existing RIFF, or a
    JSON-prefixed payload (the EP writes a metadata header on some exports)."""
    if raw[:4] == b"RIFF":
        return raw
    raw = to_pcm(raw)
    return wav_header(nch, sr, len(raw)) + raw


def to_pcm(raw):
    """Device bytes -> bare s16le PCM (drops a JSON header or RIFF wrapper)."""
    if raw[:4] == b"RIFF":
        i = 12
        while i + 8 <= len(raw):
            cid, n = raw[i:i+4], int.from_bytes(raw[i+4:i+8], "little")
            if cid == b"data":
                if i + 8 + n > len(raw):
                    raise ValueError("Incomplete WAV data")
                return raw[i+8:i+8+n]
            i += 8 + n + (n & 1)
        raise ValueError("WAV has no data chunk")
    if raw.startswith(b"{"):
        # A PCM sample can itself start with 0x7b. Strip only an actual metadata object.
        try:
            metadata, end = json.JSONDecoder().raw_decode(raw[:4096].decode("latin-1"))
        except ValueError:
            metadata = None
        if isinstance(metadata, dict) and metadata.keys() & {"channels", "samplerate", "format", "name", "sample.start"}:
            if raw[end:end+2] == b"\r\n":
                end += 2
            elif raw[end:end+1] in (b"\x00", b"\n"):
                end += 1
            raw = raw[end:]
    if len(raw) % 2:
        raw = raw[:-1]
    return raw


# K.O. II sound banks: fixed ranges of the 999 slots (the EP-40 sends its own "tabs")
DEFAULT_TABS = [{"name": n, "range": r} for n, r in (
    ("KICKS", (1, 99)), ("SNARES", (100, 199)), ("CYMBALS", (200, 299)),
    ("PERC", (300, 399)), ("BASS", (400, 499)), ("MELODIC", (500, 599)),
    ("LOOPS", (600, 699)), ("USER 1", (700, 799)), ("USER 2", (800, 899)),
    ("SFX", (900, 999)))]


def slot_number(nid, fname):
    """Slot as the device shows it: '042.pcm' -> 42, else the node id."""
    m = re.match(r"^(\d{1,4})", fname or "")
    return int(m.group(1)) if m else nid


class Station:
    """Serialized access to the device, with lazy connect and auto-recovery."""

    def __init__(self, device="EP-133"):
        self.device_name = device
        self.lock = threading.RLock()
        self.dev = None
        self.info = {}
        self.storage = {}
        self.sounds = []
        self.scan = {"running": False, "done": 0, "total": 0, "error": None}
        self.projects = {"active": None, "list": [], "read_at": None}
        self.pscan = {"running": False, "done": 0, "total": 0, "error": None}
        self.tjob = {"running": False, "stage": None, "error": None, "result": None}
        self.pjob = {"running": False, "stage": None, "error": None, "result": None}
        self.cjob = {"running": False, "stage": None, "error": None}
        self.last_fail = 0.0
        self.fail_cooldown = 4.0
        self.load_cached_inventory()
        self.load_cached_projects()

    # ---------- inventory ----------

    def load_cached_inventory(self):
        try:
            d = json.load(open(INVENTORY))
            self.sounds = d.get("sounds", [])
            self.storage = d.get("storage", {})
            log(f"loaded cached inventory: {len(self.sounds)} sounds")
        except Exception as e:
            log(f"no cached inventory ({e})")

    def save_inventory(self):
        json.dump({"device": self.info, "storage": self.storage, "sounds": self.sounds},
                  open(INVENTORY, "w"), indent=2)

    # ---------- projects ----------
    #
    # The device keeps its own grouping: projects 01..09, each with pad groups
    # A..D of twelve pads. A pad's metadata names the sound sitting on it
    # ("sym" is the slot number) plus how that pad is set up. Patterns are not
    # in the node tree. The arrangement is read from the project archive.

    def load_cached_projects(self):
        try:
            self.projects = json.load(open(PROJECTS))
            log(f"loaded cached projects: {len(self.projects.get('list', []))}")
        except Exception:
            pass

    def read_projects(self):
        """Walk projects -> groups -> pads. ~530 MIDI round trips, so it runs
        in the background and the result is cached."""
        if self.pscan["running"]:
            return
        self.pscan = {"running": True, "done": 0, "total": 0, "error": None}

        def work():
            try:
                def job(d):
                    root = json.loads(d.metadata(PROJECTS_NODE) or "{}")
                    plist = list(d.list_nodes(PROJECTS_NODE))
                    self.pscan["total"] = len(plist) * 4 * 12
                    out = []
                    for pid, _f, _s, pname in plist:
                        kids = list(d.list_nodes(pid))
                        gdir = next((k for k in kids if k[3] == "groups"), None)
                        if not gdir:
                            continue
                        gmeta = json.loads(d.metadata(gdir[0]) or "{}")
                        groups = []
                        for gid, _gf, _gs, gname in d.list_nodes(gdir[0]):
                            padmeta = json.loads(d.metadata(gid) or "{}")
                            pads = []
                            for nid, _pf, _ps, padname in d.list_nodes(gid):
                                try:
                                    m = json.loads(d.metadata(nid) or "{}")
                                except Exception:
                                    m = {}
                                pads.append({"pad": padname, "node": nid,
                                             "sym": m.get("sym", 0),
                                             "playmode": m.get("sound.playmode", ""),
                                             "pitch": m.get("sound.pitch", 0),
                                             "amplitude": m.get("sound.amplitude", 0),
                                             "pan": m.get("sound.pan", 0),
                                             "start": m.get("sample.start", 0),
                                             "end": m.get("sample.end", 0),
                                             "attack": m.get("envelope.attack", 0),
                                             "release": m.get("envelope.release", 0),
                                             "active": nid == padmeta.get("active")})
                                self.pscan["done"] += 1
                            groups.append({"name": gname, "node": gid, "pads": pads,
                                           "active": gid == gmeta.get("active"),
                                           "used": sum(1 for x in pads if x["sym"])})
                        out.append({"name": pname, "node": pid, "groups": groups,
                                    "active": pid == root.get("active"),
                                    "used": sum(g["used"] for g in groups)})
                    return {"active": root.get("active"), "list": out,
                            "read_at": time.strftime("%Y-%m-%d %H:%M")}

                self.projects = self.call(job)
                json.dump(self.projects, open(PROJECTS, "w"), indent=2)
                log(f"projects read: {len(self.projects['list'])} projects")
            except Exception as e:
                self.pscan["error"] = str(e)
                log(f"projects read failed: {e}")
            finally:
                self.pscan["running"] = False

        threading.Thread(target=work, daemon=True).start()

    def set_active(self, project, group=None):
        """Switch the project (and optionally the group) loaded on the device --
        the same metadata write the TE Sample Tool does: /projects {active: node}."""
        project = int(project)
        with self.lock:
            d = self.connect()
            if group is None:
                d.set_metadata(PROJECTS_NODE, {"active": project})
                now = json.loads(d.metadata(PROJECTS_NODE) or "{}").get("active")
                if now != project:
                    raise RuntimeError(f"device kept project node {now}")
                self.projects["active"] = now
                for p in self.projects.get("list", []):
                    p["active"] = p["node"] == now
                log(f"device project -> node {now}")
            else:
                group = int(group)
                active = json.loads(d.metadata(PROJECTS_NODE) or "{}").get("active")
                if active != project:
                    raise RuntimeError("switch to that project first - only the loaded project's group can change")
                gdir = next((k[0] for k in d.list_nodes(project) if k[3] == "groups"), None)
                if gdir is None:
                    raise RuntimeError("project has no groups folder")
                d.set_metadata(gdir, {"active": group})
                now = json.loads(d.metadata(gdir) or "{}").get("active")
                for p in self.projects.get("list", []):
                    if p["node"] == project:
                        for g in p["groups"]:
                            g["active"] = g["node"] == now
                log(f"device group -> node {now}")
        try:
            json.dump(self.projects, open(PROJECTS, "w"), indent=2)
        except Exception:
            pass
        return {"active": self.projects.get("active")}

    # ---------- connection ----------

    def connect(self):
        with self.lock:
            if self.dev:
                return self.dev
            # while the device is unplugged, don't respawn the bridge on every poll
            since = time.time() - self.last_fail
            if since < self.fail_cooldown:
                raise RuntimeError(f"{self.device_name} not connected")
            dev = None
            try:
                dev = EP(self.device_name)
                dev.identify()
                info = dev.greet()
                dev.file_init()
                # right after the device boots its file system answers empty for a moment
                for _ in range(10):
                    if dev.metadata(SOUNDS_NODE):
                        break
                    time.sleep(0.5)
                    dev.file_init()
            except Exception:
                # a connection that didn't come up all the way is closed, never kept half-made
                if dev:
                    try: dev.close()
                    except Exception: pass
                self.last_fail = time.time()
                self.fails = getattr(self, "fails", 0) + 1
                self.fail_cooldown = min(60.0, 4.0 * self.fails)
                raise
            self.dev, self.info = dev, info
            self.last_fail = 0.0
            self.fails, self.fail_cooldown = 0, 4.0
            log(f"connected: {self.info.get('product')} {self.info.get('serial')} "
                f"(chunk {getattr(self.dev, 'chunk', '?')})")
            return self.dev

    def drop(self):
        with self.lock:
            if self.dev:
                try: self.dev.close()
                except Exception: pass
            self.dev = None

    def call(self, fn, *a, **kw):
        """Run a device operation, reconnecting once if the port went away."""
        with self.lock:
            for attempt in (1, 2):
                try:
                    return fn(self.connect(), *a, **kw)
                except Exception:
                    self.drop()
                    if attempt == 2:
                        raise

    def status(self):
        if not self.lock.acquire(timeout=0.3):
            # a trim / restore / scan holds the device: report what we know, don't queue
            return {"connected": self.dev is not None, "busy": True, "device": self.info,
                    "storage": self.storage}
        try:
            def probe(d):
                d.identify()
                # a reboot shows up as a new boot of the file system: if the sounds
                # folder reads empty, start the session over
                if not d.metadata(SOUNDS_NODE):
                    log("device file system reset (reboot?) - reconnecting")
                    raise RuntimeError("file system reset")
            self.call(probe)
            return {"connected": True, "device": self.info, "storage": self.storage}
        except Exception as e:
            return {"connected": False, "error": str(e), "device": self.info,
                    "storage": self.storage}
        finally:
            self.lock.release()

    # ---------- operations ----------

    def rescan(self):
        if self.scan["running"]:
            return
        self.scan = {"running": True, "done": 0, "total": 0, "error": None}

        def work():
            try:
                def job(d):
                    dirmeta = json.loads(d.metadata(SOUNDS_NODE) or "{}")
                    log(f"sounds dir metadata keys: {sorted(dirmeta)}")
                    # the EP-40 names its banks in the metadata; the K.O. II does not,
                    # its banks are fixed hundreds (1-99 kicks, 100-199 snares, ...)
                    tabs = dirmeta.get("tabs") or DEFAULT_TABS

                    def cat(slot):
                        for t in tabs:
                            lo, hi = t["range"]
                            if lo <= slot <= hi:
                                return t["name"]
                        return "?"

                    nodes = list(d.list_nodes(SOUNDS_NODE))
                    self.scan["total"] = len(nodes)
                    out = []
                    for i, (nid, flags, size, fname) in enumerate(nodes, 1):
                        try:
                            m = json.loads(d.metadata(nid))
                        except Exception:
                            m = {}
                        sr = m.get("samplerate") or 46875
                        ch = m.get("channels") or 1
                        num = slot_number(nid, fname)
                        out.append({"slot": nid, "file": fname, "name": m.get("name", ""),
                                    "bytes": size, "channels": ch, "samplerate": sr,
                                    "seconds": round(size / (ch * 2 * sr), 3),
                                    "category": cat(num),
                                    "playmode": m.get("sound.playmode", "")})
                        self.scan["done"] = i
                    return dirmeta, out

                dirmeta, out = self.call(job)
                # a slot whose sample changed on the device invalidates its cached wav
                old = {x["slot"]: (x["bytes"], x.get("name")) for x in self.sounds}
                gone = 0
                for x in out:
                    prev = old.get(x["slot"])
                    if prev is not None and prev != (x["bytes"], x.get("name")):
                        try:
                            os.remove(os.path.join(CACHE, f"{x['slot']:03d}.wav"))
                            gone += 1
                        except OSError:
                            pass
                if gone:
                    log(f"rescan: {gone} changed sounds dropped from the wav cache")
                self.storage = {k: dirmeta[k] for k in ("max_capacity", "free_space_in_bytes")
                                if k in dirmeta}
                self.sounds = out
                self.save_inventory()
                log(f"rescan complete: {len(out)} sounds")
            except Exception as e:
                self.scan["error"] = str(e)
                log(f"rescan failed: {e}")
            finally:
                self.scan["running"] = False

        threading.Thread(target=work, daemon=True).start()

    def by_slot(self, slot):
        for s in self.sounds:
            if s["slot"] == slot:
                return s
        return None

    def play(self, slot):
        self.call(lambda d: d.play(slot))

    def transport(self, action):
        """Play / stop the device's own sequencer (MIDI real time, not SysEx)."""
        self.call(lambda d: d.transport(action))
        log(f"device transport: {action}")

    def wav(self, slot, use_cache=True):
        path = os.path.join(CACHE, f"{slot:03d}.wav")
        if use_cache and os.path.exists(path):
            return open(path, "rb").read()
        meta = self.by_slot(slot)
        if not meta:
            # never guess the format: a wrong header turns stereo 44.1k into noise
            meta = json.loads(self.call(lambda d: d.metadata(slot)) or "{}")
        name, size, raw = self.call(lambda d: d.get_file(slot))
        data = to_wav(raw, meta.get("channels", 1), meta.get("samplerate", 46875))
        with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as f:
            try:
                f.write(data)
                f.flush()
                os.replace(f.name, path)
            finally:
                Path(f.name).unlink(missing_ok=True)
        return data

    # ---------- trim (destructive: replaces the sample on the device) ----------

    # ---------- projects: write an archive back (TE's own restore path) ----------
    def write_project(self, node, make, label, base_sha=None, force=False):
        """make(d, current_archive) -> the archive to write. Runs in the background:
        fresh read, conflict check against base_sha, backup, device moved off the
        project if it is loaded, PUT, read back and compare byte for byte."""
        if self.pjob.get("running") or self.tjob.get("running"):
            raise RuntimeError("the device is busy with another write")
        proj = next((p for p in self.projects.get("list", []) if p["node"] == int(node)), None)
        if not proj:
            raise ValueError("unknown project - read the projects first")
        self.pjob = {"running": True, "node": int(node), "project": proj["name"], "stage": "starting",
                     "done": 0, "total": 0, "error": None, "result": None, "conflict": False}
        threading.Thread(target=self._write_project_work, daemon=True,
                         args=(int(node), proj["name"], make, label, base_sha, force)).start()

    def _write_project_work(self, node, pname, make, label, base_sha, force):
        import hashlib, ep133write
        J = self.pjob
        out = os.path.join(DATA_DIR, "projects_dump")
        os.makedirs(out, exist_ok=True)
        def stage(x):
            J["stage"], J["done"], J["total"] = x, 0, 0
        def prog(done, total):
            J["done"], J["total"] = done, total
        switched = None
        try:
            with self.lock:
                d = self.connect()
                stage("reading the device's copy")
                _n, _s, cur = d.get_file(node, progress=prog)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                bpath = os.path.join(out, f"{SLUG} P{pname} {stamp} before-{label}.tar")
                open(bpath, "wb").write(cur)
                J["backup"] = os.path.basename(bpath)
                if base_sha and hashlib.sha1(cur).hexdigest() != base_sha and not force:
                    J["conflict"] = True
                    raise RuntimeError("the project on the device changed since it was read here "
                                       "(something was recorded or edited on the K.O.) - nothing was written")
                data = make(d, cur)
                if data == cur:
                    J["result"] = {"unchanged": True}
                    stage("done")
                    return
                ep133write.read_members(data)                # must parse, or it doesn't go
                J["changes"] = ep133write.diff(cur, data)
                root = json.loads(d.metadata(PROJECTS_NODE) or "{}")
                if root.get("active") == node:
                    # never write under the running sequencer: step to another project and back
                    other = next(p["node"] for p in self.projects["list"] if p["node"] != node)
                    stage("moving the device off this project")
                    d.set_metadata(PROJECTS_NODE, {"active": other})
                    switched = other
                    time.sleep(0.4)
                stage("writing")
                try:
                    d.put_project(PROJECTS_NODE, node, pname, data, progress=prog)
                    d.file_init()
                except Exception as e:
                    stage("putting the old version back")
                    log(f"project {pname}: write failed ({e}) - restoring {os.path.basename(bpath)}")
                    try:
                        d.put_project(PROJECTS_NODE, node, pname, cur)
                        d.file_init()
                    except Exception as e2:
                        raise RuntimeError(f"write failed AND the old version could not go back ({e2}); "
                                           f"it is saved as {os.path.basename(bpath)}")
                    raise RuntimeError(f"write failed, the project is as it was: {e}")
                stage("reading it back")
                _n, _s, back = d.get_file(node, progress=prog)
                if switched is not None:
                    d.set_metadata(PROJECTS_NODE, {"active": node})
                    switched = None
            path = os.path.join(out, f"{SLUG} P{pname} {time.strftime('%Y%m%d-%H%M%S')} written.tar")
            open(path, "wb").write(back)
            same = back == data
            J["result"] = {"verified": same, "bytes": len(data), "changes": J.get("changes"),
                           "backup": J["backup"], "dump": os.path.basename(path),
                           "mismatch": None if same else ep133write.diff(data, back)}
            log(f"project {pname}: WROTE {len(data)} bytes ({label}), read back "
                + ("identical" if same else f"DIFFERENT {J['result']['mismatch']}") + f", backup {J['backup']}")
            stage("done")
            try:
                self.read_projects()                         # pad nodes may have been rebuilt
            except Exception:
                pass
        except Exception as e:
            J["error"] = str(e)
            J["stage"] = "failed"
            log(f"project {pname}: write FAILED: {e}")
            if switched is not None:
                try:
                    with self.lock:
                        self.connect().set_metadata(PROJECTS_NODE, {"active": node})
                except Exception:
                    pass
            self.drop()
        finally:
            J["running"] = False

    def _calibration_template(self, needs_fx):
        import ep133write
        required = {"settings", "scenes"}
        if needs_fx:
            required.add("fx_settings")
        for path in sorted((DATA_DIR / "projects_dump").glob(f"{SLUG} P*.tar"), reverse=True):
            try:
                raw = path.read_bytes()
                if required.issubset(m["name"] for m in ep133write.read_members(raw)):
                    return raw
            except (ValueError, OSError):
                continue
        raise RuntimeError("Open and read a project with settings on this device before calibrating")

    def _upload_calibration_sounds(self, model, uploaded, stage):
        used = self.call(lambda d: {slot_number(n, f) for n, _, _, f in d.list_nodes(SOUNDS_NODE)})
        sounds = model.sounds()
        available = [n for n in range(999, 899, -1) if n not in used]
        if len(available) < len(sounds):
            raise RuntimeError(f"Need {len(sounds)} free sound slots in 900–999 for calibration")
        frames = []
        for slot, (name, pcm) in zip(available, sounds):
            stage("uploading test sounds", f"{name} -> {slot}")
            self.upload(slot, name, pcm, 1, model.SR)
            self._wait("tjob")
            uploaded.append(slot)
            frames.append(len(pcm) // 2)
        return frames

    # ---------- measure the sidechain duck on the device itself ----------
    def _sc_cal_work(self, node, pname, py):
        """One write of the borrowed project per (length, shape); MIDI hits play it, no PLAY
        needed. The project, the active project and the test sounds are put back after."""
        import calib_sc
        J = self.cjob
        slots, original, prev_active = [], None, None
        out = os.path.join(DATA_DIR, "calibration"); os.makedirs(out, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        def stage(s, d=""):
            if (J.get("stage"), J.get("detail")) != (s, d):       # log a change once, not every poll
                log(f"sidechain cal: {s} {d}")
            J["stage"], J["detail"] = s, d
        note_b = pad_midi_note(1, 0)                    # pad B01 (file 1)
        note_a = pad_midi_note(0, 0)                    # pad A01
        try:
            devs = RECORDER.devices().get("devices", [])
            dev = next((x["name"] for x in devs if "EP-133" in x["name"] or "K.O" in x["name"]), None)
            if not dev:
                raise RuntimeError("the K.O. is not showing up as an audio input")
            tpl = self._calibration_template(needs_fx=True)
            with self.lock:
                d = self.connect()
                dev_active = json.loads(d.metadata(PROJECTS_NODE) or "{}").get("active")
            cached = self.projects.get("active")
            prev_active = dev_active if dev_active and dev_active != node else (cached if cached != node else dev_active)
            frames = self._upload_calibration_sounds(calib_sc, slots, stage)
            other = next(p["node"] for p in self.projects["list"] if p["node"] != node)
            with self.lock:
                d = self.connect()
                _n, _s, original = d.get_file(node)
            open(os.path.join(DATA_DIR, "projects_dump", f"{SLUG} P{pname} {stamp} before-sidechain-cal.tar"), "wb").write(original)
            plan = {"runs": [], "slots": slots}
            RECORDER.start(dev, f"sidechain cal P{pname}")
            t_rec = time.time()
            time.sleep(0.5)
            runs = getattr(self, "sc_runs", None) or calib_sc.CAL_RUNS
            self.sc_runs = None
            for i, (L, S) in enumerate(runs):
                stage("measuring", f"{i + 1}/{len(runs)} · length {L:.2f} shape {S:.2f}")
                data = calib_sc.build(original, tpl, slots, frames, L, S)
                with self.lock:
                    d = self.connect()
                    d.set_metadata(PROJECTS_NODE, {"active": other}); time.sleep(0.3)
                    d.put_project(PROJECTS_NODE, node, pname, data)
                    d.file_init()
                    d.set_metadata(PROJECTS_NODE, {"active": node})
                time.sleep(1.0)
                run = {"length": L, "shape": S, "t_trig": []}
                send_midi([[0x90, note_b, 100]]); run["t_noise"] = time.time() - t_rec
                time.sleep(0.8)
                for _ in range(3):
                    send_midi([[0x90, note_a, 100]]); run["t_trig"].append(time.time() - t_rec)
                    time.sleep(0.05); send_midi([[0x80, note_a, 0]])
                    time.sleep(1.75)
                send_midi([[0x80, note_b, 0]])
                time.sleep(0.3)
                plan["runs"].append(run)
                if not RECORDER.running():
                    raise RuntimeError(RECORDER.error or "the recorder stopped")
            RECORDER.stop(); time.sleep(0.8)
            take = RECORDER.path
            json.dump(plan, open(os.path.join(out, f"{stamp} sc plan.json"), "w"), indent=1)
            stage("analysing the recording")
            r = subprocess.run([py, os.path.join(SOURCE_DIR, "calib_sc.py"), "analyze", take, os.path.join(out, f"{stamp} sc plan.json")],
                               capture_output=True, text=True, timeout=300, cwd=DATA_DIR)
            if r.returncode != 0:
                raise RuntimeError("analysis failed: " + r.stderr.strip()[-400:])
            res = json.loads(r.stdout)
            json.dump(res, open(os.path.join(out, f"{stamp} sc result.json"), "w"), indent=1)
            json.dump(dict(res["model"], measured_at=stamp), open(os.path.join(DATA_DIR, f"{SLUG}_sidechain.json"), "w"), indent=1)
            J["summary"] = res.get("summary", "")
            stage("putting things back")
        except Exception as e:
            J["error"] = str(e)
            log(f"sidechain cal FAILED: {e}")
        finally:
            try:
                if RECORDER.running():
                    RECORDER.stop()
                send_midi([[0xB0, 123, 0]])                   # all notes off
            except Exception:
                pass
            if original is not None:
                try:
                    with self.lock:
                        d = self.connect()
                        other = next(p["node"] for p in self.projects["list"] if p["node"] != node)
                        d.set_metadata(PROJECTS_NODE, {"active": other}); time.sleep(0.3)
                        d.put_project(PROJECTS_NODE, node, pname, original)
                        d.file_init()
                        _n, _s, back = d.get_file(node)
                    if back != original:
                        J["error"] = (J["error"] + "; " if J["error"] else "") + f"project {pname} read back different - see projects_dump"
                except Exception as e:
                    J["error"] = (J["error"] + "; " if J["error"] else "") + f"project {pname} not put back ({e})"
            if prev_active:
                try:
                    self.set_active(prev_active)
                except Exception:
                    pass
            if slots:
                try:
                    self.delete([s for s in slots if self.by_slot(s)], backup=False)
                except Exception:
                    pass
            J["stage"] = "failed" if J["error"] else "done"
            J["running"] = False

    # ---------- record the master effects on the device itself ----------
    def _fx_cal_work(self, node, pname, py):
        """One write per FX setting, MIDI hits on test pads in group B, one long recording.
        The project, the active project and the test sounds are put back after."""
        import calib_fx
        J = self.cjob
        slots, original, prev_active = [], None, None
        out = os.path.join(DATA_DIR, "calibration"); os.makedirs(out, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        def stage(s, d=""):
            if (J.get("stage"), J.get("detail")) != (s, d):       # log a change once, not every poll
                log(f"fx cal: {s} {d}")
            J["stage"], J["detail"] = s, d
        try:
            devs = RECORDER.devices().get("devices", [])
            dev = next((x["name"] for x in devs if "EP-133" in x["name"] or "K.O" in x["name"]), None)
            if not dev:
                raise RuntimeError("the K.O. is not showing up as an audio input")
            tpl = self._calibration_template(needs_fx=True)
            with self.lock:
                d = self.connect()
                dev_active = json.loads(d.metadata(PROJECTS_NODE) or "{}").get("active")
            cached = self.projects.get("active")
            prev_active = dev_active if dev_active and dev_active != node else (cached if cached != node else dev_active)
            frames = self._upload_calibration_sounds(calib_fx, slots, stage)
            other = next(p["node"] for p in self.projects["list"] if p["node"] != node)
            with self.lock:
                d = self.connect()
                _n, _s, original = d.get_file(node)
            open(os.path.join(DATA_DIR, "projects_dump", f"{SLUG} P{pname} {stamp} before-fx-cal.tar"), "wb").write(original)
            runs = calib_fx.runs()
            plan = {"runs": [], "slots": slots}
            RECORDER.start(dev, f"fx cal P{pname}")
            t_rec = time.time()
            time.sleep(0.5)
            for i, r in enumerate(runs):
                stage("measuring", f"{i + 1}/{len(runs)} · {r['label']} x {r['x']:.2f} y {r['y']:.2f}"
                      + (f" send {r['send']:.2f}" if r["label"].startswith("send") else "")
                      + (f" drive {r['drive']:.2f} speed {r['speed']:.2f}" if r["label"] == "mastercomp" else ""))
                data = calib_fx.build(original, tpl, slots, frames, r)
                with self.lock:
                    d = self.connect()
                    d.set_metadata(PROJECTS_NODE, {"active": other}); time.sleep(0.25)
                    d.put_project(PROJECTS_NODE, node, pname, data)
                    d.file_init()
                    d.set_metadata(PROJECTS_NODE, {"active": node})
                time.sleep(0.8)
                r = dict(r, t_hits=[])
                for j, sig in enumerate(r["signals"]):
                    n = pad_midi_note(1, calib_fx.PADS[sig] - 1)
                    send_midi([[0x90, n, 100]]); r["t_hits"].append([sig, time.time() - t_rec])
                    time.sleep(0.06); send_midi([[0x80, n, 0]])
                    time.sleep((r["secs"] if j == 0 else 1.2) - 0.06)
                send_midi([[0xB0, 123, 0]])
                plan["runs"].append(r)
                if not RECORDER.running():
                    raise RuntimeError(RECORDER.error or "the recorder stopped")
            RECORDER.stop(); time.sleep(0.8)
            take = RECORDER.path
            json.dump(plan, open(os.path.join(out, f"{stamp} fx plan.json"), "w"), indent=1)
            stage("cutting the recording into runs")
            r = subprocess.run([py, os.path.join(SOURCE_DIR, "calib_fx.py"), "analyze", take, os.path.join(out, f"{stamp} fx plan.json"),
                                os.path.join(out, "fx")], capture_output=True, text=True, timeout=600, cwd=DATA_DIR)
            if r.returncode != 0:
                raise RuntimeError("analysis failed: " + r.stderr.strip()[-400:])
            res = json.loads(r.stdout)
            rpath = os.path.join(out, f"{stamp} fx result.json")
            json.dump(res, open(rpath, "w"), indent=1)
            # the model the browser plays, rebuilt from this recording (the one it replaces is kept)
            stage("building the effects model")
            mpath = os.path.join(DATA_DIR, f"{SLUG}_fxmodel.json")
            if os.path.exists(mpath):
                import shutil
                shutil.copy(mpath, os.path.join(out, f"{stamp} fxmodel before.json"))
            r2 = subprocess.run([py, os.path.join(SOURCE_DIR, "calib_fx_fit.py"), rpath],
                                capture_output=True, text=True, timeout=900, cwd=DATA_DIR)
            if r2.returncode != 0:
                raise RuntimeError("model fit failed: " + r2.stderr.strip()[-400:])
            J["summary"] = res.get("summary", "") + " · effects model rebuilt"
            stage("putting things back")
        except Exception as e:
            J["error"] = str(e)
            log(f"fx cal FAILED: {e}")
        finally:
            try:
                if RECORDER.running():
                    RECORDER.stop()
                send_midi([[0xB0, 123, 0]])
            except Exception:
                pass
            if original is not None:
                try:
                    with self.lock:
                        d = self.connect()
                        other = next(p["node"] for p in self.projects["list"] if p["node"] != node)
                        d.set_metadata(PROJECTS_NODE, {"active": other}); time.sleep(0.3)
                        d.put_project(PROJECTS_NODE, node, pname, original)
                        d.file_init()
                        _n, _s, back = d.get_file(node)
                    if back != original:
                        J["error"] = (J["error"] + "; " if J["error"] else "") + f"project {pname} read back different"
                except Exception as e:
                    J["error"] = (J["error"] + "; " if J["error"] else "") + f"project {pname} not put back ({e})"
            if prev_active:
                try:
                    self.set_active(prev_active)
                except Exception:
                    pass
            if slots:
                try:
                    self.delete([s for s in slots if self.by_slot(s)], backup=False)
                except Exception:
                    pass
            J["stage"] = "failed" if J["error"] else "done"
            J["running"] = False

    # ---------- measure the faders on the device itself ----------
    def calibrate(self, node, kind="faders"):
        import library
        py = library.Library.feature_python(calibration=True)
        if not py:
            raise RuntimeError("Install NumPy in .venv before starting a device measurement")
        if self.cjob.get("running") or self.pjob.get("running") or self.tjob.get("running"):
            raise RuntimeError("the device is busy")
        if RECORDER.running():
            raise RuntimeError("a recording is running - stop it first")
        proj = next((p for p in self.projects.get("list", []) if p["node"] == int(node)), None)
        if not proj:
            raise ValueError("unknown project - read the projects first")
        self.cjob = {"running": True, "stage": "starting", "detail": "", "error": None, "summary": None}
        work = {"sidechain": self._sc_cal_work, "fx": self._fx_cal_work}.get(kind, self._calibrate_work)
        args = (int(node), proj["name"], py) if kind in ("sidechain", "fx") else (int(node), proj["name"], py, kind)
        threading.Thread(target=work, args=args, daemon=True).start()

    def _wait(self, attr):
        while getattr(self, attr).get("running"):
            time.sleep(0.2)
        j = getattr(self, attr)
        if j.get("error"):
            raise RuntimeError(j["error"])
        return j

    def _calibrate_work(self, node, pname, py, kind="faders"):
        if kind == "pads":
            import calib_pads as calib               # pad settings, play modes, voices
        else:
            import calib                             # the group faders
        J = self.cjob
        slots, original, prev_active, wrote = [], None, None, False
        out = os.path.join(DATA_DIR, "calibration"); os.makedirs(out, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        def stage(s, d=""):
            if (J.get("stage"), J.get("detail")) != (s, d):       # log a change once, not every poll
                log(f"calibrate: {s} {d}")
            J["stage"], J["detail"] = s, d
        try:
            # the input the K.O. records on
            devs = RECORDER.devices().get("devices", [])
            dev = next((x["name"] for x in devs if "EP-133" in x["name"] or "EP-1320" in x["name"] or "K.O" in x["name"]), None)
            if not dev:
                raise RuntimeError("the K.O. is not showing up as an audio input (" + ", ".join(x["name"] for x in devs) + ")")
            # a template for settings/scenes: any project read here that has them
            tpl = self._calibration_template(needs_fx=False)
            # the project the K.O. is on now (writing a project makes the device load it)
            with self.lock:
                d = self.connect()
                dev_active = json.loads(d.metadata(PROJECTS_NODE) or "{}").get("active")
            # the device's own record can already say the borrowed project; then trust the last one set here
            cached = self.projects.get("active")
            prev_active = dev_active if dev_active and dev_active != node else (cached if cached != node else dev_active)
            log(f"calibrate: device on project node {dev_active}, station last set {cached} -> will return to {prev_active}")
            # test sounds into free SFX slots
            frames = self._upload_calibration_sounds(calib, slots, stage)
            # the test project, written over the borrowed one (its version is kept)
            stage("writing the test project", f"into project {pname}")
            plan = {}
            def make(d, cur):
                nonlocal original
                original = cur
                data, p = calib.build(cur, tpl, slots, frames)
                plan.update(p)
                return data
            self.write_project(node, make, "calibration")
            self._wait("pjob")
            wrote = True
            if kind == "pads":
                # what the device made of the test pads' bytes (its own names for the settings)
                stage("reading back the test pads")
                want = set(plan.get("pads", {}))

                def walk(d):
                    back = {}
                    gdir = next((k for k in d.list_nodes(node) if k[3] == "groups"), None)
                    for gid, _f, _s, gname in (d.list_nodes(gdir[0]) if gdir else []):
                        for nid, _pf, _ps, padname in d.list_nodes(gid):
                            key = gname.upper() + padname[-2:]
                            if key in want:
                                try:
                                    back[key] = json.loads(d.metadata(nid) or "{}")
                                except Exception as e:
                                    back[key] = {"error": str(e)}
                    return back
                try:
                    plan["readback"] = self.call(walk)
                except Exception as e:
                    plan["readback"] = {"error": str(e)}
            json.dump(plan, open(os.path.join(out, f"{stamp} plan.json"), "w"), indent=1)
            total = plan.get("total_s") or len(plan["steps"]) * plan["step_s"]
            # the sequencer must start from bar 1 while we record: a K.O. still playing (PLAY pressed
            # earlier) would start mid-way and shift every step, so stop it and wait for silence first
            self.transport("stop")
            self.set_active(node)
            time.sleep(0.8)
            stage("recording", f"from {dev}")
            RECORDER.start(dev, f"calibration P{pname}")
            quiet_since, t_wait, heard = time.time(), time.time(), False
            while True:                                  # the K.O. must be stopped before the take starts
                time.sleep(0.1)
                if RECORDER.meter.get("peak", 0) > 0.01:
                    quiet_since, heard = time.time(), True
                    if time.time() - t_wait > 2:
                        stage("press STOP on the K.O.", "it is still playing - the test has to start from its first bar")
                    if time.time() - t_wait > 60:
                        raise RuntimeError("the K.O. kept playing - press STOP on it and run the calibration again")
                elif time.time() - quiet_since > 0.8:
                    break
            if heard:
                # it was playing: load the test project afresh so PLAY starts at its first bar
                # (a stopped K.O. keeps its place), and start a clean take without the old playback
                stage("reloading the test project", "so PLAY starts from the first bar - wait for “press PLAY”")
                RECORDER.stop(); time.sleep(0.5)
                RECORDER.start(dev, f"calibration P{pname}")   # recording from now: an early PLAY is still caught
                other = next(p["node"] for p in self.projects["list"] if p["node"] != node)
                self.set_active(other); time.sleep(0.8)
                self.set_active(node); time.sleep(1.0)
            stage("recording", f"from {dev}")
            self.transport("start")
            t_sound, t_ask = None, time.time()
            while True:
                time.sleep(0.1)
                if not RECORDER.running():
                    raise RuntimeError(RECORDER.error or "the recorder stopped")
                if t_sound is None:
                    if RECORDER.meter.get("peak", 0) > 0.02:
                        t_sound = time.time()
                        stage("measuring", f"{round(total)} s of test sounds — don't touch the K.O.")
                    elif time.time() - t_ask > 4:
                        stage("press PLAY on the K.O.", f"project {pname} is loaded; it didn't start from MIDI")
                        if time.time() - t_ask > 300:
                            raise RuntimeError("the K.O. never started playing")
                    continue
                left = t_sound + total + 1.5 - time.time()
                J["detail"] = f"{max(0, round(left))} s left — don't touch the K.O."
                if left <= 0:
                    break
            self.transport("stop")
            RECORDER.stop()
            time.sleep(0.8)
            take = RECORDER.path
            if not os.path.exists(take):
                raise RuntimeError("the recording did not arrive")
            # analysis in a Python that has numpy (the one the library uses for 'similar')
            stage("analysing the recording")
            script = "calib_pads.py" if kind == "pads" else "calib.py"
            r = subprocess.run([py, os.path.join(SOURCE_DIR, script), "analyze", take, os.path.join(out, f"{stamp} plan.json")],
                               capture_output=True, text=True, timeout=300, cwd=DATA_DIR)
            if r.returncode != 0:
                raise RuntimeError("analysis failed: " + r.stderr.strip()[-400:])
            res = json.loads(r.stdout)
            json.dump(res, open(os.path.join(out, f"{stamp} result.json"), "w"), indent=1)
            # a take that began mid-way (the K.O. was already playing) shifts every step: keep what we had
            t0 = (res.get("measured") or {}).get("t0")
            if t0 is not None and t0 < 0.5:
                raise RuntimeError("the K.O. was already playing when the recording started, so the steps don't line up - "
                                   "press STOP on the K.O., run it again and press PLAY when asked. Nothing was changed.")
            if kind != "pads":
                sc_ = res.get("scales", {})
                bad = []
                if "1" in sc_ and abs(sc_["1"]["max"] - sc_["1"]["min"]) < 8: bad.append("PITCH")
                if "9" in sc_ and abs(sc_["9"]["max"] - sc_["9"]["min"]) < 16: bad.append("TUNE")
                if "0" in sc_ and sc_["0"]["min"] > -40: bad.append("LEVEL")
                if "8" in sc_ and abs(sc_["8"]["max"] - sc_["8"]["min"]) < 1: bad.append("PAN")
                if bad:
                    raise RuntimeError("the measurement looks wrong (" + ", ".join(bad) + ") - kept the previous scales; "
                                       f"see calibration/{stamp} result.json")
            if kind == "pads":
                md = res.get("model", {})
                pts = lambda key: {int(a[0]): a[1] for a in md.get(key, [])}
                amp, vel, pit = pts("amp_db"), pts("vel_db"), pts("pitch_st")
                bad = []
                if not (-40 < amp.get(50, -999) < -5 and amp.get(150, -999) > 0): bad.append("pad level")
                if not (vel.get(127, -999) >= vel.get(64, 999) >= vel.get(8, 999)): bad.append("velocity")
                if not abs(pit.get(-12, 0) + 12) < 1: bad.append("pitch")
                if bad:
                    raise RuntimeError("the measurement looks wrong (" + ", ".join(bad) + ") - kept the previous pad model; "
                                       f"see calibration/{stamp} result.json")
                model = dict(res["model"], readback=plan.get("readback", {}), measured_at=stamp)
                json.dump(model, open(os.path.join(DATA_DIR, f"{SLUG}_padmodel.json"), "w"), indent=1)
                J["summary"] = res.get("summary", "")
                stage("putting things back")
                return
            path = os.path.join(DATA_DIR, f"{SLUG}_scales.json")
            try:
                scales = json.load(open(path))
            except Exception:
                scales = {}
            scales.update(res["scales"])
            json.dump(scales, open(path, "w"), indent=1)
            sc = res["scales"]
            bits = []
            if "3" in sc: bits.append(f"LPF {sc['3']['min']:.0f} Hz … {sc['3']['max']/1000:.1f} kHz, {sc['3'].get('slope')} dB/oct")
            if "4" in sc: bits.append(f"HPF {sc['4']['min']:.0f} Hz … {sc['4']['max']/1000:.1f} kHz, {sc['4'].get('slope')} dB/oct")
            if "1" in sc: bits.append(f"PITCH {sc['1']['min']:+.1f} … {sc['1']['max']:+.1f} st")
            if "9" in sc: bits.append(f"TUNE {sc['9']['min']:+.1f} … {sc['9']['max']:+.1f} st")
            if "0" in sc: bits.append(f"LEVEL {sc['0']['min']:.0f} … {sc['0']['max']:.0f} dB")
            if "2" in sc: bits.append("TIME changes " + ("speed" if sc["2"].get("kind") == "speed" else "length"))
            J["summary"] = " · ".join(bits)
            stage("putting things back")
        except Exception as e:
            J["error"] = str(e)
            log(f"calibrate FAILED: {e}")
        finally:
            try:
                if RECORDER.running():
                    RECORDER.stop()
                self.transport("stop")
            except Exception:
                pass
            if wrote and original is not None:
                try:
                    self.write_project(node, lambda d, cur: original, "after-calibration")
                    self._wait("pjob")
                except Exception as e:
                    J["error"] = (J["error"] + "; " if J["error"] else "") + f"project {pname} not put back ({e}) - use versions…"
            if prev_active and prev_active != node:
                try:
                    self.set_active(prev_active)
                except Exception:
                    pass
            if slots:
                try:
                    self.delete([s for s in slots if self.by_slot(s)], backup=False)
                except Exception:
                    pass
            J["stage"] = "failed" if J["error"] else "done"
            J["running"] = False

    def trim_status(self):
        return dict(self.tjob)

    def free_space(self, d):
        return int(json.loads(d.metadata(SOUNDS_NODE) or "{}").get("free_space_in_bytes", 0))

    def wait_free(self, d, need, timeout=30.0):
        """A delete frees its space a moment later, not at once. The Sample Tool
        polls free space before uploading into it -- so do we."""
        t0, free = time.time(), 0
        while time.time() - t0 < timeout:
            free = self.free_space(d)
            if free >= need:
                return free
            time.sleep(0.5)
        raise RuntimeError(f"device has {free} bytes free, needs {need} "
                           f"(waited {timeout:.0f}s)")

    def free_slot(self, d):
        """A slot number with nothing in it right now, asked of the device itself."""
        used = {slot_number(nid, fn) for nid, _f, _s, fn in d.list_nodes(SOUNDS_NODE)}
        for n in list(range(999, 899, -1)) + list(range(1, 900)):
            if n not in used:
                return n
        raise RuntimeError("no free slot for the upload probe")

    def _probe_put(self, d, fname, nch, sr):
        probe = self.free_slot(d)
        pid = d.put_file(SOUNDS_NODE, probe, fname, b"\0\0" * nch * 2048, {"channels": nch, "samplerate": sr})
        st, _ = d.delete(pid)
        if not ok_status(st):
            log(f"probe file left in slot {pid} (delete status {st})")
        log(f"upload probe ok (slot {probe})")

    @staticmethod
    def _logprog(tag):
        mark = {"n": -1}
        def f(done, total):
            step = int(done * 10 / max(1, total))
            if step != mark["n"]:
                mark["n"] = step
                log(f"{tag}: {done}/{total} bytes")
        return f

    def slice(self, slot, regions, mono=False):
        """Cut a sample into one or more pieces and write them back.

        regions: [{start, end (seconds), fade_in, fade_out (ms), curve_in, curve_out}]
        The first piece replaces the original in its own slot (pads that play it are
        shifted to match); the rest go into the next free slots after it, skipping
        any that are taken. The original is saved to ./backup first and put back
        if the first upload fails."""
        if self.tjob.get("running"):
            raise RuntimeError("a trim is already running")
        clean = []
        import base64
        for r in regions or []:
            c = {"start": float(r["start"]), "end": float(r["end"]),
                 "fade_in": max(0.0, float(r.get("fade_in", 0) or 0)),
                 "fade_out": max(0.0, float(r.get("fade_out", 0) or 0)),
                 "curve_in": str(r.get("curve_in", "lin")), "curve_out": str(r.get("curve_out", "lin"))}
            if r.get("pcm"):
                # already rendered in the browser (resampled): s16le, fades and mono applied
                c["pcm"] = base64.b64decode(r["pcm"])
                c["channels"] = 2 if int(r.get("channels", 1)) == 2 else 1
                c["samplerate"] = max(1000, min(65535, int(r["samplerate"])))
                if len(c["pcm"]) % (2 * c["channels"]):
                    raise ValueError("rendered piece has a torn frame")
            clean.append(c)
        if not clean:
            raise ValueError("nothing to keep")
        if len(clean) > 48:
            raise ValueError("at most 48 pieces at once")
        self.tjob = {"running": True, "slot": slot, "stage": "starting", "done": 0, "total": 0,
                     "error": None, "result": None}
        threading.Thread(target=self._trim_work, daemon=True, args=(slot, clean, bool(mono))).start()

    def upload(self, slot, name, pcm, channels, samplerate, meta=None, replace=False):
        """Put a new sound (s16le PCM prepared in the browser) into a slot. An empty
        slot unless replace=True: then the sound there is saved to ./backup, deleted,
        and put back if the new one fails to go in."""
        if self.tjob.get("running"):
            raise RuntimeError("the device is busy with another write")
        if not (1 <= slot <= 999):
            raise ValueError("slot must be 1..999")
        channels = 2 if int(channels) == 2 else 1
        samplerate = max(1000, min(65535, int(samplerate)))
        if len(pcm) % (2 * channels) or len(pcm) < 64:
            raise ValueError("bad audio data")
        self.tjob = {"running": True, "slot": slot, "stage": "starting", "done": 0, "total": 0,
                     "error": None, "result": None}
        extra = {k: v for k, v in (meta or {}).items()
                 if k in ("time.mode", "sound.bpm", "sound.bars", "sound.playmode", "sound.rootnote")}
        threading.Thread(target=self._upload_work, daemon=True,
                         args=(slot, name, pcm, channels, samplerate, extra, bool(replace))).start()

    def _upload_work(self, slot, name, pcm, ch, sr, extra=None, replace=False):
        J = self.tjob
        try:
            label = "".join(c for c in (name or "") if 32 <= ord(c) < 127).strip()[:16] or f"{slot:03d}"
            with self.lock:
                d = self.connect()
                used = {slot_number(nid, fn) for nid, _f, _s, fn in d.list_nodes(SOUNDS_NODE)}
                if slot in used and not replace:
                    raise RuntimeError(f"slot {slot} is taken - pick an empty one")
                free = self.free_space(d)
                def prog(done, total):
                    J["done"], J["total"] = done, total
                old = None
                if slot in used:
                    # replace: keep the old sound on disk first, then free the slot
                    J["stage"] = "saving the old sound"
                    ometa = json.loads(d.metadata(slot) or "{}")
                    och, osr = int(ometa.get("channels") or 1), int(ometa.get("samplerate") or 46875)
                    _n, _sz, raw = d.get_file(slot, progress=prog)
                    opcm = to_pcm(raw)
                    if len(pcm) + 4096 > free + len(opcm):
                        raise RuntimeError(f"needs {len(pcm) // 1024} KB, even with slot {slot} freed only "
                                           f"{(free + len(opcm)) // 1024} KB are there")
                    olabel = ometa.get("name", "") or "unnamed"
                    osafe = re.sub(r"[^A-Za-z0-9 _-]", "", olabel)[:40].strip() or "unnamed"
                    bpath = os.path.join(BACKUP, f"{slot:03d} {osafe} {time.strftime('%Y%m%d-%H%M%S')} before-replace.wav")
                    with open(bpath, "wb") as f:
                        f.write(wav_header(och, osr, len(opcm)) + opcm)
                    with open(bpath[:-4] + ".json", "w") as f:
                        json.dump(ometa, f, indent=1)
                    old = {"meta": ometa, "pcm": opcm, "ch": och, "sr": osr, "backup": os.path.basename(bpath), "name": olabel}
                    log(f"replace {slot}: {olabel!r} backed up -> {old['backup']}")
                    J["stage"] = "freeing the slot"
                    st, _ = d.delete(slot)
                    if not ok_status(st):
                        raise IOError(f"device refused to free slot {slot} (status {st})")
                    J["stage"] = "waiting for space"
                    free = self.wait_free(d, len(pcm) + 4096)
                elif len(pcm) + 4096 > free:
                    raise RuntimeError(f"needs {len(pcm) // 1024} KB, the device has {free // 1024} KB free")
                J["stage"] = "upload"
                try:
                    nid = d.put_file(SOUNDS_NODE, slot, label, pcm, {"channels": ch, "samplerate": sr}, progress=prog)
                except Exception as e:
                    if not old:
                        raise
                    J["stage"] = "restoring the old sound"
                    try:
                        self.wait_free(d, len(old["pcm"]) + 4096, timeout=60)
                        fn = re.sub(r"[^\x20-\x7e]", "", old["name"]).replace("/", "")[:16].lower() or f"{slot:03d}"
                        rid = d.put_file(SOUNDS_NODE, slot, fn, old["pcm"], {"channels": old["ch"], "samplerate": old["sr"]})
                        d.set_metadata(rid, {k: v for k, v in old["meta"].items() if k != "crc"})
                        d.set_metadata(rid, {"name": old["name"]})
                    except Exception as e2:
                        raise RuntimeError(f"upload failed AND restore failed ({e2}) - the old sound is in backup/{old['backup']}")
                    raise RuntimeError(f"upload failed, the old sound is back in slot {slot}: {e}")
                n = len(pcm) // (2 * ch)
                d.set_metadata(nid, {"channels": ch, "samplerate": sr, "format": "s16",
                                     "sample.start": 0, "sample.end": n})
                d.set_metadata(nid, {"name": label})
                if extra:
                    d.set_metadata(nid, extra)
                fixed = 0
                if old:
                    # pads that played the old sound kept its start/end in frames: open them to the new length
                    J["stage"] = "pads"
                    for pr in self.projects.get("list", []):
                        for g in pr["groups"]:
                            for p in g["pads"]:
                                if p.get("sym") != slot:
                                    continue
                                try:
                                    pm = json.loads(d.metadata(p["node"]) or "{}")
                                    if pm.get("sym") == slot and (pm.get("sample.start", 0) or pm.get("sample.end", 0)):
                                        d.set_metadata(p["node"], {"sample.start": 0, "sample.end": n})
                                        p["start"], p["end"] = 0, n
                                        fixed += 1
                                except Exception as e:
                                    log(f"replace {slot}: pad {p.get('node')} not reset ({e})")
                    if fixed:
                        json.dump(self.projects, open(PROJECTS, "w"), indent=2)
                d.file_init()
            with open(os.path.join(CACHE, f"{slot:03d}.wav"), "wb") as f:
                f.write(wav_header(ch, sr, len(pcm)) + pcm)
            cat = next((t["name"] for t in DEFAULT_TABS if t["range"][0] <= slot <= t["range"][1]), "?")
            self.sounds = [x for x in self.sounds if x["slot"] != slot]
            self.sounds.append({"slot": slot, "file": f"{slot:03d}.pcm", "name": label, "bytes": len(pcm),
                                "channels": ch, "samplerate": sr, "seconds": round(len(pcm) / (ch * 2 * sr), 3),
                                "category": cat, "playmode": ""})
            self.sounds.sort(key=lambda x: x["slot"])
            if "free_space_in_bytes" in self.storage:
                self.storage["free_space_in_bytes"] = max(0, free - len(pcm))
            self.save_inventory()
            J["result"] = {"slot": slot, "name": label, "bytes": len(pcm)}
            if old:
                J["result"].update({"replaced": old["name"], "backup": old["backup"], "pads_fixed": fixed})
            J["stage"] = "done"
            log(f"UPLOADED {label!r} -> slot {slot} ({len(pcm)} bytes, {ch} ch @ {sr} Hz)"
                + (f", replacing {old['name']!r} (backup {old['backup']}, {fixed} pads opened)" if old else ""))
        except Exception as e:
            J["error"] = str(e)
            J["stage"] = "failed"
            log(f"upload FAILED slot {slot}: {e}")
            self.drop()
        finally:
            J["running"] = False

    def send_take_to_pad(self, target, name, pcm, ch, rate):
        if ch not in (1, 2) or not 1000 <= rate <= 65535 or len(pcm) < 64 or len(pcm) % (2*ch):
            raise ValueError("Invalid sample format or selection too short")
        with self.lock:
            if any(job.get("running") for job in (self.tjob, self.pjob, self.cjob, self.pscan)):
                raise RuntimeError("Device busy; try again after the current operation")
            proj = next(p for p in self.projects["list"] if p["node"] == target["project"])
            group = next(g for g in proj["groups"] if g["name"] == target["group"])
            pad = next(p for p in group["pads"] if p["node"] == target["pad"])
            d = self.connect()
            def check_pad():
                current = json.loads(d.metadata(pad["node"]) or "{}")
                if "sym" not in current and not any(i == pad["node"] for i, *_ in d.list_nodes(group["node"])):
                    raise ValueError("Pad no longer exists; read projects again")
                if current.get("sym", 0) != target["expected"]:
                    raise ValueError("Pad assignment changed; read projects again")
            check_pad()
            used = {slot_number(i, fn) for i, _, _, fn in d.list_nodes(SOUNDS_NODE)}
            slot = next((i for i in range(1, 1000) if i not in used), None)
            if slot is None:
                raise ValueError("No empty sound slots")
            self.tjob = {"running": True, "slot": slot, "done": 0, "total": 0, "error": None}
            self._upload_work(slot, name, pcm, ch, rate)
            if self.tjob["error"]:
                raise RuntimeError(self.tjob["error"])
            frames = len(pcm) // (2*ch)
            try:
                check_pad()
                values = {"sym": slot, "sample.start": 0, "sample.end": frames}
                if not target["expected"]:
                    values.update({"sound.amplitude": 100, "sound.playmode": "oneshot"})
                status, _ = d.set_metadata(pad["node"], values)
                got = json.loads(d.metadata(pad["node"]))
                if not ok_status(status) or any(got.get(k) != v for k, v in values.items()):
                    raise IOError("Pad read-back mismatch")
            except Exception as e:
                raise RuntimeError(f"Sound saved in slot {slot:03d}; pad assignment unverified: {e}") from e
            pad.update(sym=slot, start=0, end=frames)
            if not target["expected"]:
                pad.update(amplitude=100, playmode="oneshot")
            group["used"] = sum(bool(p.get("sym")) for p in group["pads"])
            proj["used"] = sum(g["used"] for g in proj["groups"])
            with open(PROJECTS, "w") as f:
                json.dump(self.projects, f, indent=2)
            return {"slot": slot, "frames": frames}

    def device_free(self):
        with self.lock:
            return self.free_space(self.connect())

    def trim(self, slot, start_s, end_s, mono=False, fade_in_ms=0.0, fade_out_ms=0.0):
        """One piece: cut to [start_s, end_s] and write it back into the same slot."""
        self.slice(slot, [{"start": start_s, "end": end_s, "fade_in": fade_in_ms, "fade_out": fade_out_ms}], mono)

    @staticmethod
    def _curve(kind, x):
        """Fade gain for 0..1 progress: lin, exp (slow start), log (fast start), s."""
        if kind == "exp":
            return x * x
        if kind == "log":
            return x ** 0.5
        if kind == "s":
            return x * x * (3 - 2 * x)
        return x

    def _render_piece(self, pcm, ch, sr, frames, r, mono):
        """-> (offset in original frames, frames, channels, s16le bytes, samplerate)"""
        a = max(0, min(frames, int(round(r["start"] * sr))))
        if "pcm" in r:
            n = len(r["pcm"]) // (2 * r["channels"])
            if n < 16:
                raise ValueError("a piece is shorter than 16 frames")
            return a, n, r["channels"], r["pcm"], r["samplerate"]
        return self._cut_piece(pcm, ch, sr, frames, r, mono) + (sr,)

    def _cut_piece(self, pcm, ch, sr, frames, r, mono):
        import array
        a = max(0, min(frames, int(round(r["start"] * sr))))
        b = max(0, min(frames, int(round(r["end"] * sr))))
        if b - a < max(16, sr // 200):
            raise ValueError("a piece is shorter than 5 ms")
        x = array.array("h")
        x.frombytes(pcm[a * 2 * ch:b * 2 * ch])
        if sys.byteorder != "little":
            x.byteswap()
        nch = ch
        if mono and ch == 2:
            x = array.array("h", ((x[i] + x[i + 1]) // 2 for i in range(0, len(x), 2)))
            nch = 1
        n = len(x) // nch
        fi = min(n, int(sr * r["fade_in"] / 1000.0))
        fo = min(n - fi, int(sr * r["fade_out"] / 1000.0))
        for i in range(fi):
            g = self._curve(r["curve_in"], i / fi)
            for c in range(nch):
                x[i * nch + c] = int(x[i * nch + c] * g)
        for i in range(fo):
            g = self._curve(r["curve_out"], i / fo)
            k = n - 1 - i
            for c in range(nch):
                x[k * nch + c] = int(x[k * nch + c] * g)
        if sys.byteorder != "little":
            x.byteswap()
        return a, n, nch, x.tobytes()

    def _trim_work(self, slot, regions, mono):
        J = self.tjob

        def prog(stage):
            def f(done, total):
                J["stage"], J["done"], J["total"] = stage, done, total
            return f

        def stage(name):
            J["stage"], J["done"], J["total"] = name, 0, 0

        try:
            with self.lock:                      # no auto-retry: every step here is one-shot
                d = self.connect()
                meta = json.loads(d.metadata(slot) or "{}")
                if not meta:
                    raise RuntimeError(f"slot {slot} is empty")
                ch = int(meta.get("channels") or 1)
                sr = int(meta.get("samplerate") or 46875)
                stage("download")
                _n, _sz, raw = d.get_file(slot, progress=prog("download"))
                pcm = to_pcm(raw)
                frames = len(pcm) // (2 * ch)

                label = meta.get("name", "") or "unnamed"
                safe = re.sub(r"[^A-Za-z0-9 _-]", "", label)[:40].strip() or "unnamed"
                stamp = time.strftime("%Y%m%d-%H%M%S")
                bpath = os.path.join(BACKUP, f"{slot:03d} {safe} {stamp} before-trim.wav")
                with open(bpath, "wb") as f:
                    f.write(wav_header(ch, sr, len(pcm)) + pcm)
                with open(bpath[:-4] + ".json", "w") as f:
                    json.dump(meta, f, indent=1)
                log(f"trim {slot}: original backed up -> {os.path.basename(bpath)}")

                pieces = [self._render_piece(pcm, ch, sr, frames, r, mono) for r in regions]
                a, n, nch, new, nsr = pieces[0]
                ratio = nsr / sr                 # pads and loops are counted in frames

                # where the other pieces go: the next free slots after this one
                used = {slot_number(nid, fn) for nid, _f, _s, fn in d.list_nodes(SOUNDS_NODE)}
                targets, cand = [slot], slot + 1
                for _ in pieces[1:]:
                    while cand <= 999 and cand in used:
                        cand += 1
                    if cand > 999:
                        cand = next((k for k in range(1, 1000) if k not in used and k not in targets), None)
                        if cand is None:
                            raise RuntimeError("no free slots left for the extra pieces")
                    targets.append(cand); used.add(cand); cand += 1
                need = sum(len(p[3]) for p in pieces) + 4096 * len(pieces)
                free = self.free_space(d)
                if need > free + len(pcm):
                    raise RuntimeError(f"pieces need {need // 1024} KB, only {(free + len(pcm)) // 1024} KB "
                                       "can be freed - make fewer or shorter pieces, or use mono")
                J["targets"] = targets

                newmeta = {k: v for k, v in meta.items() if k != "crc"}
                newmeta.update({"channels": nch, "samplerate": nsr, "format": "s16",
                                "sample.start": 0, "sample.end": n})
                ls, le = meta.get("sound.loopstart", -1), meta.get("sound.loopend", -1)
                if isinstance(ls, (int, float)) and ls >= 0:
                    ls, le = int(round((int(ls) - a) * ratio)), int(round((int(le) - a) * ratio))
                    if ls < 0 or le > n or le <= ls:
                        ls, le = -1, -1
                    newmeta["sound.loopstart"], newmeta["sound.loopend"] = ls, le
                fname = re.sub(r"[^\x20-\x7e]", "", label).replace("/", "").replace('"', "")[:16].lower() or f"{slot:03d}"
                head = {"channels": nch, "samplerate": nsr}

                stage("probe")
                if self.free_space(d) >= 64 * 1024:
                    self._probe_put(d, fname, nch, sr)
                else:
                    log(f"trim {slot}: memory full - skipping the upload probe")

                stage("replace")
                st, _ = d.delete(slot)
                if not ok_status(st):
                    raise IOError(f"device refused to free slot {slot} (status {st})")
                try:
                    stage("waiting for space")
                    self.wait_free(d, len(new) + 4096)
                    nid = d.put_file(SOUNDS_NODE, slot, fname, new, head, progress=prog("upload"))
                    d.set_metadata(nid, newmeta)
                    if meta.get("name"):
                        d.set_metadata(nid, {"name": meta["name"]})
                except Exception as e:
                    log(f"trim {slot}: upload failed ({e}) - restoring the original")
                    stage("restoring")
                    try:
                        self.wait_free(d, len(pcm), timeout=60)
                        rid = d.put_file(SOUNDS_NODE, slot, fname, pcm,
                                         {"channels": ch, "samplerate": sr},
                                         progress=prog("restoring"))
                        d.set_metadata(rid, {k: v for k, v in meta.items() if k != "crc"})
                        if meta.get("name"):
                            d.set_metadata(rid, {"name": meta["name"]})
                        raise RuntimeError(f"upload failed, original restored: {e}")
                    except RuntimeError:
                        raise
                    except Exception as e2:
                        raise RuntimeError(f"upload failed AND restore failed ({e2}) - "
                                           f"the original is in backup/{os.path.basename(bpath)}")

                # the extra pieces, each into its own free slot
                extra, missed = [], []
                for k, (pa, pn, pch, pdata, psr) in enumerate(pieces[1:], 2):
                  try:
                    tgt = targets[k - 1]
                    stage(f"piece {k}/{len(pieces)}")
                    self.wait_free(d, len(pdata) + 4096)
                    pname = (label[:13] + f"-{k}")[:16]
                    pm = {kk: v for kk, v in newmeta.items() if not kk.startswith("sound.loop")}
                    pm.update({"channels": pch, "samplerate": psr, "sample.start": 0, "sample.end": pn})
                    pid = d.put_file(SOUNDS_NODE, tgt, pname, pdata, {"channels": pch, "samplerate": psr},
                                     progress=prog(f"piece {k}/{len(pieces)}"))
                    d.set_metadata(pid, pm)
                    d.set_metadata(pid, {"name": pname})
                    extra.append({"slot": tgt, "name": pname, "bytes": len(pdata), "channels": pch,
                                  "samplerate": psr, "seconds": round(len(pdata) / (pch * 2 * psr), 3)})
                    log(f"slice {slot}: piece {k} -> slot {tgt} ({len(pdata)} bytes)")
                  except Exception as e:
                    # piece 1 is already in place; say which extra didn't make it
                    missed.append({"piece": k, "slot": targets[k - 1], "error": str(e)})
                    log(f"slice {slot}: piece {k} FAILED ({e}) - carrying on")
                d.file_init()

                # pads that play this slot keep their own start/end in frames
                stage("pads")
                pads = [p for pr in self.projects.get("list", []) for g in pr["groups"] for p in g["pads"]]
                J["done"], J["total"] = 0, len(pads)
                fixed = 0
                for i, p in enumerate(pads, 1):
                    J["done"] = i
                    try:
                        pm = json.loads(d.metadata(p["node"]) or "{}")
                    except Exception:
                        continue
                    if pm.get("sym") != slot:
                        continue
                    ps, pe = int(pm.get("sample.start", 0) or 0), int(pm.get("sample.end", 0) or 0)
                    if pe == 0:
                        continue
                    if ps == 0 and pe >= frames:
                        ns, ne = 0, n
                    else:
                        ns = max(0, int(round((ps - a) * ratio)))
                        ne = min(n, int(round((pe - a) * ratio)))
                        if ne <= ns:
                            ns, ne = 0, n
                    if (ns, ne) != (ps, pe):
                        d.set_metadata(p["node"], {"sample.start": ns, "sample.end": ne})
                        p["start"], p["end"] = ns, ne
                        fixed += 1

            # local bookkeeping
            with open(os.path.join(CACHE, f"{slot:03d}.wav"), "wb") as f:
                f.write(wav_header(nch, nsr, len(new)) + new)
            s = self.by_slot(slot)
            if s:
                s.update({"bytes": len(new), "channels": nch, "samplerate": nsr,
                          "seconds": round(len(new) / (nch * 2 * nsr), 3)})
            cat = (s or {}).get("category", "?")
            for e in extra:
                self.sounds = [x for x in self.sounds if x["slot"] != e["slot"]]
                tabs = DEFAULT_TABS
                ecat = next((t["name"] for t in tabs if t["range"][0] <= e["slot"] <= t["range"][1]), cat)
                self.sounds.append(dict(e, file=f"{e['slot']:03d}.pcm", category=ecat,
                                        playmode=meta.get("sound.playmode", "")))
            self.sounds.sort(key=lambda x: x["slot"])
            self.save_inventory()
            if fixed:
                json.dump(self.projects, open(PROJECTS, "w"), indent=2)
            total_new = len(new) + sum(e["bytes"] for e in extra)
            saved = len(pcm) - total_new
            J["result"] = {"slot": slot, "before": len(pcm), "after": total_new, "saved": saved,
                           "pieces": [{"slot": slot, "bytes": len(new)}] + [{"slot": e["slot"], "bytes": e["bytes"]} for e in extra],
                           "pads_fixed": fixed, "backup": os.path.basename(bpath), "missed": missed}
            J["stage"] = "done"
            log(f"TRIMMED slot {slot} ({label}): {len(pcm)} -> {total_new} bytes in {1 + len(extra)} piece(s), "
                f"{saved / 1048576:.2f} MB freed, {fixed} pads adjusted")
        except Exception as e:
            J["error"] = str(e)
            J["stage"] = "failed"
            log(f"trim FAILED slot {slot}: {e}")
            self.drop()
        finally:
            J["running"] = False

    def rename(self, slot, name):
        self.call(lambda d: d.rename(slot, name))
        s = self.by_slot(slot)
        if s:
            s["name"] = name
        self.save_inventory()
        log(f"renamed slot {slot} -> {name!r}")

    def delete(self, slots, backup=True):
        """Erase slots. Backs each one up to ./backup first unless told not to."""
        done, failed = [], []
        for slot in slots:
            s = self.by_slot(slot)
            label = (s or {}).get("name", "") or "unnamed"
            try:
                if backup:
                    data = self.wav(slot, use_cache=False)
                    safe = re.sub(r"[^A-Za-z0-9 _-]", "", label)[:40].strip() or "unnamed"
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    with open(os.path.join(BACKUP, f"{slot:03d} {safe} {stamp}.wav"), "wb") as f:
                        f.write(data)
                self.call(lambda d: d.delete(slot))
                self.sounds = [x for x in self.sounds if x["slot"] != slot]
                done.append(slot)
                log(f"DELETED slot {slot} ({label}){' [backed up]' if backup else ''}")
            except Exception as e:
                failed.append({"slot": slot, "error": str(e)})
                log(f"delete FAILED slot {slot}: {e}")
        self.save_inventory()
        return {"deleted": done, "failed": failed}



EMPTY_METER = {"secs": 0, "frames": 0, "peak": 0.0, "rms": 0.0, "bands": []}


def parse_meter(line):
    """'METER secs=.. peak=.. rms=.. bands=a,b,c' -> dict, or None if malformed."""
    d = dict(kv.split("=", 1) for kv in line[6:].split() if "=" in kv)
    try:
        return {"secs": float(d.get("secs", 0)), "frames": int(d.get("frames", 0)),
                "peak": float(d.get("peak", 0)), "rms": float(d.get("rms", 0)),
                "bands": [float(x) for x in d.get("bands", "").split(",") if x]}
    except ValueError:
        return None


class Monitor:
    """Runs ./usbrec --meter so the analyser stays live when nothing is recording.

    It writes no audio. The UI 'touches' it on every poll and a watchdog shuts it
    down once nobody is looking, so an idle browser tab costs nothing."""

    IDLE_TIMEOUT = 8.0

    def __init__(self):
        self.proc = None
        self.device = None
        self.meter = dict(EMPTY_METER)
        self.touched = 0.0
        self.error = None
        self.retry_at = 0.0
        self.started_at = 0.0
        self.fails = 0
        self.lock = threading.RLock()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def touch(self, device):
        """Called on every level poll: (re)start metering for this input."""
        with self.lock:
            self.touched = time.time()
            if self.running() and self.device == device:
                return
            if self.running():
                self.stop()
            self._start(device)

    def _start(self, device):
        if not os.path.exists(USBREC) or not device:
            return
        # if usbrec keeps dying straight away (blocked by Gatekeeper, device gone)
        # don't respawn it on every poll -- that is what floods macOS with dialogs
        if time.time() < self.retry_at:
            return
        try:
            self.proc = subprocess.Popen([USBREC, "--history", device, "86400"],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as e:
            self.error = str(e)
            return
        self.device = device
        self.error = None
        self.started_at = self.proc.t0 = time.time()
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        log(f"monitoring levels on {device!r}")

    def _pump(self, proc):
        for line in proc.stderr:
            line = line.strip()
            if line.startswith("METER "):
                m = parse_meter(line)
                if m and proc is self.proc:
                    self.meter = m
            elif "NO AUDIO" in line and not getattr(proc, "stopping", False):
                self.error = ("no audio received - grant Microphone permission to your "
                              "terminal in System Settings > Privacy & Security")
        if proc is self.proc:
            self.meter = dict(EMPTY_METER)
        rc = proc.wait()
        if getattr(proc, "stopping", False):            # stopped on purpose (a take starts, the tab left): not a failure
            return
        if rc != 0 and time.time() - getattr(proc, "t0", self.started_at) < 3.0:
            self.fails += 1
            self.retry_at = time.time() + min(300, 15 * self.fails)
            self.error = self.error or (f"usbrec exited at once (code {rc}) - rebuild it with "
                                        "./station, or it may be blocked by macOS")
            log(f"usbrec died on start (code {rc}); next try in {self.retry_at - time.time():.0f}s")
        else:
            self.fails = 0

    def stop(self):
        with self.lock:
            if self.proc is not None:
                self.proc.stopping = True                 # its exit is ours, not a crash to back off from
            if self.running():
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
            self.meter = dict(EMPTY_METER)

    def keep(self, device):
        import select
        with RECORDER.folder_lock, self.lock:
            if not self.running() or self.device != device or not self.meter.get("frames"):
                raise ValueError("Wait for this input to start monitoring")
            proc = self.proc
            deadline = time.monotonic() + 6
            def read_exact(size):
                chunks = bytearray()
                while len(chunks) < size:
                    if not select.select([proc.stdout], [], [], max(0, deadline-time.monotonic()))[0]:
                        raise TimeoutError("Monitor did not return the recording")
                    chunk = os.read(proc.stdout.fileno(), min(size-len(chunks), 262144))
                    if not chunk:
                        raise IOError("Monitor stopped during capture")
                    chunks.extend(chunk)
                return chunks
            proc.send_signal(signal.SIGUSR1)
            try:
                header = read_exact(44)
                size = int.from_bytes(header[40:44], "little")
                if header[:4] != b"RIFF" or header[36:40] != b"data":
                    raise IOError("Invalid monitor response")
                pcm = read_exact(size)
            except Exception:
                self.stop()
                raise
            if not pcm:
                raise ValueError("Buffer not ready, try again")
            folder = Path(RECORDER.folder)
            with tempfile.NamedTemporaryFile(dir=folder, prefix=".keep-", delete=False) as f:
                tmp = Path(f.name)
            try:
                tmp.write_bytes(header + pcm)
                name = time.strftime("%Y%m%d-%H%M%S") + " Last 30s " + tmp.name[-6:] + ".wav"
                os.replace(tmp, folder / name)
            finally:
                tmp.unlink(missing_ok=True)
            return {"file": name}

    def _watchdog(self):
        while True:
            time.sleep(2.0)
            if self.running() and time.time() - self.touched > self.IDLE_TIMEOUT:
                log("levels idle - stopping monitor")
                self.stop()


class Recorder:
    """Drives ./usbrec to capture a CoreAudio input device to a WAV."""

    LINE = re.compile(r"^\s+(.+?)\s{2,}in=(\d+)\s+(\d+) Hz\s+uid=(.*)$")

    def __init__(self):
        self.settings = LIBRARY_DIR / "takes-folder.json"
        self.folder = RECDIR
        self.folder_lock = threading.RLock()
        self.starting = False
        try:
            folder = json.loads(self.settings.read_text())
            if isinstance(folder, str) and os.path.isabs(folder):
                self.folder = folder
        except (OSError, ValueError):
            pass
        self.proc = None
        self.path = None
        self.device = None
        self.started = 0.0
        self.meter = {"secs": 0, "frames": 0, "peak": 0.0, "rms": 0.0, "bands": []}
        self.last = None
        self.error = None

    def set_folder(self, folder):
        with self.folder_lock:
            if self.running() or self.starting:
                raise ValueError("Stop the recording before changing the takes folder")
            if not os.path.isdir(folder) or not os.access(folder, os.W_OK):
                raise ValueError("Choose a writable folder")
            folder = os.path.realpath(folder)
            self.settings.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.settings.with_suffix(".tmp")
            tmp.write_text(json.dumps(folder))
            tmp.replace(self.settings)
            self.folder = folder

    def choose_folder(self):
        result = subprocess.run(["osascript", "-e",
            'POSIX path of (choose folder with prompt "Choose a folder for KO2 Editor Plus takes")'],
            capture_output=True, text=True)
        if result.returncode:
            if "(-128)" in result.stderr:
                return
            raise RuntimeError(result.stderr.strip() or "Folder picker failed")
        self.set_folder(result.stdout.rstrip("\n"))

    def devices(self):
        try:
            out = subprocess.run([USBREC, "--list"], capture_output=True, text=True, timeout=10).stdout
        except Exception as e:
            return {"error": str(e), "devices": []}
        devs = []
        for line in out.splitlines():
            m = self.LINE.match(line)
            if m:
                devs.append({"name": m.group(1).strip(), "channels": int(m.group(2)),
                             "rate": int(m.group(3)), "uid": m.group(4).strip()})
        return {"devices": devs}

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, device, name=""):
        if self.running():
            raise RuntimeError("already recording")
        if not device:
            raise RuntimeError("no input picked - choose the device in the list")
        with self.folder_lock:
            if self.starting or self.running():
                raise RuntimeError("a take is already starting")
            self.starting = True                # level polls must not restart the meter while the take opens
        try:
            res = self._start(device, name)
            t0 = time.time()                # usbrec that can't open the input exits at once: say so
            while time.time() - t0 < 1.2:
                if self.proc.poll() is not None:
                    time.sleep(0.2)
                    raise RuntimeError(self.error or f"the recorder stopped at once (code {self.proc.returncode})")
                if self.meter.get("frames"):
                    break
                time.sleep(0.05)
            return res
        finally:
            self.starting = False
            MONITOR.retry_at = 0.0

    def _start(self, device, name=""):
        if not os.path.exists(USBREC):
            raise RuntimeError("usbrec not built - run: clang -O2 -o usbrec usbrec.c "
                               "-framework AudioUnit -framework CoreAudio "
                               "-framework AudioToolbox -framework CoreFoundation")
        MONITOR.stop()                      # don't hold the input twice
        safe = re.sub(r"[^\w -]", "", name)[:48].strip() or "take"
        fname = f"{time.strftime('%Y%m%d-%H%M%S')} {safe}.wav"
        if not os.path.isdir(self.folder):
            raise ValueError("Takes folder is unavailable. Reconnect it or choose another folder.")
        self.path = self.take_path(fname)
        suffix = 1
        while os.path.lexists(self.path):
            suffix += 1
            self.path = self.take_path(f"{os.path.splitext(fname)[0]} ({suffix}).wav")
        fname = os.path.basename(self.path)
        self.device = device
        self.error = None
        self.meter = {"secs": 0, "frames": 0, "peak": 0.0, "rms": 0.0, "bands": []}
        self.proc = subprocess.Popen([USBREC, "--rec", device, self.path],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.started = time.time()
        threading.Thread(target=self._pump, daemon=True).start()
        log(f"recording -> {fname}  (input: {device!r})")
        return {"file": fname}

    def _pump(self):
        proc = self.proc
        for line in proc.stderr:
            line = line.strip()
            if line.startswith("METER "):
                m = parse_meter(line)
                if m:
                    self.meter = m
            elif line.startswith("DONE "):
                head, _, path = line[5:].partition("file=")   # path may contain spaces
                d = dict(kv.split("=", 1) for kv in head.split() if "=" in kv)
                self.last = {"file": os.path.basename(path.strip()),
                             "frames": int(d.get("frames", 0)), "secs": float(d.get("secs", 0)),
                             "rate": int(float(d.get("rate", 0))), "channels": int(d.get("ch", 0))}
                log(f"recorded {self.last['file']}  {self.last['secs']:.2f}s")
            elif line and "NO AUDIO" in line:
                self.error = ("no audio received - grant Microphone permission to your terminal "
                              "in System Settings > Privacy & Security")
                log("recording produced no audio (input permission?)")
            elif line and not line.startswith(("recording", "metering")):
                self.error = line

    def stop(self):
        if not self.running():
            return {"running": False, "last": self.last, "error": self.error}
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        time.sleep(0.25)
        return {"running": False, "last": self.last, "error": self.error}

    def status(self):
        return {"running": self.running(), "device": self.device,
                "file": os.path.basename(self.path) if self.path else None,
                "elapsed": round(time.time() - self.started, 1) if self.running() else 0,
                "meter": self.meter, "last": self.last, "error": self.error}

    def take_path(self, fname):
        """Resolve a take name to a path inside the selected folder, refusing anything else."""
        path = os.path.realpath(os.path.join(self.folder, os.path.basename(fname)))
        if os.path.dirname(path) != os.path.realpath(self.folder) or not path.lower().endswith(".wav"):
            raise ValueError("not a take")
        return path

    def delete_take(self, fname):
        path = self.take_path(fname)
        if self.running() and self.path and os.path.realpath(self.path) == path:
            raise RuntimeError("that take is still recording")
        os.remove(path)
        log(f"deleted take {os.path.basename(path)}")

    def rename_take(self, fname, name):
        path = self.take_path(fname)
        safe = re.sub(r"[^\w .-]", "", name)[:64].strip()
        if self.running() and self.path == path:
            raise RuntimeError("Stop the recording before renaming the take")
        if not safe:
            raise ValueError("name cannot be empty")
        if not safe.lower().endswith(".wav"):
            safe += ".wav"
        dest = self.take_path(safe)
        if os.path.exists(dest):
            raise RuntimeError("a take by that name already exists")
        os.rename(path, dest)
        log(f"renamed take {os.path.basename(path)} -> {os.path.basename(dest)}")
        return os.path.basename(dest)

    def takes(self):
        out = []
        folder = self.folder
        for f in sorted(os.listdir(folder), reverse=True):
            if not f.lower().endswith(".wav"):
                continue
            p = os.path.join(folder, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            st = os.stat(p)
            secs = 0
            try:
                import wave
                with wave.open(p) as w:
                    secs = round(w.getnframes() / w.getframerate(), 2)
            except Exception:
                pass
            out.append({"file": f, "bytes": st.st_size, "secs": secs, "mtime": st.st_mtime})
        return out


RECORDER = Recorder()
MONITOR = Monitor()


def pad_midi_note(group, pad):
    # Pad files are numbered from the top left. MIDI counts from the bottom row.
    return 36 + 12 * group + (3 - pad // 3) * 3 + pad % 3


def send_midi(messages):
    def send(device):
        for message in messages:
            device.send_short(message)
    STATION.call(send)


STATION = None
LIB = None


def project_arrangement(node, fresh):
    """Parse a project's archive (pulled from the device if not cached, or if asked)."""
    import ep133proj, ep133write
    proj = next((p for p in STATION.projects.get("list", []) if p["node"] == node), None)
    pname = proj["name"] if proj else str(node)
    out = DATA_DIR / "projects_dump"
    out.mkdir(exist_ok=True)
    raw = None
    if not fresh:
        for path in sorted(out.glob(f"{SLUG} P{pname} *.tar"), reverse=True):
            try:
                candidate = path.read_bytes()
                ep133write.read_members(candidate)
            except (ValueError, OSError) as exc:
                log(f"Skipping unreadable project snapshot {path.name}: {exc}")
                continue
            raw = candidate
            break
    if raw is None:
        _n, _s, raw = STATION.call(lambda d: d.get_file(node))
        ep133write.read_members(raw)
        ep133proj.parse_archive(raw)
        path = out / f"{SLUG} P{pname} {time.strftime('%Y%m%d-%H%M%S')}.tar"
        with tempfile.NamedTemporaryFile(dir=out, delete=False) as part:
            try:
                part.write(raw)
                part.flush()
                os.replace(part.name, path)
            finally:
                Path(part.name).unlink(missing_ok=True)
        log(f"project {pname}: read {len(raw)} bytes of patterns and scenes")
    arr = ep133proj.parse_archive(raw)
    if proj:
        for g in proj["groups"]:
            for pad in g["pads"]:
                dst = arr["pads"].setdefault(g["name"].upper(), {}).setdefault(pad["pad"], {})
                for k in ("playmode", "pitch", "start", "end", "attack", "release"):
                    dst.setdefault(k, pad.get(k))
    import hashlib
    arr["sha"] = hashlib.sha1(raw).hexdigest()
    sc = next((m["data"] for m in ep133write.read_members(raw) if m["name"] == "scenes"), None)
    arr["scene_rows"] = ep133write.scenes_rows(sc)["rows"] if sc else []
    arr["dump"] = os.path.basename(path)
    arr.update({"project": pname, "node": node,
                "read_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))})
    return arr, pname, path


def project_dumps(pname):
    import glob, hashlib
    out = []
    for p in sorted(glob.glob(os.path.join(DATA_DIR, "projects_dump", f"{SLUG} P{pname} *.tar")), reverse=True):
        b = os.path.basename(p)
        m = re.match(r".* (\d{8})-(\d{6})(?: (.+))?\.tar$", b)
        kind = (m.group(3) if m and m.group(3) else "read") if m else "?"
        out.append({"file": b, "kind": kind, "bytes": os.path.getsize(p),
                    "when": f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]} {m.group(2)[:2]}:{m.group(2)[2:4]}:{m.group(2)[4:]}" if m else "",
                    "sha": hashlib.sha1(open(p, "rb").read()).hexdigest()})
    return out


def dump_path(fname):
    path = os.path.realpath(os.path.join(DATA_DIR, "projects_dump", os.path.basename(fname)))
    if os.path.dirname(path) != os.path.realpath(os.path.join(DATA_DIR, "projects_dump")) or not path.endswith(".tar"):
        raise ValueError("not a project dump")
    return path


def params_path():
    return os.path.join(DATA_DIR, f"{SLUG}_params.json")


def load_params():
    """What each fader-automation id means -- named by you, not guessed."""
    try:
        return json.load(open(params_path()))
    except Exception:
        return {}


def device_names():
    """device name -> [(slot, seconds)], what the library matches its files against"""
    dev = {}
    for snd in STATION.sounds:
        secs = snd.get("seconds")
        if secs is None and snd.get("bytes") and snd.get("samplerate"):
            secs = snd["bytes"] / (2 * (snd.get("channels") or 1) * snd["samplerate"])
        dev.setdefault((snd.get("name") or "").strip().lower(), []).append((snd["slot"], secs))
    return dev


def export_dir(pname):
    """One folder per project per day: exports/ep133 P03 2026-09-27/"""
    d = os.path.join(DATA_DIR, "exports", f"{SLUG} P{pname} {time.strftime('%Y-%m-%d')}")
    os.makedirs(d, exist_ok=True)
    return d


STEM_EXPORTS = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---------- helpers ----------

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or "{}")

    def fail(self, e):
        log("error: " + "".join(traceback.format_exception_only(type(e), e)).strip())
        self.send(500, {"error": str(e)})

    def _local_only(self):
        """Only this page may drive the station. A web page on another site could
        otherwise POST to 127.0.0.1 and delete sounds (CSRF), or reach it through a
        hostname that resolves here (DNS rebinding)."""
        port = self.server.server_address[1]
        ok_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in ok_hosts:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in ok_hosts}:
            return False
        return True

    # ---------- routes ----------

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._local_only():
            return self.send(403, {"error": "the station only answers its own page"})
        try:
            if u.path == "/api/health":
                return self.send(200, "KO2 Station", "text/plain; charset=utf-8")

            if u.path in ("/", "/index.html"):
                html = open(os.path.join(SOURCE_DIR, "station.html"), "rb").read()
                return self.send(200, html, "text/html; charset=utf-8")

            if u.path.startswith("/vendor/"):
                # third-party scripts kept as their own files (e.g. lame.min.js for MP3, LGPL)
                name = os.path.basename(u.path)
                path = os.path.join(SOURCE_DIR, "vendor", name)
                if not re.fullmatch(r"[A-Za-z0-9_.-]+\.js", name) or not os.path.isfile(path):
                    return self.send(404, {"error": "not found"})
                return self.send(200, open(path, "rb").read(), "application/javascript")

            if u.path == "/api/sidechain":
                # the duck as measured here (fit: T per length, p per shape); {} until measured with a fit
                try:
                    return self.send(200, json.load(open(model_path(f"{SLUG}_sidechain.json"))).get("fit") or {})
                except Exception:
                    return self.send(200, {})

            if u.path == "/api/calstatus":
                # what has been measured on this device, and when
                out = {}
                for n in ("scales", "padmodel", "sidechain", "fxmodel"):
                    f = model_path(f"{SLUG}_{n}.json")
                    try:
                        d = json.load(open(f))
                    except Exception:
                        out[n] = None
                        continue
                    when = d.get("measured_at") if isinstance(d, dict) else None
                    if n == "scales":
                        when = time.strftime("%Y%m%d-%H%M%S", time.localtime(os.path.getmtime(f)))
                        out[n] = {"when": when, "faders": [k for k, v in d.items() if isinstance(v, dict) and v.get("measured")]}
                    else:
                        out[n] = {"when": when or time.strftime("%Y%m%d-%H%M%S", time.localtime(os.path.getmtime(f)))}
                return self.send(200, out)

            if u.path == "/api/status":
                # with the sound and pad reads in progress, so the page can show them and refresh after
                return self.send(200, dict(STATION.status(), scan=STATION.scan, pscan=STATION.pscan))

            if u.path == "/api/sounds":
                return self.send(200, {"sounds": STATION.sounds, "storage": STATION.storage,
                                       "device": STATION.info, "scan": STATION.scan})

            if u.path == "/api/projects":
                return self.send(200, {"projects": STATION.projects, "scan": STATION.pscan})

            if u.path == "/api/project/arrange":
                node = int(q.get("node", ["0"])[0])
                fresh = q.get("refresh", ["0"])[0] == "1"
                arr, _pn, _p = project_arrangement(node, fresh)
                import ep133proj
                arr["param_names"] = load_params()
                arr["fader_labels"] = ep133proj.FADER_LABELS
                arr["fader_pads"] = ep133proj.FADER_PADS
                return self.send(200, arr)

            if u.path == "/api/scales":
                try:
                    return self.send(200, json.load(open(model_path(f"{SLUG}_scales.json"))))
                except Exception:
                    return self.send(200, {})

            if u.path == "/api/fxmodel":
                try:
                    return self.send(200, json.load(open(model_path(f"{SLUG}_fxmodel.json"))))
                except Exception:
                    return self.send(200, {})

            if u.path.startswith("/api/fxir/"):
                # measured impulse responses of the K.O.'s master FX (calibration/fx/ir)
                name = os.path.basename(unquote(u.path[len("/api/fxir/"):]))
                path = DATA_DIR / "calibration" / ("fx" if SLUG == "ep133" else f"fx-{SLUG}") / "ir" / name
                if SLUG == "ep133" and not path.is_file():
                    path = SOURCE_DIR / "defaults" / "ir" / name
                if not re.fullmatch(r"[A-Za-z0-9_.-]+\.wav", name) or not os.path.isfile(path):
                    return self.send(404, {"error": "not found"})
                return self.send(200, open(path, "rb").read(), "audio/wav")

            if u.path == "/api/padmodel":
                try:
                    return self.send(200, json.load(open(model_path(f"{SLUG}_padmodel.json"))))
                except Exception:
                    return self.send(200, {})

            if u.path == "/api/lib/status":
                return self.send(200, dict(LIB.scan))

            if u.path == "/api/lib/facets":
                return self.send(200, LIB.facets())

            if u.path == "/api/lib/search":
                g = lambda k, d="": q.get(k, [d])[0]
                dev = device_names()
                ids, score = None, {}
                if g("similar"):
                    sim = LIB.similar(int(g("similar")), 150)
                    ids = [i for i, _ in sim]; score = dict(sim)
                res = LIB.search(g("q"), g("type"), g("kind"), g("part"), g("bpm_min") or None,
                                 g("bpm_max") or None, g("key"), g("pack"), g("whole") == "1",
                                 g("fav") == "1", g("tag"), g("sort", "name"),
                                 200 if ids is not None else min(500, int(g("limit", "100"))),
                                 0 if ids is not None else int(g("offset", "0")), dev, ids)
                if score:
                    for r in res["rows"]:
                        r["score"] = score.get(r["id"])
                    res["rows"].sort(key=lambda r: -(r["score"] or 0))
                return self.send(200, res)

            if u.path == "/api/lib/cover":
                # a pack's cover as the Splice app shows it, kept on disk after the first look
                import hashlib
                url = LIB.cover_url(q.get("pack", [""])[0])
                if not url or not url.startswith("https://splice-res.cloudinary.com/"):
                    return self.send(404, {"error": "no cover"})
                cdir = os.path.join(LIBRARY_DIR, "covers"); os.makedirs(cdir, exist_ok=True)
                path = os.path.join(cdir, hashlib.sha1(url.encode()).hexdigest()[:20])
                if not os.path.exists(path):
                    tmp = path + ".part"
                    r = subprocess.run(["curl", "-sfL", "--max-time", "20", "-H", "Accept: image/jpeg,image/png,image/webp",
                                        "-o", tmp, url], capture_output=True)
                    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 100:
                        return self.send(502, {"error": "cover not fetched"})
                    os.replace(tmp, path)
                data = open(path, "rb").read()
                ctype = ("image/png" if data[:4] == b"\x89PNG" else "image/webp" if data[8:12] == b"WEBP"
                         else "image/avif" if data[4:12] == b"ftypavif" else "image/jpeg")
                return self.send(200, data, ctype, {"Cache-Control": "max-age=604800"})

            if u.path == "/api/lib/packs":
                return self.send(200, {"packs": LIB.packs_detail(device_names())})

            if u.path == "/api/lib/matches":
                return self.send(200, {str(k): v for k, v in LIB.device_matches(STATION.sounds).items()})

            if u.path == "/api/lib/file":
                path = LIB.path_of(int(q.get("id", ["0"])[0]))
                if not path or not os.path.isfile(path):
                    return self.send(404, {"error": "file not on disk (moved, or the drive is not connected)"})
                ext = os.path.splitext(path)[1].lower()
                ctype = {".wav": "audio/wav", ".wave": "audio/wav", ".aif": "audio/aiff", ".aiff": "audio/aiff",
                         ".flac": "audio/flac", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".m4a": "audio/mp4"}.get(ext, "application/octet-stream")
                return self.send(200, open(path, "rb").read(), ctype)

            if u.path == "/api/free":
                return self.send(200, {"free": STATION.device_free(),
                                       "used_slots": sorted(x["slot"] for x in STATION.sounds)})

            if u.path == "/api/calibrate/status":
                return self.send(200, dict(STATION.cjob))

            if u.path == "/api/project/write/status":
                return self.send(200, dict(STATION.pjob))

            if u.path == "/api/project/versions":
                pname = q.get("project", [""])[0]
                if not re.fullmatch(r"\d{2}", pname):
                    return self.send(400, {"error": "project=NN"})
                return self.send(200, {"versions": project_dumps(pname)})

            if u.path == "/api/trim/status":
                return self.send(200, STATION.trim_status())

            if u.path == "/api/scan":
                return self.send(200, STATION.scan)

            if u.path == "/api/audio/devices":
                return self.send(200, RECORDER.devices())

            if u.path == "/api/levels":
                dev = (q.get("device", [""])[0] or "").strip()
                if getattr(RECORDER, "starting", False):
                    return self.send(200, {"source": "record", "running": True, "elapsed": 0, "file": None,
                                           "meter": RECORDER.meter, "error": None})
                if RECORDER.running():
                    return self.send(200, {"source": "record", "running": True,
                                           "elapsed": round(time.time() - RECORDER.started, 1),
                                           "file": os.path.basename(RECORDER.path or "") or None,
                                           "meter": RECORDER.meter, "error": RECORDER.error})
                if dev:
                    MONITOR.touch(dev)
                return self.send(200, {"source": "monitor", "running": MONITOR.running(),
                                       "elapsed": 0, "file": None,
                                       "meter": MONITOR.meter, "error": MONITOR.error})

            if u.path == "/api/record/status":
                return self.send(200, RECORDER.status())

            if u.path == "/api/recordings":
                with RECORDER.folder_lock:
                    online = os.path.isdir(RECORDER.folder)
                    return self.send(200, {"takes": RECORDER.takes() if online else [], "folder": RECORDER.folder,
                                           "available": online, "default": RECORDER.folder == RECDIR})

            m = re.match(r"^/api/recordings/(.+\.wav)$", u.path)
            if m:
                fname = os.path.basename(unquote(m.group(1)))
                with RECORDER.folder_lock:
                    if q.get("folder", [RECORDER.folder])[0] != RECORDER.folder:
                        return self.send(409, {"error": "Takes folder changed. Refresh the list."})
                    path = RECORDER.take_path(fname)
                if not os.path.exists(path):
                    return self.send(404, {"error": "no such take"})
                return self.send(200, open(path, "rb").read(), "audio/wav",
                                 {"Content-Disposition": "inline; filename*=UTF-8''" + quote(fname)})

            m = re.match(r"^/api/wav/(\d+)\.wav$", u.path)
            if m:
                slot = int(m.group(1))
                data = STATION.wav(slot)
                return self.send(200, data, "audio/wav",
                                 {"Content-Disposition": f'inline; filename="{slot:03d}.wav"'})

            if u.path == "/api/export.zip":
                slots = [int(x) for x in (q.get("slots", [""])[0]).split(",") if x.strip()]
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for slot in slots:
                        s = STATION.by_slot(slot) or {}
                        nm = re.sub(r"[^A-Za-z0-9 _-]", "", s.get("name", "") or "unnamed")[:40]
                        z.writestr(f"{slot:03d} {nm}.wav".strip(), STATION.wav(slot))
                data = buf.getvalue()
                return self.send(200, data, "application/zip",
                                 {"Content-Disposition": f'attachment; filename="{SLUG}-export.zip"'})

            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.fail(e)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._local_only():
            log(f"refused cross-site POST {u.path} from {self.headers.get('Origin')!r}")
            return self.send(403, {"error": "the station only answers its own page"})
        if u.path == "/api/takes/save":
            # a take trimmed / encoded in the browser: saved under exports/takes/
            try:
                q = parse_qs(u.query)
                n = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(n)
                fname = os.path.basename(q.get("name", ["take.mp3"])[0])
                fname = re.sub(r"[^A-Za-z0-9 _.#()+-]", "", fname)[:100] or "take.mp3"
                if not fname.lower().endswith((".mp3", ".wav")):
                    fname += ".mp3"
                folder = os.path.join(DATA_DIR, "exports", "takes"); os.makedirs(folder, exist_ok=True)
                base, ext = os.path.splitext(fname); k = 2
                while os.path.exists(os.path.join(folder, fname)):
                    fname = f"{base} {k}{ext}"; k += 1
                open(os.path.join(folder, fname), "wb").write(data)
                rel = os.path.relpath(os.path.join(folder, fname), DATA_DIR)
                log(f"take saved: {rel} ({len(data)} bytes)")
                return self.send(200, {"ok": True, "saved": rel, "file": fname, "bytes": len(data)})
            except Exception as e:
                return self.fail(e)

        if u.path == "/api/export/stem":
            try:
                q = parse_qs(u.query)
                export = STEM_EXPORTS[q["id"][0]]
                export.add(q["name"][0], self.rfile, int(self.headers["Content-Length"]))
                return self.send(200, {"ok": True})
            except Exception as e:
                self.close_connection = True
                return self.fail(e)
        try:
            b = self.body_json()

            if u.path == "/api/export/midi":
                import ep133proj
                arr = b["arrangement"]
                project = re.sub(r"[^0-9A-Za-z]", "", str(arr["project"]))[:4] or "x"
                kind = b.get("kind", "midi")
                if kind not in ("midi", "tracks", "groups"):
                    raise ValueError("Unknown export format")
                mode = "song" if b.get("song") else "scenes"
                data = ep133proj.to_midi(arr, b.get("names"), load_params(), float(b["bpm"]), pad_cc=True)
                if b.get("staged") and kind != "midi":
                    token = secrets.token_hex(16)
                    STEM_EXPORTS[token] = project_files.StemExport(export_dir(project), f"{kind} {mode}",
                                                                    f"P{project} {mode}.mid", data)
                    return self.send(200, {"id": token})
                folder = tempfile.mkdtemp(prefix=f"{kind} {mode} ", dir=export_dir(project))
                name = f"P{project} {mode}.mid"
                with open(os.path.join(folder, name), "wb") as out:
                    out.write(data)
                return self.send(200, data, "audio/midi", {"X-Export-Folder": os.path.basename(folder),
                    "X-Saved-To": os.path.relpath(os.path.join(folder, name), DATA_DIR)})

            if u.path == "/api/export/abort":
                export = STEM_EXPORTS.pop(b.get("id"), None)
                if export:
                    with export.lock:
                        export.temp.cleanup()
                return self.send(200, {"ok": True})

            if u.path == "/api/export/finish":
                export = STEM_EXPORTS[b["id"]]
                ref = b.get("reference")
                with RECORDER.folder_lock:
                    path = None
                    if ref:
                        if ref["folder"] != RECORDER.folder:
                            raise ValueError("Takes folder changed; choose the reference again")
                        path = RECORDER.take_path(ref["file"])
                        if RECORDER.running() and RECORDER.path == path:
                            raise ValueError("Stop the reference take before exporting")
                    dest = export.finish(int(b["count"]), path)
                STEM_EXPORTS.pop(b["id"], None)
                return self.send(200, {"saved": os.path.relpath(dest, DATA_DIR)})

            if u.path == "/api/project/collect":
                proj = next(p for p in STATION.projects["list"] if p["node"] == int(b["node"]))
                version = next((v for v in project_dumps(proj["name"]) if v["sha"] == b["sha"]), None)
                if not version:
                    raise ValueError("Read this project before collecting it")
                raw = Path(dump_path(version["file"])).read_bytes()
                settings = {k: b.get(k) for k in ("labels", "repeats", "bpm", "mode", "songOrder")}
                settings["device"] = SLUG
                def read_sound(slot):
                    d = STATION.connect()
                    meta = json.loads(d.metadata(slot))
                    _, _, data = d.get_file(slot)
                    pcm = to_pcm(data)
                    if not pcm or not meta.get("channels") or not meta.get("samplerate"):
                        raise ValueError(f"Missing sound or format in slot {slot}")
                    return to_wav(data, int(meta["channels"]), int(meta["samplerate"])), meta
                with STATION.lock:
                    dest, count = project_files.collect(export_dir(proj["name"]), proj["name"], raw,
                                                        b.get("patch"), settings, read_sound)
                return self.send(200, {"saved": os.path.relpath(dest, DATA_DIR), "samples": count})

            if u.path == "/api/reveal":
                # show an export in Finder (only inside ./exports)
                rel = str(b.get("path", "exports"))
                path = os.path.realpath(os.path.join(DATA_DIR, rel))
                root = os.path.realpath(os.path.join(DATA_DIR, "exports"))
                if not path.startswith(root):
                    return self.send(400, {"error": "only exports can be shown"})
                os.makedirs(path if not os.path.splitext(path)[1] else os.path.dirname(path), exist_ok=True)
                subprocess.run(["open", "-R", path] if os.path.isfile(path) else ["open", path])
                return self.send(200, {"ok": True, "path": path})

            if u.path == "/api/scales":
                # what the device's screen shows at each end of a fader, typed in by you
                clean = {}
                for k, v in (b.get("scales") or {}).items():
                    if not str(k).isdigit() or not 0 <= int(k) <= 11 or not isinstance(v, dict):
                        continue
                    c = {}
                    for f in ("min", "max", "step"):
                        if v.get(f) not in (None, ""):
                            c[f] = float(v[f])
                    for f in ("unit", "curve", "kind"):
                        if v.get(f):
                            c[f] = str(v[f])[:8]
                    for f in ("slope", "peak"):
                        if v.get(f) not in (None, ""):
                            c[f] = float(v[f])
                    if isinstance(v.get("pts"), list):
                        c["pts"] = [[int(a), float(b_)] for a, b_ in v["pts"]][:64]
                    c["checked"] = bool(v.get("checked"))
                    c["measured"] = bool(v.get("measured"))
                    clean[str(int(k))] = c
                json.dump(clean, open(os.path.join(DATA_DIR, f"{SLUG}_scales.json"), "w"), indent=1)
                return self.send(200, clean)

            if u.path == "/api/play":
                STATION.play(int(b["slot"]))
                return self.send(200, {"ok": True})

            if u.path == "/api/record/folder":
                if b.get("default"):
                    RECORDER.set_folder(RECDIR)
                else:
                    RECORDER.choose_folder()
                return self.send(200, {"folder": RECORDER.folder})

            if u.path == "/api/record/keep":
                return self.send(200, MONITOR.keep(str(b["device"])))

            if u.path == "/api/record/start":
                return self.send(200, RECORDER.start(str(b["device"]), str(b.get("name", ""))))

            if u.path == "/api/record/stop":
                return self.send(200, RECORDER.stop())

            if u.path == "/api/recordings/delete":
                if b.get("confirm") != "DELETE":
                    return self.send(400, {"error": "delete requires confirm:'DELETE'"})
                with RECORDER.folder_lock:
                    if b.get("folder", RECORDER.folder) != RECORDER.folder:
                        return self.send(409, {"error": "Takes folder changed. Refresh the list."})
                    RECORDER.delete_take(str(b["file"]))
                return self.send(200, {"ok": True})

            if u.path == "/api/recordings/rename":
                with RECORDER.folder_lock:
                    if b.get("folder", RECORDER.folder) != RECORDER.folder:
                        return self.send(409, {"error": "Takes folder changed. Refresh the list."})
                    name = RECORDER.rename_take(str(b["file"]), str(b["name"]))
                    return self.send(200, {"ok": True, "file": name})

            if u.path == "/api/calibrate":
                if b.get("confirm") != "CALIBRATE":
                    return self.send(400, {"error": "calibrate requires confirm:'CALIBRATE'"})
                if b.get("kind") == "sidechain" and b.get("runs"):
                    STATION.sc_runs = [(float(L), float(S)) for L, S in b["runs"]][:30]
                STATION.calibrate(int(b["node"]), b.get("kind") if b.get("kind") in ("pads", "sidechain", "fx") else "faders")
                return self.send(200, {"ok": True})

            if u.path == "/api/project/send":
                # an edit made in the browser, applied to the exact archive it was made on
                import ep133write, hashlib
                if b.get("confirm") != "SEND":
                    return self.send(400, {"error": "send requires confirm:'SEND'"})
                node, base = int(b["node"]), str(b.get("sha", ""))
                proj = next((p for p in STATION.projects.get("list", []) if p["node"] == node), None)
                if not proj:
                    return self.send(400, {"error": "unknown project"})
                src = next((v for v in project_dumps(proj["name"]) if v["sha"] == base), None)
                if not src:
                    return self.send(409, {"error": "the version these edits were made on is gone - read the project again"})
                raw = open(dump_path(src["file"]), "rb").read()
                new = ep133write.apply_patch(raw, b.get("patch") or {})     # refuses bad edits before anything moves
                STATION.write_project(node, lambda d, cur: new, "edit", base_sha=base, force=bool(b.get("force")))
                return self.send(200, {"ok": True, "changes": ep133write.diff(raw, new)})

            if u.path == "/api/project/duplicate":
                if b.get("confirm") != "DUPLICATE":
                    return self.send(400, {"error": "duplicate requires confirm:'DUPLICATE'"})
                src, dst = int(b["src"]), int(b["dst"])
                if src == dst:
                    return self.send(400, {"error": "pick another project to copy into"})
                target = next((p for p in STATION.projects.get("list", []) if p["node"] == dst), None)
                if target and target.get("used") and not b.get("force"):
                    return self.send(409, {"error": f"project {target['name']} has {target['used']} pads in use - "
                                                    "copying would replace it (send force to do it anyway)"})
                STATION.write_project(dst, lambda d, cur: d.get_file(src)[2], "copy")
                return self.send(200, {"ok": True})

            if u.path == "/api/project/restore":
                if b.get("confirm") != "RESTORE":
                    return self.send(400, {"error": "restore requires confirm:'RESTORE'"})
                node = int(b["node"])
                data = open(dump_path(b["file"]), "rb").read()
                import ep133write
                ep133write.read_members(data)
                STATION.write_project(node, lambda d, cur: data, "restore")
                return self.send(200, {"ok": True})

            if u.path == "/api/projects/active":
                return self.send(200, STATION.set_active(b["project"], b.get("group")))

            if u.path == "/api/projects/read":
                STATION.read_projects()
                return self.send(200, {"ok": True, "scan": STATION.pscan})

            if u.path == "/api/rescan":
                STATION.rescan()
                return self.send(200, {"ok": True, "scan": STATION.scan})

            if u.path == "/api/trim":
                if b.get("confirm") != "TRIM":
                    return self.send(400, {"error": "trim requires confirm:'TRIM'"})
                STATION.trim(int(b["slot"]), b["start"], b["end"], b.get("mono", False),
                             b.get("fade_in", 0), b.get("fade_out", 0))
                return self.send(200, {"ok": True})

            if u.path == "/api/lib/scan":
                return self.send(200, LIB.start_scan())

            if u.path == "/api/lib/features":
                return self.send(200, LIB.start_features(SOURCE_DIR))


            if u.path == "/api/lib/mark":
                return self.send(200, LIB.mark(int(b["id"]), b.get("fav"), b.get("tags"), b.get("sent")))

            if u.path == "/api/lib/roots":
                return self.send(200, {"roots": LIB.set_roots(b.get("roots") or [])})

            if u.path == "/api/lib/reveal":
                path = LIB.path_of(int(b["id"]))
                if not path:
                    return self.send(404, {"error": "no such file"})
                subprocess.run(["open", "-R", path])
                return self.send(200, {"ok": True})

            if u.path == "/api/takes/pad":
                import base64
                if b.get("confirm") != "SEND":
                    return self.send(400, {"error": "Choose and confirm a pad first"})
                return self.send(200, STATION.send_take_to_pad(b["target"], str(b["name"]),
                    base64.b64decode(b["pcm"], validate=True), int(b["channels"]), int(b["samplerate"])))

            if u.path == "/api/upload":
                import base64
                if b.get("confirm") != "UPLOAD":
                    return self.send(400, {"error": "upload requires confirm:'UPLOAD'"})
                STATION.upload(int(b["slot"]), str(b.get("name", "")), base64.b64decode(b["pcm"]),
                               int(b.get("channels", 1)), int(b.get("samplerate", 46875)), b.get("meta"),
                               b.get("replace") is True and b.get("confirm_replace") == "REPLACE")
                return self.send(200, {"ok": True})

            if u.path == "/api/slice":
                if b.get("confirm") != "TRIM":
                    return self.send(400, {"error": "slice requires confirm:'TRIM'"})
                STATION.slice(int(b["slot"]), b.get("regions") or [], b.get("mono", False))
                return self.send(200, {"ok": True})

            if u.path == "/api/rename":
                STATION.rename(int(b["slot"]), str(b["name"]))
                return self.send(200, {"ok": True})

            if u.path == "/api/delete":
                if b.get("confirm") != "DELETE":
                    return self.send(400, {"error": "delete requires confirm:'DELETE'"})
                slots = [int(x) for x in b.get("slots", [])]
                if not slots:
                    return self.send(400, {"error": "no slots given"})
                res = STATION.delete(slots, backup=bool(b.get("backup", True)))
                return self.send(200, res)

            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.fail(e)


def main():
    global STATION
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--device", default="EP-133",
                    help="MIDI port name substring: EP-133 (K.O. II), EP-40 (riddim), EP-1320")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)

    for exe, fw in (("midibridge", ["-framework", "CoreMIDI", "-framework", "CoreFoundation"]),
                    ("usbrec", ["-framework", "AudioUnit", "-framework", "CoreAudio",
                                "-framework", "AudioToolbox", "-framework", "CoreFoundation"])):
        src, binary = SOURCE_DIR / (exe + ".c"), SOURCE_DIR / exe
        if os.access(binary, os.X_OK) and binary.stat().st_mtime >= src.stat().st_mtime:
            continue
        try:
            with tempfile.TemporaryDirectory(dir=SOURCE_DIR) as build:
                target = Path(build) / exe
                subprocess.run(["clang", "-O2", "-o", target, src, *fw], check=True)
                target.replace(binary)
            log(f"built {exe}")
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SystemExit(f"Could not build {exe}. Install Apple's Command Line Tools "
                             f"with xcode-select --install, then start again. ({exc})")
    set_device_paths(args.device)
    STATION = Station(args.device)
    global LIB
    import library
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    LIB = library.Library(LIBRARY_DIR / "library.sqlite", LIBRARY_DIR / "library_roots.json")
    stop_scans = threading.Event()
    threading.Thread(target=LIB.auto_scan, args=(stop_scans,), daemon=True).start()
    log(f"epstation on http://127.0.0.1:{args.port}  (device filter: {args.device!r})")
    log(f"cache -> {CACHE}  inventory -> {os.path.basename(INVENTORY)}")
    log(f"backups -> {BACKUP}")
    log(f"recordings -> {RECORDER.folder}")
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        srv.server_close()
        stop_scans.set()
        RECORDER.stop()
        MONITOR.stop()
        STATION.drop()


if __name__ == "__main__":
    main()
