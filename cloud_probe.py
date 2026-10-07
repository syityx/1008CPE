"""独立检查真实云端两个UDP端口和CPE回传，不需要VLC。

示例：python cloud_probe.py --bind-ip 192.168.2.180
会临时注册为接收端；实际实验运行时不要执行，避免替换实验接收地址。
"""
import argparse
import socket
import time
from pathlib import Path

import protocol as wire


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind-ip", default="192.168.2.180")
    args = parser.parse_args()
    cfg = wire.load_config(Path(__file__).with_name("receive") / "config.json")
    receiver = wire.udp_socket(args.bind_ip, 0)
    sender = wire.udp_socket(args.bind_ip, 0)
    target = (cfg["cloud_host"], cfg["cloud_control_port"])
    registration = wire.make_control("register", cfg["token"], cfg["experiment_id"])
    deadline = time.monotonic() + 8
    next_register = 0
    observed = None
    try:
        while time.monotonic() < deadline:
            if time.monotonic() >= next_register:
                receiver.sendto(registration, target)
                next_register = time.monotonic() + 1
            try:
                packet, address = receiver.recvfrom(4096)
            except socket.timeout:
                continue
            control = wire.parse_control(packet, cfg["token"], cfg["experiment_id"])
            if address == target and control and control.get("kind") == "registered":
                observed = control.get("observed")
                break
        if observed is None:
            print("失败：未收到云端注册回复。检查中继运行、UDP 30007、CPE出口路由。")
            return 1
        print(f"注册成功：绑定 {receiver.getsockname()}，云端看到 {observed}")
        video = wire.pack_video(1, 0, b"1008CPE-cloud-probe")
        enveloped = wire.signed_message(wire.DATA_MAGIC, video, cfg["token"])
        data_port = wire.load_config(Path(__file__).with_name("send") / "config.json")["cloud_data_port"]
        deadline = time.monotonic() + 5
        next_send = 0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                sender.sendto(enveloped, (cfg["cloud_host"], data_port))
                next_send = time.monotonic() + 0.5
            try:
                packet, address = receiver.recvfrom(4096)
            except socket.timeout:
                continue
            if address == target and packet == video:
                print("通过：云端 UDP 30006 收入、30007 转发、接收端原 socket 回包均正常。")
                return 0
        print("失败：注册成功，但未收到视频回传。优先检查云端 UDP 30006 入站规则。")
        return 1
    finally:
        receiver.close()
        sender.close()


if __name__ == "__main__":
    raise SystemExit(main())
