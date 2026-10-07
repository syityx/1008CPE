"""VLC → 原模糊 PID 分流 → 局域网/阿里云，两条独立发送队列。

运行：双击send/start.cmd，或python send/main.py；Wi-Fi地址默认自动识别。
手机 USB 存在 NAT：接收端先向本程序的 LAN socket 注册，
本程序必须从该同一个 socket 回传链路 0，不能另开随机源端口。
"""
import argparse
import logging
import queue
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import protocol as wire
import fuzzy_pid
import network_setup

LOG = logging.getLogger("send")


def choose_media(number, g):
    """保留原规则：每 10 个包中位置 < G 的包走链路 1。"""
    return 1 if number % 10 < g else 0


class Splitter:
    def __init__(self, config):
        self.config = config
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.queues = [queue.PriorityQueue(config["max_queue_packets"]) for _ in range(2)]
        self.controller = fuzzy_pid.strategy()
        fuzzy_pid.interval_count = 0
        self.g = config["initial_g"]
        self.packet_count = 0
        self.peer = None
        self.peer_seen = 0.0
        self.stats = {"input": 0, "sent0": 0, "sent1": 0, "dropped": 0, "feedback": 0}
        self.sockets = []
        self.threads = []

    def start(self):
        try:
            self.source = wire.udp_socket(self.config["source_ip"], self.config["source_port"])
            self.sockets.append(self.source)
            self.lan = wire.udp_socket(self.config["lan_bind_ip"], self.config["lan_port"])
            self.sockets.append(self.lan)
            cloud_ip = self.config["cloud_bind_ip"]
            if cloud_ip in ("auto", "0.0.0.0", ""):
                cloud_ip = self.config["lan_bind_ip"]
            self.cloud = wire.udp_socket(cloud_ip, 0)
            self.sockets.append(self.cloud)
        except Exception:
            self.close()
            raise
        jobs = [(self.rxThread_Source, ()), (self.rxThread_Target, ()),
                (self.txThread, (0,)), (self.txThread, (1,)),
                (self.announce_sender, ()), (self.report, ())]
        for target, args in jobs:
            t = threading.Thread(target=self.guarded, args=(target, args), daemon=True)
            t.start()
            self.threads.append(t)
        LOG.info("VLC 输入 %s:%s；局域网注册入口 %s:%s；云端 %s:%s",
                 *self.source.getsockname(), *self.lan.getsockname(),
                 self.config["cloud_host"], self.config["cloud_data_port"])

    def guarded(self, target, args):
        try:
            target(*args)
        except Exception:
            LOG.exception("工作线程失败，停止程序")
            self.stop.set()

    def rxThread_Source(self):
        while not self.stop.is_set():
            try:
                payload, _ = self.source.recvfrom(65535)
            except socket.timeout:
                continue
            if not payload or len(payload) > wire.MAX_PAYLOAD:
                self.stats["dropped"] += 1
                continue
            with self.lock:
                # 用独立总包数统计窗口，序号回绕不会破坏 20 包窗口。
                if self.packet_count and self.packet_count % self.config["strategy_interval"] == 0:
                    fuzzy_pid.interval_count += 1
                number = self.packet_count & 0xFFFFFFFF
                media = choose_media(self.packet_count, self.g)
                self.packet_count += 1
            packet = wire.pack_video(media, number, payload)
            try:
                self.queues[media].put_nowait((self.packet_count, packet))
            except queue.Full:
                self.stats["dropped"] += 1
            self.stats["input"] += 1

    def announce_sender(self):
        """将本机LAN地址告知云端，接收端无需手填笔记本的动态Wi-Fi地址。

        公告只是地址发现；链路0的视频仍由发送端直接回传到接收端。
        """
        target = (self.config["cloud_host"], self.config.get("cloud_control_port", 30007))
        announced = False
        next_announce = 0.0
        while not self.stop.is_set():
            if time.monotonic() >= next_announce:
                packet = wire.make_control("announce_sender", self.config["token"],
                                           self.config["experiment_id"],
                                           lan_ip=self.config["lan_bind_ip"], lan_port=self.config["lan_port"])
                try:
                    self.cloud.sendto(packet, target)
                except OSError as exc:
                    LOG.warning("发送端地址公告失败：%s", exc)
                next_announce = time.monotonic() + self.config.get("announce_interval", 3)
            try:
                packet, address = self.cloud.recvfrom(4096)
            except socket.timeout:
                continue
            if address != target:
                continue
            reply = wire.parse_control(packet, self.config["token"], self.config["experiment_id"])
            if reply and reply.get("kind") == "sender_announced" and not announced:
                LOG.info("云端已记录发送端LAN地址，接收端会自动获取")
                announced = True

    def rxThread_Target(self):
        """同一 LAN 端口接收注册和原格式的缓存反馈。"""
        while not self.stop.is_set():
            try:
                packet, address = self.lan.recvfrom(4096)
            except socket.timeout:
                continue
            control = wire.parse_control(packet, self.config["token"], self.config["experiment_id"])
            if control and control.get("kind") == "register":
                with self.lock:
                    changed = address != self.peer
                    self.peer, self.peer_seen = address, time.monotonic()
                if changed:
                    LOG.info("局域网接收端已注册，实际回传目标 %s:%s", *address)
                reply = wire.make_control("registered", self.config["token"],
                                          self.config["experiment_id"], observed=list(address))
                self.lan.sendto(reply, address)
                continue
            feedback = wire.unpack_feedback(packet)
            with self.lock:
                if feedback is None or address != self.peer:
                    continue
                count0, count1 = feedback
                self.stats["feedback"] += 1
                increase = self.controller.data_process(count0, count1)
                # 保留原版暖机条件、整数 PID 输出、4.5 系数和 G 的 0..10 限幅。
                if (self.config.get("fixed_g") is None and increase != 9.9
                        and fuzzy_pid.interval_count > self.config["warmup_intervals"]):
                    self.g = min(10, max(0, 5 + 4.5 * increase))

    def txThread(self, media):
        sock = self.lan if media == 0 else self.cloud
        while not self.stop.is_set():
            if media == 0:
                with self.lock:
                    peer = self.peer if time.monotonic() - self.peer_seen < self.config["peer_timeout"] else None
                if peer is None:
                    self.stop.wait(0.05)
                    continue
                target = peer
            else:
                target = (self.config["cloud_host"], self.config["cloud_data_port"])
            try:
                _, packet = self.queues[media].get(timeout=0.1)
            except queue.Empty:
                continue
            if media == 1:
                # 云端去掉认证信封，再将原始 5 字节包头的视频包原样转发。
                packet = wire.signed_message(wire.DATA_MAGIC, packet, self.config["token"])
            try:
                sock.sendto(packet, target)
                self.stats[f"sent{media}"] += 1
            except OSError as exc:
                self.stats["dropped"] += 1
                LOG.warning("链路 %s 发送失败：%s", media, exc)

    def report(self):
        while not self.stop.wait(2):
            LOG.info("输入=%s LAN发送=%s 云端发送=%s 丢弃=%s 反馈=%s G=%.2f 队列=%s/%s",
                     self.stats["input"], self.stats["sent0"], self.stats["sent1"],
                     self.stats["dropped"], self.stats["feedback"], self.g,
                     self.queues[0].qsize(), self.queues[1].qsize())

    def close(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=1)
        for sock in self.sockets:
            sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--lan-ip", help="发送笔记本的 Wi-Fi IPv4 地址")
    parser.add_argument("--fixed-g", type=float, help="验证链路时暂时固定 G（0..10），默认使用 fuzzy PID")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = wire.load_config(args.config)
    if args.lan_ip:
        config["lan_bind_ip"] = args.lan_ip
    if args.fixed_g is not None:
        if not 0 <= args.fixed_g <= 10:
            parser.error("--fixed-g 必须在 0..10 范围内")
        config["initial_g"] = config["fixed_g"] = args.fixed_g
    app = Splitter(config)
    try:
        network_setup.prepare_sender(config)
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
