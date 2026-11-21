#!/usr/bin/env python3
"""Talk to a Teenage Engineering EP-133 / EP-40 over MIDI SysEx via ./midibridge."""
import subprocess, itertools, json, re, time

from paths import SOURCE_DIR

TE_ID = (0x00, 0x20, 0x76)
SYSEX_TE = 0x40
BIT_IS_REQUEST = 0x40
BIT_REQ_ID = 0x20
CMD_GREET = 1
CMD_FILE = 5
STATUS_SPECIFIC_SUCCESS_START = 64


def ok(status):
    """TE reserves >=64 for command-specific *success* codes, not failures."""
    return status == 0 or status >= STATUS_SPECIFIC_SUCCESS_START
FILE_INIT, FILE_PUT, FILE_GET, FILE_LIST = 1, 2, 3, 4
FILE_PLAYBACK, FILE_DELETE, FILE_METADATA, FILE_INFO = 5, 6, 7, 11
METADATA_SET, METADATA_GET = 1, 2
PLAYBACK_START, PLAYBACK_STOP = 1, 2
MIDI_START, MIDI_CONTINUE, MIDI_STOP = 0xFA, 0xFB, 0xFC
GET_TYPE_INIT, GET_TYPE_DATA = 0, 1
TYPE_FILE, TYPE_DIR = 1, 2


def pack7(data):
    out = bytearray()
    for i in range(0, len(data), 7):
        chunk = data[i:i+7]
        mask = 0
        for k, c in enumerate(chunk):
            if c & 0x80:
                mask |= 1 << k
        out.append(mask)
        out.extend(c & 0x7F for c in chunk)
    return bytes(out)


def unpack7(body):
    out = bytearray()
    for i in range(0, len(body), 8):
        grp = body[i:i+8]
        if not grp:
            break
        mask, data = grp[0], grp[1:]
        for k, c in enumerate(data):
            out.append(c | (0x80 if (mask >> k) & 1 else 0))
    return bytes(out)


class EP:
    def __init__(self, dev="EP-133", bridge=SOURCE_DIR / "midibridge"):
        self.p = subprocess.Popen([bridge, dev], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first = self.p.stderr.readline().strip()
        if first != "READY":
            try:
                self.p.kill(); self.p.wait(timeout=2)
            except Exception:
                pass
            raise RuntimeError(first or "bridge failed to start")
        self.device_id = 0x7F
        self.seq = itertools.cycle(range(1, 4096))
        self.events = []
        self._pending = []

    def _readline(self, timeout=15.0):
        """A line from the bridge, or give up: a wedged bridge must not hang the station."""
        import select
        r, _, _ = select.select([self.p.stdout], [], [], timeout)
        if not r:
            try:
                self.p.kill()
            except Exception:
                pass
            raise TimeoutError("MIDI bridge stopped answering")
        return self.p.stdout.readline()

    def raw(self, hexstr):
        self.p.stdin.write(hexstr + "\n")
        self.p.stdin.flush()
        return bytes.fromhex(self._readline().strip())

    def send_short(self, data):
        """Fire a short MIDI message (transport, notes). Nothing replies to these."""
        self.p.stdin.write("s" + bytes(data).hex() + "\n")
        self.p.stdin.flush()
        self._readline()

    def _rid_of(self, msg):
        """Request id echoed in a reply header: 5 high bits, then 7 low bits."""
        return ((msg[6] & 0x1F) << 7) | (msg[7] & 0x7F)

    def _is_reply(self, reply, rid, cmd):
        """ours: a TE SysEx with our request id and command, and only data bytes inside"""
        return (len(reply) >= 10 and reply[0] == 0xF0 and reply[-1] == 0xF7 and tuple(reply[1:4]) == TE_ID
                and self._rid_of(reply) == rid and reply[8] == cmd and all(b < 0x80 for b in reply[1:-1]))

    def request(self, cmd, payload=b"", timeout=8.0):
        rid = next(self.seq)
        msg = bytes([0xF0, *TE_ID, self.device_id, SYSEX_TE,
                     BIT_IS_REQUEST | BIT_REQ_ID | ((rid >> 7) & 31), rid & 127, cmd]) \
            + pack7(payload) + bytes([0xF7])
        deadline = time.time() + timeout
        reply = self.raw(msg.hex())
        # The device also pushes unsolicited messages, and a reply that came too late for an
        # earlier request can still be waiting: skip anything that isn't ours and keep listening
        # until the deadline (a stale message must not shorten the wait for the real answer).
        while True:
            if reply and self._is_reply(reply, rid, cmd):
                # F0 00 20 76 <id> 40 <flags> <rid> <cmd> <status> <packed...> F7
                self.events = (self.events + self._pending)[-64:]
                self._pending = []
                return reply[9], unpack7(reply[10:-1])
            if reply:
                self._pending = (self._pending + [reply])[-32:]
            if time.time() >= deadline:
                raise TimeoutError(f"no reply to cmd={cmd} payload={payload.hex()[:40]}")
            reply = self.raw("r")

    def identify(self):
        """the MIDI identity reply (F0 7E <id> 06 02 ...): only that one sets the device id"""
        r = self.raw("f07e7f0601f7")
        deadline = time.time() + 6.0
        while True:
            if len(r) >= 5 and r[0] == 0xF0 and r[1] == 0x7E and r[3] == 0x06 and r[4] == 0x02:
                self.device_id = r[2]
                return r
            if r:
                self._pending = (self._pending + [r])[-32:]
            if time.time() >= deadline:
                raise TimeoutError("no identity reply from the device")
            r = self.raw("r")

    def greet(self):
        st, data = self.request(CMD_GREET)
        return dict(kv.split(":", 1) for kv in data.decode(errors="replace").split(";") if ":" in kv)

    def file_init(self, subscribe=False):
        """flags bit0 subscribes to push events; off by default so replies stay 1:1.

        The reply carries the device's chunk size -- the largest message it takes,
        which sets how many bytes fit in one upload page."""
        st, data = self.request(CMD_FILE, bytes([FILE_INIT, 1 if subscribe else 0,
                                                 0x00, 0x40, 0x00, 0x00]))
        if len(data) >= 5:
            self.chunk = int.from_bytes(data[1:5], "big")
        return st, data

    def list_nodes(self, node_id):
        """Yield (id, flags, size, name) for children of node_id."""
        page = 0
        while True:
            st, data = self.request(CMD_FILE, bytes([FILE_LIST, page >> 8, page & 0xFF,
                                                     node_id >> 8, node_id & 0xFF]))
            if not ok(st) or len(data) <= 2:
                return
            got = (data[0] << 8) | data[1]
            if got != page:
                raise RuntimeError(f"page mismatch {got} != {page}")
            body = data[2:]
            off = 0
            while off < len(body):
                e = body[off:]
                if len(e) < 8:
                    break
                nid = (e[0] << 8) | e[1]
                flags = e[2]
                size = (e[3] << 24) | (e[4] << 16) | (e[5] << 8) | e[6]
                end = e.index(0, 7) if 0 in e[7:] else len(e)
                name = e[7:end].decode(errors="replace")
                yield nid, flags, size, name
                off += 7 + len(name) + 1
            page += 1

    def metadata(self, node_id):
        """Concatenate paged metadata JSON for a node."""
        out, page = "", 0
        while True:
            st, data = self.request(CMD_FILE, bytes([FILE_METADATA, METADATA_GET,
                                                     node_id >> 8, node_id & 0xFF,
                                                     page >> 8, page & 0xFF]))
            if not ok(st) or len(data) < 2:
                break
            chunk = data[2:].split(b"\0")[0].decode(errors="replace")
            out += chunk
            if not chunk:
                break
            page += 1
        return out

    # ---- playback (audition on the device's own output) ----

    def play(self, node_id, offset=0, length=0):
        """Start playback of a sound through the device's own outputs."""
        return self.request(CMD_FILE, bytes([FILE_PLAYBACK, PLAYBACK_START,
                                             node_id >> 8, node_id & 0xFF]) +
                            offset.to_bytes(4, "big") + length.to_bytes(4, "big"))

    def stop(self, node_id, offset=0):
        return self.request(CMD_FILE, bytes([FILE_PLAYBACK, PLAYBACK_STOP,
                                             node_id >> 8, node_id & 0xFF]) +
                            offset.to_bytes(4, "big") + (0).to_bytes(4, "big"))

    # ---- transport (the device's own play / stop, over MIDI real time) ----

    def transport(self, action):
        """Start, continue or stop the device's own sequencer.

        These are the standard MIDI real-time bytes rather than TE SysEx -- the
        device acts on them only while its sync setting accepts external MIDI
        transport, and says nothing back either way.
        """
        code = {"start": MIDI_START, "continue": MIDI_CONTINUE, "stop": MIDI_STOP}[action]
        self.send_short([code])

    # ---- file transfer ----

    def get_file(self, node_id, progress=None):
        """Download a sound's raw bytes. Returns (name, declared_size, data)."""
        st, data = self.request(CMD_FILE, bytes([FILE_GET, GET_TYPE_INIT,
                                                 node_id >> 8, node_id & 0xFF]) +
                                (0).to_bytes(4, "big"))
        if not ok(st):
            raise IOError(f"get init failed for node {node_id}, status {st}")
        size = int.from_bytes(data[3:7], "big")
        end = data.index(0, 7) if 0 in data[7:] else len(data)
        name = data[7:end].decode(errors="replace")

        buf, page = bytearray(), 0
        while len(buf) < size:
            st, d = self.request(CMD_FILE, bytes([FILE_GET, GET_TYPE_DATA,
                                                  page >> 8, page & 0xFF]))
            if not ok(st):
                raise IOError(f"get page {page} failed, status {st}")
            got = (d[0] << 8) | d[1]
            if got != page:
                raise IOError(f"page mismatch {got} != {page}")
            chunk = d[2:]
            if not chunk:
                if len(buf) < size:          # a cut-short read must not pass for the whole file
                    raise IOError(f"short read of node {node_id}: {len(buf)} of {size} bytes")
                break
            buf += chunk
            page = (page + 1) & 0xFFFF
            if progress:
                progress(len(buf), size)
        return name, size, bytes(buf[:size])

    # ---- mutations ----

    def _page_payload(self):
        """Raw bytes per upload page -- the Sample Tool's calculateMaxPayloadLength."""
        # the device may report a large chunk; midibridge reads one hex line of at
        # most 256 KiB, and small pages are what the device acks reliably anyway
        chunk = min(getattr(self, "chunk", 0) or 512, 2048)
        a = chunk - 6
        o = 8 + 2 + 1
        if a <= o:
            return 64
        n = a - 1 - o
        return n - n // 8

    @staticmethod
    def safe_filename(name, fallback="sample"):
        """What the Sample Tool sends as a file name: no extension, no dots, plain
        lowercase ASCII, 16 chars. The device reads anything after a dot as a
        file type and refuses the upload, so a name like 'os_fire.ides_c#m' fails."""
        n = (name or "").lower()
        n = re.sub(r"[^a-z0-9 _-]", "", n.replace(".", "_")).strip()[:16].strip()
        return n or fallback

    def put_project(self, parent_id, node_id, name, data, progress=None):
        """Replace a whole project with an archive, as the TE Sample Tool's
        uploadProjectArchive does: PUT onto the project's own node under /projects,
        flagged READ | DIR, the bare project number as the name, no metadata."""
        return self.put_file(parent_id, node_id, name, data, None, progress, flags=4 | TYPE_DIR, raw_name=True)

    def put_file(self, parent_id, file_id, filename, data, meta=None, progress=None, flags=None, raw_name=False):
        """Upload a file under parent_id at file_id (the slot). Returns the new node id.

        Mirrors the TE Sample Tool: an init message (flags, ids, size, name, and a
        small JSON header), then numbered data pages, then one empty page to close."""
        flags = flags if flags is not None else 4 | TYPE_FILE   # readable file
        name = (str(filename)[:54] if raw_name else self.safe_filename(filename)).encode()
        init = bytes([FILE_PUT, 0, flags]) + file_id.to_bytes(2, "big") \
            + parent_id.to_bytes(2, "big") + len(data).to_bytes(4, "big") + name + b"\0"
        if meta:
            init += json.dumps(meta, separators=(",", ":")).encode()
        st, reply = self.request(CMD_FILE, init)
        if not ok(st):
            why = reply.split(b"\0")[0].decode(errors="replace").strip()
            raise IOError(f"put init refused (status {st}{': ' + why if why else ''})")
        new_id = (reply[0] << 8) | reply[1] if len(reply) >= 2 else file_id
        per = self._page_payload()
        page, off = 0, 0
        while off < len(data):
            part = data[off:off + per]
            st, _ = self.request(CMD_FILE, bytes([FILE_PUT, 1]) + page.to_bytes(2, "big") + part)
            if not ok(st):
                raise IOError(f"put page {page} refused (status {st})")
            off += len(part)
            page = (page + 1) & 0xFFFF
            if progress:
                progress(off, len(data))
        st, _ = self.request(CMD_FILE, bytes([FILE_PUT, 1]) + page.to_bytes(2, "big"))
        if not ok(st):
            raise IOError(f"put close refused (status {st})")
        return new_id

    def set_metadata(self, node_id, obj):
        """Merge a metadata dict into a node (e.g. {'name': 'new name'})."""
        blob = json.dumps(obj, separators=(",", ":")).encode()
        chunk = min(getattr(self, "chunk", 0), 2048)
        if chunk and len(blob) > chunk - 8:
            return self._set_metadata_paged(node_id, blob)
        return self.request(CMD_FILE, bytes([FILE_METADATA, METADATA_SET,
                                             node_id >> 8, node_id & 0xFF]) + blob + b"\0")

    def _set_metadata_paged(self, node_id, blob):
        """Metadata too long for one message goes as init + pages + empty page."""
        st, _ = self.request(CMD_FILE, bytes([FILE_METADATA, 4, 0]) + node_id.to_bytes(2, "big")
                             + len(blob).to_bytes(4, "big"))
        if not ok(st):
            raise IOError(f"paged metadata init refused (status {st})")
        per, page, off = self._page_payload(), 0, 0
        while off < len(blob):
            part = blob[off:off + per]
            st, _ = self.request(CMD_FILE, bytes([FILE_METADATA, 4, 1]) + page.to_bytes(2, "big") + part)
            if not ok(st):
                raise IOError(f"paged metadata page {page} refused (status {st})")
            off += len(part); page += 1
        return self.request(CMD_FILE, bytes([FILE_METADATA, 4, 1]) + page.to_bytes(2, "big"))

    def rename(self, node_id, name):
        name = "".join(c for c in name if 32 <= ord(c) < 127)[:54].strip()
        if not name:
            raise ValueError("name cannot be empty")
        return self.set_metadata(node_id, {"name": name})

    def delete(self, node_id):
        """ERASES the sound in this slot. Irreversible."""
        return self.request(CMD_FILE, bytes([FILE_DELETE, node_id >> 8, node_id & 0xFF]))

    def close(self):
        try:
            self.p.stdin.close(); self.p.wait(timeout=2)
        except Exception:
            self.p.kill()
