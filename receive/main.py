"""双路 UDP 合流：Wi-Fi/手机 USB 注册到发送端，CPE 注册到阿里云。

保留原版按全局序号排序、最多等待 TIMEOUT 后跳过缺包的行为。
每一路注册、保活、接收均复用同一个绑定本地网卡地址的 socket。
"""
import argparse
import heapq
import logging
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import protocol as wire

LOG = logging.getLogger("receive")


class ReorderBuffer:
    """将序号、链路标记、负载放在同一条记录中，避免原版双队列错位。"""
    def __init__(self, timeout=0.1, max_packets=8192):
        self.timeout = timeout
        self.max_packets = max_packets
        self.condition = threading.Condition()
        self.heap = []
        self.seen = set()
        self.counts = [0, 0]
        self.next_expected = 0
        self.last_transmission = time.monotonic()
        self.skipped = self.late = self.duplicate = self.overflow = 0

    def put(self, media, number, payload):
        with self.condition:
            # 将 32 位序号展开为连续整数，允许 0xFFFFFFFF → 0 回绕。
            delta = (number - (self.next_expected & 0xFFFFFFFF)) & 0xFFFFFFFF
            if delta >= 0x80000000:
                delta -= 0x100000000
            absolute = self.next_expected + delta
            if absolute < self.next_expected:
                self.late += 1
                return False
            if absolute in self.seen:
                self.duplicate += 1
                return False
            if len(self.heap) >= self.max_packets:
                self.overflow += 1
                return False
            heapq.heappush(self.heap, (absolute, media, payload))
            self.seen.add(absolute)
            self.counts[media] += 1
            self.condition.notify_all()
            return True

    def get(self, stop):
        with self.condition:
            while not stop.is_set():
                if self.heap:
                    number, media, payload = self.heap[0]
                    remaining = self.timeout - (time.monotonic() - self.last_transmission)
                    if number == self.next_expected or remaining <= 0:
                        heapq.heappop(self.heap)
                        self.seen.remove(number)
                        self.counts[media] -= 1
                        self.skipped += number - self.next_expected
                        self.next_expected = number + 1
                        self.last_transmission = time.monotonic()
                        return payload
                    self.condition.wait(min(remaining, 0.1))
                else:
                    self.condition.wait(0.1)
        return None

    def backlog(self):
        with self.condition:
            return tuple(self.counts)


class Combiner:
    def __init__(self, config):
        self.config = config
        self.stop = threading.Event()
        self.buffer = ReorderBuffer(config["reorder_timeout"], config["max_queue_packets"])
        self.sockets = []
        self.threads = []
        self.online = [0.0, 0.0]
        self.observed = [None, None]
        self.stats = {"received0": 0, "received1": 0, "output": 0, "invalid": 0}

    def start(self):
        try:
            self.lan = wire.udp_socket(self.config["lan_bind_ip"], self.config["lan_receive_port"])
            self.sockets.append(self.lan)
            self.cpe = wire.udp_socket(self.config["cpe_bind_ip"], self.config["cpe_receive_port"])
            self.sockets.append(self.cpe)
            self.output = wire.udp_socket("127.0.0.1", 0)
            self.sockets.append(self.output)
        except Exception:
            self.close()
            raise
        targets = [(self.config["sender_host"], self.config["sender_lan_port"]),
                   (self.config["cloud_host"], self.config["cloud_control_port"])]
        jobs = [(self.rxThread, (0, self.lan, targets[0])),
                (self.rxThread, (1, self.cpe, targets[1])),
                (self.txThread, ()), (self.report, ())]
        for target, args in jobs:
            t = threading.Thread(target=self.guarded, args=(target, args), daemon=True)
            t.start()
            self.threads.append(t)
        LOG.info("局域网绑定 %s:%s；CPE绑定 %s:%s；VLC打开 udp://@:%s",
                 *self.lan.getsockname(), *self.cpe.getsockname(), self.config["vlc_port"])

    def guarded(self, target, args):
        try:
            target(*args)
        except Exception:
            LOG.exception("工作线程失败，停止程序")
            self.stop.set()

    def rxThread(self, media, sock, target):
        next_register = next_feedback = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= next_register:
                registration = wire.make_control("register", self.config["token"], self.config["experiment_id"])
                try:
                    sock.sendto(registration, target)
                except OSError as exc:
                    LOG.warning("链路 %s 注册发送失败：%s", media, exc)
                next_register = now + self.config["keepalive_interval"]
            if media == 0 and now >= next_feedback:
                # 反馈仍是原来的 121 + 两个 uint16，且明确 count0 对应链路 0。
                try:
                    sock.sendto(wire.pack_feedback(*self.buffer.backlog()), target)
                except OSError:
                    pass
                next_feedback = now + self.config["feedback_interval"]
            # 缩短等待，使 20ms 反馈不会被 200ms recv 超时拖慢。
            wait_until = next_register if media == 1 else min(next_register, next_feedback)
            sock.settimeout(max(0.001, min(0.2, wait_until - time.monotonic())))
            try:
                packet, address = sock.recvfrom(65535)
            except socket.timeout:
                continue
            if address != target:
                self.stats["invalid"] += 1
                continue
            control = wire.parse_control(packet, self.config["token"], self.config["experiment_id"])
            if control and control.get("kind") == "registered":
                self.online[media] = time.monotonic()
                observed = control.get("observed")
                if observed != self.observed[media]:
                    self.observed[media] = observed
                    LOG.info("链路 %s 注册成功；对端看到的地址=%s", media, observed)
                continue
            video = wire.unpack_video(packet)
            if video is None or video[0] != media:
                self.stats["invalid"] += 1
                continue
            _, number, payload = video
            self.stats[f"received{media}"] += 1
            self.buffer.put(media, number, payload)

    def txThread(self):
        while not self.stop.is_set():
            payload = self.buffer.get(self.stop)
            if payload is None:
                break
            self.output.sendto(payload, (self.config["vlc_host"], self.config["vlc_port"]))
            self.stats["output"] += 1

    def report(self):
        while not self.stop.wait(2):
            now = time.monotonic()
            ready = [now - last < self.config["registration_timeout"] for last in self.online]
            LOG.info("注册LAN/CPE=%s/%s 收包=%s/%s 输出=%s 缓存=%s 跳过=%s 迟到=%s 重复=%s 溢出=%s",
                     *ready, self.stats["received0"], self.stats["received1"], self.stats["output"],
                     self.buffer.backlog(), self.buffer.skipped, self.buffer.late,
                     self.buffer.duplicate, self.buffer.overflow)

    def close(self):
        self.stop.set()
        with self.buffer.condition:
            self.buffer.condition.notify_all()
        for t in self.threads:
            t.join(timeout=1)
        for sock in self.sockets:
            sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--lan-ip", help="接收端 Wi-Fi 或手机 USB 网络适配器的 IPv4")
    parser.add_argument("--cpe-ip", help="台式机连接 CPE 的以太网 IPv4")
    parser.add_argument("--sender-ip", help="笔记本发送端的 Wi-Fi IPv4")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = wire.load_config(args.config)
    for key, value in [("lan_bind_ip", args.lan_ip), ("cpe_bind_ip", args.cpe_ip), ("sender_host", args.sender_ip)]:
        if value:
            config[key] = value
    for key in ("lan_bind_ip", "cpe_bind_ip", "sender_host"):
        try:
            socket.inet_pton(socket.AF_INET, config[key])
        except OSError:
            parser.error(f"请在配置文件或命令行填写 {key} 的真实 IPv4 地址。")
        if config[key] == "0.0.0.0":
            parser.error(f"{key} 必须指定实际地址，避免两路走同一网络。")
    app = Combiner(config)
    try:
        app.start()
        while not app.stop.wait(0.5):
            pass
        return 1
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        LOG.error("启动失败：%s", exc)
        return 1
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
