"""真实UDP socket验证：双路循环、丢片重传、损坏拒绝和视频兼容。

全部绑定回环地址，不修改系统路由。真实公网检查单独运行并保存结果。
"""
import json
import csv
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import file_protocol as files
import protocol as wire
from cloud_relay import Relay
from file_transfer import FileSender, DownloadTransport, FileDownloader, file_hash, create_demo_files

ROOT = Path(__file__).resolve().parent


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def config(name):
    value = wire.load_config(ROOT / name)
    value["token"] = "file-test-only-token-at-least-16"
    return value


class FileTests(unittest.TestCase):
    def setup_chain(self, rounds=1, corrupt=False, empty=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = root / "source"
        source.mkdir()
        # 两轮跨越20块统计窗口和PID暖机；文件尾块不是完整32KiB。
        entries = (("empty.txt", 0),) if empty else (("small.txt", 170003), ("medium.txt", 360007), ("large.txt", 470011))
        for name, size in entries:
            (source / name).write_bytes(bytes(range(256)) * (size // 256) + bytes(range(size % 256)))
        cloud = config("cloud_config.json")
        cloud.update(bind_ip="127.0.0.1", data_port=free_port(), control_port=free_port())
        relay = Relay(cloud)
        failures = []
        def run():
            try:
                relay.run()
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (relay.stop.set(), thread.join(2)))
        until = time.monotonic() + 2
        while not hasattr(relay, "control") and not failures and time.monotonic() < until:
            time.sleep(0.01)
        self.assertFalse(failures)
        sender_config = config("send/config.json")
        sender_config.update(lan_bind_ip="127.0.0.1", lan_port=free_port(), cloud_bind_ip="127.0.0.1",
                             cloud_host="127.0.0.1", cloud_data_port=cloud["data_port"],
                             cloud_control_port=cloud["control_port"], announce_interval=0.05,
                             file_burst_interval=0.0001)
        sender = FileSender(sender_config, source)
        self.addCleanup(sender.close)
        if corrupt:
            sender.manifest[0]["sha256"] = "0" * 64
        sender.start()
        receiver_config = config("receive/config.json")
        receiver_config.update(lan_bind_ip="127.0.0.1", cpe_bind_ip="127.0.0.1", sender_host="auto",
                               sender_lan_port=sender_config["lan_port"], lan_receive_port=free_port(),
                               cpe_receive_port=free_port(), cloud_host="127.0.0.1",
                               cloud_control_port=cloud["control_port"], auto_routes=False,
                               keepalive_interval=0.05, file_mode="fixed", file_block_kib=4,
                               file_rounds=rounds, file_duration=0, file_request_timeout=0.15,
                               file_max_retries=3, file_registration_timeout=3)
        transport = DownloadTransport(receiver_config)
        self.addCleanup(transport.close)
        transport.start()
        downloader = FileDownloader(receiver_config, transport, root / "results")
        return source, relay, sender, transport, downloader

    def test_two_rounds_auto_discovery_and_exact_files(self):
        source, relay, sender, transport, downloader = self.setup_chain(rounds=2)
        result = downloader.run()
        self.assertEqual(result["reason"], "rounds")
        self.assertEqual(result["completed_files"], 6)
        self.assertEqual(transport.targets[0], sender.lan.getsockname())
        self.assertGreater(relay.stats["file_requests"], 0)
        self.assertTrue(all(count > 0 for count in result["assigned_blocks"]))
        for original in source.iterdir():
            target = downloader.output / "downloads" / original.name
            self.assertEqual(target.read_bytes(), original.read_bytes())
        self.assertGreater(downloader.controller.precount, 10)
        with (downloader.output / "throughput.csv").open(encoding="utf-8-sig", newline="") as stream:
            last = list(csv.DictReader(stream))[-1]
        self.assertEqual([int(last["lan_bytes"]), int(last["cpe_bytes"])], result["received_bytes"])

    def test_drop_reorder_duplicate_recovered_same_lane(self):
        source, relay, sender, transport, downloader = self.setup_chain()
        forward = relay.forward
        dropped = False
        held = None
        def lossy(packet):
            nonlocal dropped, held
            fragment = files.unpack_fragment(packet, relay.config["token"])
            if fragment and not dropped:
                dropped = True
                return
            if fragment and held is None:
                held = packet
                return
            forward(packet)
            if held:
                forward(held)
                forward(held)  # 故意重复和乱序。
                held = None
        with patch.object(relay, "forward", side_effect=lossy):
            result = downloader.run()
        self.assertEqual(result["completed_files"], 3)
        self.assertGreaterEqual(result["retries"][1], 1)
        for original in source.iterdir():
            self.assertEqual(file_hash(downloader.output / "downloads" / original.name), file_hash(original))

    def test_invalid_hash_never_published_as_complete(self):
        _, _, _, _, downloader = self.setup_chain(corrupt=True)
        with self.assertRaisesRegex(ValueError, "完整性校验"):
            downloader.run()
        self.assertFalse((downloader.output / "downloads" / "small.txt").exists())
        result = json.loads((downloader.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(result["completed_files"], 0)
        self.assertEqual(result["reason"], "error")
        with (downloader.output / "files.csv").open(encoding="utf-8-sig", newline="") as stream:
            record = list(csv.DictReader(stream))[-1]
        self.assertEqual(record["status"], "failed")

    def test_missing_lane_fails_without_switch_or_skip(self):
        _, relay, _, _, downloader = self.setup_chain()
        with patch.object(relay, "forward", return_value=None):
            with self.assertRaises(TimeoutError):
                downloader.run()
        result = json.loads((downloader.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(result["completed_files"], 0)
        self.assertEqual(result["reason"], "error")

    def test_fuzzy_core_updates_after_warmup(self):
        _, _, _, _, downloader = self.setup_chain(rounds=2)
        downloader.config["file_mode"] = "fuzzy"
        result = downloader.run()
        self.assertEqual(result["completed_files"], 6)
        self.assertGreater(result["pid_updates"], 0)

    def test_duration_stop_saves_partial_and_summary(self):
        _, _, _, _, downloader = self.setup_chain(rounds=0)
        downloader.config["file_duration"] = 0.05
        result = downloader.run()
        self.assertEqual(result["reason"], "duration")
        self.assertTrue((downloader.output / "summary.json").exists())
        with (downloader.output / "files.csv").open(encoding="utf-8-sig", newline="") as stream:
            record = list(csv.DictReader(stream))[-1]
        self.assertEqual(record["status"], "stopped")

    def test_empty_file_loop_honors_duration(self):
        _, _, _, _, downloader = self.setup_chain(rounds=0, empty=True)
        downloader.config["file_duration"] = 0.05
        result = downloader.run()
        self.assertEqual(result["reason"], "duration")
        self.assertGreater(result["completed_files"], 0)
        self.assertEqual((downloader.output / "downloads" / "empty.txt").read_bytes(), b"")

    def test_file_changed_rejected(self):
        source, _, _, transport, _ = self.setup_chain()
        transport.wait_ready(3)
        (source / "small.txt").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "文件已改变"):
            transport.request(0, "file_block", file_index=0, offset=0, size=100)

    def test_protocol_corruption_and_oversize_rejected(self):
        identity = "abcd" * 8
        packet = files.pack_fragment(identity, 1, 0, 1, b"data", "test-token")
        self.assertEqual(files.unpack_fragment(packet, "test-token"), (identity, 1, 0, 1, b"data"))
        self.assertIsNone(files.unpack_fragment(packet[:-1] + b"!", "test-token"))
        self.assertIsNone(files.unpack_fragment(packet, "wrong-token"))
        large = files.pack_fragment(identity, 1, 0, 1, b"x" * 1201, "test-token")
        self.assertIsNone(files.unpack_fragment(large, "test-token"))

    def test_default_file_generation_preserves_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            create_demo_files(directory)
            values = sorted(p.stat().st_size for p in Path(directory).iterdir())
            self.assertEqual(values, [1000000, 3000000, 5000000])
            path = Path(directory) / "small.txt"
            path.write_bytes(b"user-content")
            create_demo_files(directory)
            self.assertEqual(path.read_bytes(), b"user-content")


if __name__ == "__main__":
    unittest.main(verbosity=2)
