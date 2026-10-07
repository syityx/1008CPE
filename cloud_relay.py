"""阿里云前台 UDP 中继：30006 收视频，30007 注册并回传。

不安装后台服务；手动运行，Ctrl+C 停止。只服务一个配置好的实验。
"""
import argparse
import logging
import selectors
import socket
import threading
import time
from pathlib import Path

import protocol as wire

LOG = logging.getLogger("cloud")


class Relay:
    def __init__(self, config):
        self.config = config
        self.stop = threading.Event()
        self.peer = None
        self.peer_seen = 0.0
        self.sockets = []
        self.stats = {"forwarded": 0, "offline": 0, "invalid": 0, "registrations": 0}

    def run(self):
        selector = selectors.DefaultSelector()
        try:
            for name, key in [("data", "data_port"), ("control", "control_port")]:
                sock = wire.udp_socket(self.config["bind_ip"], self.config[key])
                self.sockets.append(sock)
                sock.setblocking(False)
                selector.register(sock, selectors.EVENT_READ, name)
                if name == "control":
                    self.control = sock
            LOG.info("UDP 中继就绪：视频入口=%s 注册/回传=%s", self.config["data_port"], self.config["control_port"])
            next_report = time.monotonic() + 2
            while not self.stop.is_set():
                for key, _ in selector.select(timeout=0.2):
                    # 每次只读一个数据报，避免视频洪流饿死注册端口。
                    try:
                        packet, address = key.fileobj.recvfrom(65535)
                    except BlockingIOError:
                        continue
                    if key.data == "control":
                        self.register(packet, address)
                    else:
                        self.forward(packet)
                if self.peer is not None and time.monotonic() - self.peer_seen >= self.config["peer_timeout"]:
                    LOG.warning("接收端保活超时，停止转发并等待重新注册")
                    self.peer = None
                if time.monotonic() >= next_report:
                    LOG.info("接收端=%s 转发=%s 未注册丢弃=%s 非法=%s", self.peer,
                             self.stats["forwarded"], self.stats["offline"], self.stats["invalid"])
                    next_report = time.monotonic() + 2
        finally:
            selector.close()
            for sock in self.sockets:
                sock.close()

    def register(self, packet, address):
        control = wire.parse_control(packet, self.config["token"], self.config["experiment_id"])
        if not control or control.get("kind") != "register":
            self.stats["invalid"] += 1
            return
        if address != self.peer:
            LOG.info("接收端注册，实际 NAT 回传地址 %s:%s", *address)
        self.peer, self.peer_seen = address, time.monotonic()
        self.stats["registrations"] += 1
        reply = wire.make_control("registered", self.config["token"], self.config["experiment_id"], observed=list(address))
        # 同一公网 IP、同一源端口回包，兼容限制较严格的 NAT。
        self.control.sendto(reply, address)

    def forward(self, packet):
        original = wire.verify_message(packet, wire.DATA_MAGIC, self.config["token"])
        video = wire.unpack_video(original) if original is not None else None
        if video is None or video[0] != 1:
            self.stats["invalid"] += 1
            return
        if self.peer is None or time.monotonic() - self.peer_seen >= self.config["peer_timeout"]:
            self.stats["offline"] += 1
            return
        try:
            self.control.sendto(original, self.peer)
            self.stats["forwarded"] += 1
        except OSError as exc:
            LOG.warning("转发失败：%s", exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("cloud_config.json")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    relay = Relay(wire.load_config(args.config))
    try:
        relay.run()
    except KeyboardInterrupt:
        LOG.info("中继已手动停止")


if __name__ == "__main__":
    main()
