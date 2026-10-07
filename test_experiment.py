"""标准库测试：在本机真实 UDP socket 上模拟两条链路，不改变系统路由。"""
import importlib.util
import json
import shutil
import socket
import struct
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

import protocol as wire
from cloud_relay import Relay
from initialize_config import initialize_configs
import network_setup

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "send"))
import fuzzy_pid


def import_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


send = import_file("experiment_sender", ROOT / "send/main.py")
receive = import_file("experiment_receiver", ROOT / "receive/main.py")


def test_config(relative):
    """只读公开示例，测试不依赖也不使用真实部署令牌。"""
    path = ROOT / relative
    cfg = json.loads(path.with_name(path.stem + ".example.json").read_text(encoding="utf-8"))
    cfg["token"] = "unit-test-only-token-not-for-deployment"
    return cfg


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待条件超时")


class ProtocolTests(unittest.TestCase):
    def test_configuration_initialization_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("send/config.json", "receive/config.json", "cloud_config.json"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                example = path.with_name(path.stem + ".example.json")
                shutil.copyfile(ROOT / example.relative_to(root), example)
            created = initialize_configs(root, "203.0.113.10")
            self.assertEqual(len(created), 3)
            configs = [json.loads((root / name).read_text(encoding="utf-8")) for name in created]
            self.assertEqual(len({cfg["token"] for cfg in configs}), 1)
            self.assertGreaterEqual(len(configs[0]["token"]), 16)
            self.assertEqual(configs[0]["token"], test_config_defaults()["token"])
            before = {name: (root / name).read_bytes() for name in created}
            self.assertEqual(initialize_configs(root, "203.0.113.20"), [])
            self.assertEqual(before, {name: (root / name).read_bytes() for name in created})

    def test_initialization_needs_no_manual_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("send/config", "receive/config", "cloud_config"):
                path = root / (name + ".example.json")
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / (name + ".example.json"), path)
            self.assertEqual(len(initialize_configs(root)), 3)
            cfg = wire.load_config(root / "receive/config.json")
            self.assertEqual(cfg["cloud_host"], "47.97.248.31")
            self.assertEqual(cfg["sender_host"], "auto")

    def test_original_header_and_feedback(self):
        packet = wire.pack_video(1, 258, b"abc")
        self.assertEqual(packet, b"\x01\x00\x00\x01\x02abc")
        self.assertEqual(wire.unpack_video(packet), (1, 258, b"abc"))
        self.assertEqual(wire.unpack_feedback(wire.pack_feedback(3, 8)), (3, 8))
        self.assertEqual(wire.unpack_feedback(wire.pack_feedback(99999, 0)), (65535, 0))
        self.assertIsNone(wire.unpack_video(b"\x01"))

    def test_authentication(self):
        token = "test-secret-at-least-16-characters"
        packet = wire.make_control("register", token, "test")
        self.assertEqual(wire.parse_control(packet, token, "test")["kind"], "register")
        self.assertIsNone(wire.parse_control(packet, "wrong-token", "test"))
        self.assertIsNone(wire.parse_control(packet, token, "different-experiment"))
        self.assertIsNone(wire.parse_control(packet[:-1] + b"!", token, "test"))

    def test_pid_golden_trace_from_original(self):
        # 参考结果由原0623fuzzypid.py计算，覆盖正负误差及历史项变化。
        pairs = [(0, 0), (10, 0), (100, 0), (50, 0), (400, 0),
                 (0, 0), (20, 10), (0, 100), (50, 200), (800, 100)]
        pid = fuzzy_pid.strategy()
        self.assertEqual([pid.Fuzzy_PID_Increase(a, b) for a, b in pairs],
                         [0, 0, 0, 0, 4, -6, 2, -2, -1, 8])
        second = fuzzy_pid.strategy()
        self.assertEqual(second.erro_pre, 0)
        self.assertEqual(second.kp, 0.5)

    def test_split_rule(self):
        self.assertEqual([sum(send.choose_media(n, g) for n in range(10))
                          for g in (0, 5, 9.5, 10)], [0, 5, 10, 10])


class ReorderTests(unittest.TestCase):
    def test_out_of_order_backlog_and_duplicate(self):
        buf = receive.ReorderBuffer()
        stop = threading.Event()
        buf.put(1, 1, b"one")
        buf.put(0, 0, b"zero")
        self.assertFalse(buf.put(1, 1, b"duplicate"))
        self.assertEqual(buf.backlog(), (1, 1))
        self.assertEqual(buf.get(stop), b"zero")
        self.assertEqual(buf.backlog(), (0, 1))
        self.assertEqual(buf.get(stop), b"one")
        self.assertFalse(buf.put(0, 0, b"late"))
        self.assertEqual((buf.duplicate, buf.late), (1, 1))

    def test_timeout_skips_missing_packet(self):
        buf = receive.ReorderBuffer(timeout=0.03)
        stop = threading.Event()
        buf.put(1, 1, b"one")
        start = time.monotonic()
        self.assertEqual(buf.get(stop), b"one")
        self.assertGreaterEqual(time.monotonic() - start, 0.02)
        self.assertEqual(buf.skipped, 1)
        self.assertFalse(buf.put(0, 0, b"late"))

    def test_wrap_and_bounded_queue(self):
        buf = receive.ReorderBuffer(max_packets=2)
        buf.next_expected = 0xFFFFFFFF
        buf.put(1, 0, b"after-wrap")
        buf.put(0, 0xFFFFFFFF, b"before-wrap")
        self.assertFalse(buf.put(0, 1, b"overflow"))
        stop = threading.Event()
        self.assertEqual(buf.get(stop), b"before-wrap")
        self.assertEqual(buf.get(stop), b"after-wrap")
        self.assertEqual(buf.overflow, 1)


def test_config_defaults():
    return json.loads((ROOT / "send/config.example.json").read_text(encoding="utf-8"))


class InterfaceTests(unittest.TestCase):
    def test_usb_and_direct_wifi_route_plans(self):
        cpe = dict(ip="192.168.2.180", prefix=24, index=8, name="CPE", gateway="192.168.2.230", wifi=False)
        usb = dict(ip="10.210.158.92", prefix=24, index=13, name="USB", gateway="10.210.158.59", wifi=False)
        wifi = dict(ip="192.168.1.20", prefix=24, index=14, name="Wi-Fi", gateway="192.168.1.1", wifi=True)
        virtual = dict(ip="172.16.1.1", prefix=24, index=15, name="VMware", gateway="172.16.1.2", wifi=False)
        for lan, next_hop in ((usb, usb["gateway"]), (wifi, "0.0.0.0")):
            cfg = test_config("receive/config.json")
            with patch.object(network_setup.sys, "platform", "win32"), \
                    patch.object(network_setup, "windows_interfaces", return_value=[virtual, cpe, lan]), \
                    patch.object(network_setup, "ensure_host_route") as route:
                network_setup.prepare_receiver(cfg)
                network_setup.ensure_sender_route(cfg, "192.168.1.10")
                self.assertEqual((cfg["cpe_bind_ip"], cfg["lan_bind_ip"]), (cpe["ip"], lan["ip"]))
                self.assertEqual(route.call_args_list[0].args, ("47.97.248.31", cpe["gateway"], 8))
                self.assertEqual(route.call_args_list[1].args, ("192.168.1.10", next_hop, lan["index"]))

    def test_explicit_sender_ignores_discovery(self):
        cfg = test_config("receive/config.json")
        cfg.update(sender_host="127.0.0.1", sender_lan_port=1234, auto_routes=False)
        combiner = receive.Combiner(cfg)
        combiner.discover_sender(["192.168.1.10", 30002])
        self.assertFalse(combiner.auto_sender)
        self.assertEqual(combiner.lan_target, ("127.0.0.1", 1234))

    def test_sender_auto_prefers_wifi(self):
        rows = [dict(ip="10.1.1.1", index=1, name="LAN", gateway="10.1.1.254", wifi=False),
                dict(ip="192.168.1.10", index=2, name="Wi-Fi", gateway="192.168.1.1", wifi=True)]
        cfg = test_config("send/config.json")
        with patch.object(network_setup.sys, "platform", "win32"), \
                patch.object(network_setup, "windows_interfaces", return_value=rows):
            network_setup.prepare_sender(cfg)
        self.assertEqual(cfg["lan_bind_ip"], "192.168.1.10")


class NetworkTests(unittest.TestCase):
    def start_relay(self, config):
        relay = Relay(config)
        failures = []

        def run():
            try:
                relay.run()
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (relay.stop.set(), thread.join(2)))
        wait_for(lambda: hasattr(relay, "control") or failures)
        if failures:
            raise failures[0]
        return relay

    def test_cloud_registration_rebinding_and_expiry(self):
        cfg = test_config("cloud_config.json")
        cfg.update(bind_ip="127.0.0.1", data_port=free_port(), control_port=free_port(), peer_timeout=0.2)
        relay = self.start_relay(cfg)
        receiver1 = wire.udp_socket("127.0.0.1", 0)
        receiver2 = wire.udp_socket("127.0.0.1", 0)
        transmitter = wire.udp_socket("127.0.0.1", 0)
        for sock in (receiver1, receiver2, transmitter):
            self.addCleanup(sock.close)
            sock.settimeout(1)
        register = wire.make_control("register", cfg["token"], cfg["experiment_id"])
        control_target = ("127.0.0.1", cfg["control_port"])
        data_target = ("127.0.0.1", cfg["data_port"])
        for receiver in (receiver1, receiver2):
            receiver.sendto(register, control_target)
            ack, source = receiver.recvfrom(4096)
            self.assertEqual(source, control_target)
            self.assertIsNotNone(wire.parse_control(ack, cfg["token"], cfg["experiment_id"]))
            original = wire.pack_video(1, 7, b"unchanged-video")
            transmitter.sendto(wire.signed_message(wire.DATA_MAGIC, original, cfg["token"]), data_target)
            received, source = receiver.recvfrom(4096)
            self.assertEqual((received, source), (original, control_target))
        wait_for(lambda: relay.peer is None)
        transmitter.sendto(wire.signed_message(wire.DATA_MAGIC, original, cfg["token"]), data_target)
        wait_for(lambda: relay.stats["offline"] == 1)
        self.assertEqual(relay.stats["forwarded"], 2)

    def test_sender_metadata_validation_and_expiry(self):
        cfg = test_config("cloud_config.json")
        cfg.update(bind_ip="127.0.0.1", data_port=free_port(), control_port=free_port(), peer_timeout=0.2)
        relay = self.start_relay(cfg)
        sock = wire.udp_socket("127.0.0.1", 0)
        self.addCleanup(sock.close)
        sock.settimeout(1)
        target = ("127.0.0.1", cfg["control_port"])
        def control(kind, **fields):
            sock.sendto(wire.make_control(kind, cfg["token"], cfg["experiment_id"], **fields), target)
        control("announce_sender", lan_ip="invalid", lan_port=30002)
        wait_for(lambda: relay.stats["invalid"] == 1)
        self.assertIsNone(relay.sender)
        control("announce_sender", lan_ip="192.168.1.10", lan_port=30002)
        reply, _ = sock.recvfrom(4096)
        self.assertEqual(wire.parse_control(reply, cfg["token"], cfg["experiment_id"])["kind"], "sender_announced")
        control("register")
        reply, _ = sock.recvfrom(4096)
        self.assertEqual(wire.parse_control(reply, cfg["token"], cfg["experiment_id"])["sender_lan"], ["192.168.1.10", 30002])
        relay.sender_seen = time.monotonic() - 1
        control("register")
        reply, _ = sock.recvfrom(4096)
        self.assertIsNone(wire.parse_control(reply, cfg["token"], cfg["experiment_id"])["sender_lan"])

    def test_full_two_path_split_relay_combine(self):
        self.run_two_path("auto")

    def test_full_two_path_with_explicit_sender(self):
        self.run_two_path("127.0.0.1")

    def run_two_path(self, sender_host):
        # 配置全部改成回环地址，只验证程序，不触碰真实网卡或公网。
        cloud_cfg = test_config("cloud_config.json")
        cloud_cfg.update(bind_ip="127.0.0.1", data_port=free_port(), control_port=free_port())
        relay = self.start_relay(cloud_cfg)
        output = wire.udp_socket("127.0.0.1", 0)
        self.addCleanup(output.close)
        source = wire.udp_socket("127.0.0.1", 0)
        self.addCleanup(source.close)
        sc = test_config("send/config.json")
        sc.update(source_port=free_port(), lan_bind_ip="127.0.0.1", lan_port=free_port(),
                  cloud_bind_ip="127.0.0.1", cloud_host="127.0.0.1", cloud_data_port=cloud_cfg["data_port"],
                  cloud_control_port=cloud_cfg["control_port"], announce_interval=0.05, fixed_g=5, initial_g=5)
        rc = test_config("receive/config.json")
        rc.update(lan_bind_ip="127.0.0.1", cpe_bind_ip="127.0.0.1", sender_host=sender_host, auto_routes=False,
                  sender_lan_port=sc["lan_port"], lan_receive_port=free_port(), cpe_receive_port=free_port(),
                  cloud_host="127.0.0.1", cloud_control_port=cloud_cfg["control_port"],
                  vlc_port=output.getsockname()[1], keepalive_interval=0.05, reorder_timeout=0.5)
        splitter, combiner = send.Splitter(sc), receive.Combiner(rc)
        self.addCleanup(splitter.close)
        self.addCleanup(combiner.close)
        splitter.start()
        combiner.start()
        wait_for(lambda: all(combiner.online))
        self.assertEqual(combiner.lan_target, ("127.0.0.1", sc["lan_port"]))
        count = 300
        expected = [struct.pack("!I", i) + bytes([i % 256]) * 1312 for i in range(count)]
        for payload in expected:
            source.sendto(payload, ("127.0.0.1", sc["source_port"]))
            time.sleep(0.0005)
        actual = []
        deadline = time.monotonic() + 4
        while len(actual) < count and time.monotonic() < deadline:
            try:
                actual.append(output.recvfrom(65535)[0])
            except socket.timeout:
                pass
        self.assertEqual(actual, expected, "合流后内容或顺序不一致")
        self.assertEqual((combiner.stats["received0"], combiner.stats["received1"]), (150, 150))
        self.assertEqual(relay.stats["forwarded"], 150)
        self.assertGreater(splitter.stats["feedback"], 0)
        self.assertGreater(fuzzy_pid.interval_count, 10)
        self.assertFalse(splitter.stop.is_set())
        self.assertFalse(combiner.stop.is_set())
        self.assertEqual(combiner.buffer.skipped, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
