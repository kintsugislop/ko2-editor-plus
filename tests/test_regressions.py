"""Offline release checks. All station data and audio in this suite are temporary."""
import io
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ep133write
import library
import paths
import project_files


def float_wav(frames):
    data = bytes(frames * 8)
    fmt = struct.pack("<IHHIIHHH", 18, 3, 2, 48000, 384000, 8, 32, 0)
    return (b"RIFF" + struct.pack("<I", 50 + len(data)) + b"WAVEfmt " + fmt
            + b"fact" + struct.pack("<II", 4, frames) + b"data" + struct.pack("<I", len(data)) + data)


class StationChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = tempfile.TemporaryDirectory()
        root = Path(cls.runtime.name)
        with patch.object(paths, "DATA_DIR", root), patch.object(paths, "LIBRARY_DIR", root):
            import epstation
        epstation.set_device_paths("Test Device")
        cls.station = epstation

    @classmethod
    def tearDownClass(cls):
        cls.runtime.cleanup()

    def test_pcm_start_and_silence_survive_decode(self):
        samples = b"{\x00\x00\x00\x07\xff"
        self.assertEqual(self.station.to_pcm(samples), samples)
        self.assertEqual(self.station.to_pcm(self.station.wav_header(1, 48000, 6) + samples), samples)
        silence = b"\x00\x00" * 5
        self.assertEqual(self.station.to_pcm(b'{"name":"}","channels":1}\x00' + silence), silence)

    def test_truncated_project_cannot_be_written_or_used_as_a_template(self):
        members = [{"type": "0", "name": "settings", "data": bytes(700)}]
        archive = ep133write.write_members(members)
        self.assertEqual(ep133write.write_members(ep133write.read_members(archive)), archive)
        for damaged in (archive[:510], archive[:640], archive[:-1024], b"X" + archive[1:]):
            with self.subTest(size=len(damaged)), self.assertRaises(ValueError):
                ep133write.read_members(damaged)

    def test_failed_stem_retry_preserves_complete_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            export = project_files.StemExport(tmp, "tracks", "Project.mid", b"MThd")
            first, longer = float_wav(1), float_wav(3)
            export.add("P01 Kick.wav", io.BytesIO(first), len(first))
            with self.assertRaisesRegex(ValueError, "Incomplete stem"):
                export.add("P01 Kick.wav", io.BytesIO(longer[:-3]), len(longer))
            with self.assertRaises(ValueError):
                export.add("P01 Bass.wav", io.BytesIO(longer[:-1]), len(longer))
            self.assertEqual((export.stage / "P01 Kick.wav").read_bytes(), first)
            self.assertFalse((export.stage / "P01 Bass.wav").exists())
            export.add("P01 Bass.wav", io.BytesIO(longer), len(longer))
            dest = export.finish(2)
            self.assertEqual((dest / "P01 Kick.wav").read_bytes(), longer)
            self.assertEqual(sorted(p.suffix for p in dest.iterdir()), [".mid", ".wav", ".wav"])
            export.temp.cleanup()

    def test_calibration_only_cleans_up_successful_uploads(self):
        device = Mock()
        device.list_nodes.return_value = [(999, 0, 10, "999.pcm")]
        station = self.station.Station()
        station.call = lambda fn: fn(device)
        station.upload = Mock()
        station._wait = Mock(side_effect=[{}, RuntimeError("upload failed")])
        model = Mock(SR=48000)
        model.sounds.return_value = [("first", bytes(64)), ("second", bytes(64))]
        owned = []
        with self.assertRaisesRegex(RuntimeError, "upload failed"):
            station._upload_calibration_sounds(model, owned, Mock())
        self.assertEqual([call.args[0] for call in station.upload.call_args_list], [998, 997])
        self.assertEqual(owned, [998])
        notes = [self.station.pad_midi_note(group, pad) for group in range(4) for pad in range(12)]
        self.assertEqual(sorted(notes), list(range(36, 84)))
        self.assertEqual((notes[0], notes[9], notes[12]), (45, 36, 57))

    def test_library_follows_folder_changes_and_retains_disconnected_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            samples = root / "Samples"
            samples.mkdir()
            catalog = library.Library(root / "index.sqlite", root / "roots.json")
            catalog.set_roots([str(samples)])
            with patch.object(catalog, "splice_db", return_value=None):
                def names():
                    catalog._scan()
                    self.assertIsNone(catalog.scan["error"])
                    with catalog.con() as con:
                        return [row[0] for row in con.execute("SELECT name FROM files ORDER BY name")]
                with wave.open(str(samples / "kick.wav"), "wb") as out:
                    out.setparams((1, 2, 48000, 0, "NONE", ""))
                    out.writeframes(bytes(128))
                self.assertEqual(names(), ["kick"])
                (samples / "kick.wav").rename(samples / "rim.wav")
                self.assertEqual(names(), ["rim"])
                samples.rename(root / "disconnected")
                self.assertEqual(names(), ["rim"])
                catalog.set_roots([])
                self.assertEqual(names(), [])


if __name__ == "__main__":
    unittest.main()
