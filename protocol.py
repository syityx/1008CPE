"""两端与云端共用的协议。视频的原始 5 字节包头保持不变。

链路 0 = 局域网/手机 USB；链路 1 = 阿里云/CPE。
注册报文和云端入口加 HMAC，避免公开中继被陌生报文占用。
HMAC 只验证来源，不加密视频；SSH 私钥不参与视频传输。
"""
import hashlib
import hmac
import json
import socket
import struct
from pathlib import Path

CONTROL_MAGIC = b"FZC1"
DATA_MAGIC = b"FZD1"
VIDEO_HEADER = struct.Struct("!BI")
FEEDBACK = struct.Struct("!BHH")
MAX_PAYLOAD = 1316


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if len(config.get("token", "")) < 16:
        raise ValueError("token 至少 16 个字符，两端和云端必须一致。")
    return config


def signed_message(magic, payload, token):
    signature = hmac.new(token.encode(), magic + payload, hashlib.sha256).digest()
    return magic + signature + payload


def verify_message(packet, magic, token):
    if len(packet) < 36 or not packet.startswith(magic):
        return None
    payload = packet[36:]
    signature = hmac.new(token.encode(), magic + payload, hashlib.sha256).digest()
    return payload if hmac.compare_digest(packet[4:36], signature) else None


def make_control(kind, token, experiment_id, **fields):
    content = {"kind": kind, "experiment_id": experiment_id, **fields}
    payload = json.dumps(content, separators=(",", ":"), ensure_ascii=True).encode()
    return signed_message(CONTROL_MAGIC, payload, token)


def parse_control(packet, token, experiment_id):
    if len(packet) > 4096:
        return None
    payload = verify_message(packet, CONTROL_MAGIC, token)
    if payload is None:
        return None
    try:
        content = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(content, dict) or content.get("experiment_id") != experiment_id:
        return None
    return content


def pack_video(media, number, payload):
    if media not in (0, 1) or not payload or len(payload) > MAX_PAYLOAD:
        raise ValueError("视频链路标记或包长无效。")
    return VIDEO_HEADER.pack(media, number & 0xFFFFFFFF) + payload


def unpack_video(packet):
    if not 5 < len(packet) <= MAX_PAYLOAD + 5 or packet[0] not in (0, 1):
        return None
    media, number = VIDEO_HEADER.unpack_from(packet)
    return media, number, packet[5:]


def pack_feedback(count0, count1):
    # 原协议只有 uint16；饱和计数避免队列变大时溢出。
    return FEEDBACK.pack(121, min(65535, max(0, count0)), min(65535, max(0, count1)))


def unpack_feedback(packet):
    if len(packet) != FEEDBACK.size or packet[0] != 121:
        return None
    _, count0, count1 = FEEDBACK.unpack(packet)
    return count0, count1


def udp_socket(ip, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        sock.bind((ip, port))
        sock.settimeout(0.2)
        # Windows 的 UDP 对端未启动时，不让 ICMP 错误变成接收线程异常。
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        return sock
    except Exception:
        sock.close()
        raise
