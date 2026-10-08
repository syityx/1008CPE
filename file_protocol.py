"""文件下载协议：复用注册socket，每个下载块拆成不超过1200字节的小片。

request_id唯一标识一次文件块下载，重传沿用同一标识；下一轮使用新标识，
防止上轮迟到包混入下一轮。HMAC格式与公开实验token沿用1008CPE。
"""
import math
import struct
import uuid

import protocol as wire

MAGIC = b"FZF1"
FRAGMENT_BYTES = 1200
HEADER = struct.Struct("!16sBHH")
MAX_BLOCK_BYTES = 256 * 1024
REQUEST_KINDS = {"file_manifest", "file_block"}
REPLY_KINDS = {"file_manifest_reply", "file_error"}


def valid_id(value):
    try:
        return isinstance(value, str) and len(value) == 32 and uuid.UUID(hex=value).hex == value
    except (ValueError, TypeError):
        return False


def fragment_count(size):
    return math.ceil(size / FRAGMENT_BYTES)


def pack_fragment(request_id, lane, index, total, payload, token):
    header = HEADER.pack(uuid.UUID(hex=request_id).bytes, lane, index, total)
    return wire.signed_message(MAGIC, header + payload, token)


def unpack_fragment(packet, token):
    original = wire.verify_message(packet, MAGIC, token)
    if original is None or not HEADER.size < len(original) <= HEADER.size + FRAGMENT_BYTES:
        return None
    identity, lane, index, total = HEADER.unpack_from(original)
    if lane not in (0, 1) or not 0 <= index < total <= fragment_count(MAX_BLOCK_BYTES):
        return None
    return uuid.UUID(bytes=identity).hex, lane, index, total, original[HEADER.size:]
