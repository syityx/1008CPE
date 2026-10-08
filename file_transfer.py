"""1008CPE两路文件下载：发送端供文件，接收端调度/合并，云端仅转发。

沿用Wi-Fi/USB主动注册、CPE保活、云端地址发现。UDP上补充有限重试、
缺片重传和SHA256校验；单路每次一个在途文件块，不跨路抢块或跳过缺块。
"""
import csv
import hashlib
import json
import logging
import math
import socket
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import file_protocol as files
import network_setup
import protocol as wire
from send import fuzzy_pid

LOG = logging.getLogger("files")


class Cancelled(Exception):
    pass


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_demo_files(folder):
    """目录为空时生成小/中/大文件；已有文件目录由用户决定内容，不覆盖。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if any(path.is_file() for path in folder.iterdir()):
        return
    for name, size in (("small.txt", 1000000), ("medium.txt", 3000000), ("large.txt", 5000000)):
        pattern = (("1008CPE dual-path download: " + name + "\n") * 100).encode()
        with (folder / name).open("xb") as output:
            while size:
                chunk = pattern[:min(size, len(pattern))]
                output.write(chunk)
                size -= len(chunk)


class FileSender:
    def __init__(self, config, folder):
        self.config = config
        self.folder = Path(folder).resolve()
        create_demo_files(self.folder)
        order = {"small.txt": 0, "medium.txt": 1, "large.txt": 2}
        paths = sorted((p for p in self.folder.iterdir() if p.is_file()),
                       key=lambda p: (order.get(p.name, 3), p.name))
        if not 1 <= len(paths) <= 8:
            raise ValueError("文件目录需要1至8个文件。")
        self.paths, self.snapshots, self.manifest = [], [], []
        for path in paths:
            if path.resolve().parent != self.folder or len(path.name) > 64:
                raise ValueError("文件不能链接到目录外，文件名最多64字符。")
            stat = path.stat()
            self.paths.append(path)
            self.snapshots.append((stat.st_size, stat.st_mtime_ns))
            self.manifest.append(dict(name=path.name, size=stat.st_size, sha256=file_hash(path)))
        self.stop = threading.Event()
        self.sockets, self.threads = [], []
        self.peer, self.peer_seen = None, 0.0
        self.sent_bytes = [0, 0]
        self.burst_packets = config.get("file_burst_packets", 8)
        self.burst_interval = config.get("file_burst_interval", 0.001)
        if type(self.burst_packets) is not int or not 1 <= self.burst_packets <= 64 or self.burst_interval < 0:
            raise ValueError("发片批量需要1至64包，批次间隔不能为负数。")

    def control(self, kind, **fields):
        return wire.make_control(kind, self.config["token"], self.config["experiment_id"], **fields)

    def start(self):
        try:
            self.lan = wire.udp_socket(self.config["lan_bind_ip"], self.config["lan_port"])
            self.sockets.append(self.lan)
            ip = self.config.get("cloud_bind_ip", "auto")
            if ip in ("auto", "", "0.0.0.0"):
                ip = self.config["lan_bind_ip"]
            self.cloud = wire.udp_socket(ip, 0)
            self.sockets.append(self.cloud)
            for lane, sock in enumerate(self.sockets):
                thread = threading.Thread(target=self.serve, args=(lane, sock), daemon=True)
                self.threads.append(thread)
                thread.start()
        except Exception:
            self.close()
            raise
        LOG.info("文件发送端就绪：LAN=%s 文件=%s；等待接收端请求，不需要VLC", self.lan.getsockname(),
                 [item["name"] for item in self.manifest])

    def serve(self, lane, sock):
        cloud = (self.config["cloud_host"], self.config["cloud_control_port"])
        next_announce = 0.0
        try:
            while not self.stop.is_set():
                if lane == 1 and time.monotonic() >= next_announce:
                    sock.sendto(self.control("announce_sender", lan_ip=self.config["lan_bind_ip"],
                                            lan_port=self.config["lan_port"], service="files"), cloud)
                    next_announce = time.monotonic() + self.config.get("announce_interval", 3)
                try:
                    packet, address = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                if lane == 1 and address != cloud:
                    continue
                message = wire.parse_control(packet, self.config["token"], self.config["experiment_id"])
                if not message:
                    continue
                if lane == 0 and message.get("kind") == "register":
                    if address != self.peer:
                        LOG.info("局域网接收端已注册：%s", address)
                    self.peer, self.peer_seen = address, time.monotonic()
                    sock.sendto(self.control("registered", observed=list(address), service="files"), address)
                    continue
                if message.get("kind") not in files.REQUEST_KINDS:
                    continue
                if lane == 0 and (address != self.peer or time.monotonic() - self.peer_seen >= self.config["peer_timeout"]):
                    continue
                self.answer(lane, sock, address, message)
        except Exception:
            if not self.stop.is_set():
                LOG.exception("文件服务线程失败")
                self.stop.set()

    def answer(self, lane, sock, address, message):
        identity = message.get("request_id")
        if not files.valid_id(identity):
            return
        try:
            if message["kind"] == "file_manifest":
                reply = self.control("file_manifest_reply", request_id=identity, files=self.manifest)
                if len(reply) > 4096:
                    raise ValueError("文件清单过大，请缩短文件名或减少文件数。")
                sock.sendto(reply, address)
                return
            index, offset, size = (message.get(k) for k in ("file_index", "offset", "size"))
            if any(type(v) is not int for v in (index, offset, size)):
                raise ValueError("文件块参数必须为整数。")
            if not 0 <= index < len(self.paths) or not 1 <= size <= files.MAX_BLOCK_BYTES:
                raise ValueError("文件编号或块大小无效。")
            path = self.paths[index]
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != self.snapshots[index]:
                raise ValueError("发送文件已改变，请停止两端并重新启动。")
            if not 0 <= offset < stat.st_size or offset + size > stat.st_size:
                raise ValueError("文件块范围越界。")
            total = files.fragment_count(size)
            wanted = message.get("missing")
            if wanted is None:
                wanted = range(total)
            elif not isinstance(wanted, list) or not wanted or len(wanted) > total or any(
                    type(v) is not int or not 0 <= v < total for v in wanted):
                raise ValueError("缺片编号无效。")
            with path.open("rb") as source:
                source.seek(offset)
                block = source.read(size)
            if len(block) != size:
                raise ValueError("发送文件读取不完整。")
            target = address if lane == 0 else (self.config["cloud_host"], self.config["cloud_data_port"])
            for position, part in enumerate(wanted, 1):
                if self.stop.is_set():
                    return
                payload = block[part * files.FRAGMENT_BYTES:(part + 1) * files.FRAGMENT_BYTES]
                sock.sendto(files.pack_fragment(identity, lane, part, total, payload, self.config["token"]), target)
                self.sent_bytes[lane] += len(payload)
                # 小批次发片，既限制UDP突发，也避免Windows逐片等待的计时粒度限速。
                if position % self.burst_packets == 0:
                    self.stop.wait(self.burst_interval)
        except (OSError, ValueError) as exc:
            sock.sendto(self.control("file_error", request_id=identity, error=str(exc)), address)

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(1)
        for sock in self.sockets:
            sock.close()


class DownloadTransport:
    """两个socket的注册/接收线程，为每路提供可靠文件块请求。"""
    def __init__(self, config):
        self.config = config
        self.stop = threading.Event()
        self.condition = threading.Condition()
        self.pending = {}
        self.sockets, self.threads = [], []
        self.targets = [None if config["sender_host"] in ("auto", "", "0.0.0.0") else
                        (config["sender_host"], config["sender_lan_port"]),
                        (config["cloud_host"], config["cloud_control_port"])]
        self.auto_sender = self.targets[0] is None
        self.online = [0.0, 0.0]
        self.received_bytes, self.retries = [0, 0], [0, 0]

    def control(self, kind, **fields):
        return wire.make_control(kind, self.config["token"], self.config["experiment_id"], **fields)

    def start(self):
        try:
            for lane, ipkey, portkey in ((0, "lan_bind_ip", "lan_receive_port"),
                                         (1, "cpe_bind_ip", "cpe_receive_port")):
                sock = wire.udp_socket(self.config[ipkey], self.config[portkey])
                self.sockets.append(sock)
                thread = threading.Thread(target=self.receive, args=(lane, sock), daemon=True)
                self.threads.append(thread)
                thread.start()
        except Exception:
            self.close()
            raise

    def receive(self, lane, sock):
        next_register = 0.0
        try:
            while not self.stop.is_set():
                with self.condition:
                    target = self.targets[lane]
                if target is None:
                    self.stop.wait(0.05)
                    continue
                if time.monotonic() >= next_register:
                    sock.sendto(self.control("register"), target)
                    next_register = time.monotonic() + self.config["keepalive_interval"]
                try:
                    packet, address = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                if address != target:
                    continue
                message = wire.parse_control(packet, self.config["token"], self.config["experiment_id"])
                if message and message.get("kind") == "registered":
                    if lane == 1 and self.auto_sender:
                        endpoint = message.get("sender_lan")
                        new_target = wire.lan_endpoint(*endpoint) if isinstance(endpoint, list) and len(endpoint) == 2 else None
                        if new_target and new_target != self.targets[0]:
                            network_setup.ensure_sender_route(self.config, new_target[0])
                            with self.condition:
                                self.targets[0] = new_target
                                self.online[0] = 0.0
                            LOG.info("自动发现发送端LAN地址：%s", new_target)
                    if lane == 0 and message.get("service") != "files":
                        raise ValueError("发送端正在运行视频模式，请两端都使用文件模式启动。")
                    if not self.online[lane]:
                        LOG.info("链路%s注册成功，本机=%s，对端看到=%s", lane, sock.getsockname(), message.get("observed"))
                    with self.condition:
                        self.online[lane] = time.monotonic()
                        self.condition.notify_all()
                    continue
                fragment = files.unpack_fragment(packet, self.config["token"])
                with self.condition:
                    if message and message.get("kind") in files.REPLY_KINDS:
                        box = self.pending.get(message.get("request_id"))
                        if box is not None and box["lane"] == lane:
                            box["reply"] = message
                            self.condition.notify_all()
                    elif fragment and fragment[1] == lane:
                        identity, _, index, total, payload = fragment
                        self.received_bytes[lane] += len(payload)
                        box = self.pending.get(identity)
                        if box is not None and box["lane"] == lane and total == box.get("total"):
                            length = min(files.FRAGMENT_BYTES, box["size"] - index * files.FRAGMENT_BYTES)
                            if len(payload) == length:
                                box["parts"].setdefault(index, payload)
                                self.condition.notify_all()
        except Exception:
            if not self.stop.is_set():
                LOG.exception("链路%s接收失败", lane)
                self.stop.set()
                with self.condition:
                    self.condition.notify_all()

    def wait_ready(self, timeout=60):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not all(self.online):
                if self.stop.is_set():
                    raise Cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("双路注册超时，请确认发送端已启动、CPE上网和Wi-Fi互通。")
                self.condition.wait(0.1)
        LOG.info("注册LAN/CPE=True/True，开始循环文件下载")

    def request(self, lane, kind, **fields):
        identity = uuid.uuid4().hex
        box = dict(lane=lane, reply=None, parts={}, **fields)
        if kind == "file_block":
            box["total"] = files.fragment_count(fields["size"])
        with self.condition:
            self.pending[identity] = box
        try:
            for attempt in range(self.config.get("file_max_retries", 5) + 1):
                if self.stop.is_set():
                    raise Cancelled()
                with self.condition:
                    target = self.targets[lane]
                    missing = [i for i in range(box.get("total", 0)) if i not in box["parts"]]
                request = dict(fields)
                if kind == "file_block" and attempt:
                    request["missing"] = missing
                self.sockets[lane].sendto(self.control(kind, request_id=identity, **request), target)
                deadline = time.monotonic() + self.config.get("file_request_timeout", 1.0)
                with self.condition:
                    while not self.stop.is_set():
                        if box["reply"]:
                            if box["reply"]["kind"] == "file_error":
                                raise ValueError(box["reply"].get("error", "发送端拒绝请求"))
                            return box["reply"], attempt
                        if kind == "file_block" and len(box["parts"]) == box["total"]:
                            return b"".join(box["parts"][i] for i in range(box["total"])), attempt
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self.condition.wait(min(remaining, 0.1))
                if attempt < self.config.get("file_max_retries", 5):
                    self.retries[lane] += 1
            raise TimeoutError("链路%s请求失败，超过重试次数；不跳过缺块、不自动换路。" % lane)
        finally:
            with self.condition:
                self.pending.pop(identity, None)

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
        for thread in self.threads:
            thread.join(1)
        for sock in self.sockets:
            sock.close()


class FileDownloader:
    def __init__(self, config, transport, output):
        self.config, self.transport = config, transport
        self.output = Path(output)
        self.stop = transport.stop
        self.controller = fuzzy_pid.strategy()
        fuzzy_pid.interval_count = 0
        self.g, self.units, self.updates = 5.0, 0, 0
        self.assigned = [0, 0]
        self.completed, self.round = 0, 0
        self.pending_counts = [0, 0]
        self.current_file = None
        if config.get("file_mode", "fuzzy") not in ("fuzzy", "fixed"):
            raise ValueError("file_mode需要是fuzzy或fixed。")
        self.block_size = int(config.get("file_block_kib", 32)) * 1024
        self.window = int(config.get("file_window", 32))
        if not 4096 <= self.block_size <= files.MAX_BLOCK_BYTES or not 2 <= self.window <= 128:
            raise ValueError("文件块大小需要4至256 KiB，缓存窗口需要2至128块。")

    def choose(self):
        # 原20单位窗口、位置<G、暖机和PID公式不变；一个调度单位现在是文件块。
        fuzzy_pid.interval_count = self.units // 20
        lane = 1 if self.units % 10 < self.g else 0
        self.units += 1
        self.assigned[lane] += 1
        return lane

    def feedback(self, pending):
        self.pending_counts = [sum(lane == i for lane, _ in pending.values()) for i in (0, 1)]
        increase = self.controller.data_process(*self.pending_counts)
        if self.config.get("file_mode", "fuzzy") == "fuzzy" and increase != 9.9 and fuzzy_pid.interval_count > 10:
            self.g = min(10, max(0, 5 + 4.5 * increase))
            self.updates += 1
        self.controls.writerow([time.monotonic() - self.started, self.units, fuzzy_pid.interval_count,
                                *self.pending_counts, self.g, self.updates])

    def tick(self):
        if self.stop.is_set():
            raise Cancelled()
        now = time.monotonic()
        duration = self.config.get("file_duration", 0)
        if duration and now - self.started >= duration:
            self.reason = "duration"
            self.stop.set()
            raise Cancelled()
        if now - self.last_sample >= 0.2:
            current = self.transport.received_bytes.copy()
            elapsed = now - self.last_sample
            rates = [(current[i] - self.last_bytes[i]) * 8 / elapsed / 1e6 for i in (0, 1)]
            self.samples.writerow([now - self.started, *current, *rates, *self.pending_counts, self.g])
            self.last_bytes, self.last_sample = current, now
        if now - self.last_log >= 2:
            LOG.info("第%s轮 已完成=%s文件 分配块LAN/CPE=%s 收到字节=%s 重试=%s 待合并=%s G=%.2f",
                     self.round, self.completed, self.assigned, self.transport.received_bytes,
                     self.transport.retries, self.pending_counts, self.g)
            self.last_log = now
            for stream in self.streams:
                stream.flush()

    def fetch_block(self, lane, index, offset, size):
        before = time.monotonic()
        data, retries = self.transport.request(lane, "file_block", file_index=index, offset=offset, size=size)
        return data, retries, time.monotonic() - before

    def download_file(self, index, item, pool):
        number = math.ceil(item["size"] / self.block_size)
        queues, active, pending = [deque(), deque()], [None, None], {}
        issued = expected = committed = 0
        digest = hashlib.sha256()
        before = time.monotonic()
        self.current_file = dict(item=item, committed=0, digest=digest, started=before)
        folder = self.output / "downloads"
        folder.mkdir(exist_ok=True)
        part = folder / (item["name"] + ".part")
        last_feedback = 0.0
        with part.open("wb") as target:
            while expected < number:
                self.tick()
                while issued < number and issued - expected < self.window:
                    lane = self.choose()
                    queues[lane].append(issued)
                    issued += 1
                for lane in (0, 1):
                    if active[lane] is None and queues[lane]:
                        block = queues[lane].popleft()
                        offset = block * self.block_size
                        size = min(self.block_size, item["size"] - offset)
                        active[lane] = (block, pool.submit(self.fetch_block, lane, index, offset, size))
                changed = False
                for lane in (0, 1):
                    if active[lane] and active[lane][1].done():
                        block, future = active[lane]
                        data, retries, seconds = future.result()
                        pending[block] = (lane, data)
                        self.blocks.writerow([self.round, item["name"], block, lane, len(data), retries, seconds])
                        active[lane] = None
                        changed = True
                now = time.monotonic()
                if changed or now - last_feedback >= 0.1:
                    self.feedback(pending)
                    last_feedback = now
                while expected in pending:
                    _, data = pending.pop(expected)
                    target.write(data)
                    digest.update(data)
                    committed += len(data)
                    self.current_file["committed"] = committed
                    expected += 1
                self.pending_counts = [sum(lane == i for lane, _ in pending.values()) for i in (0, 1)]
                self.stop.wait(0.003)
        if committed != item["size"] or digest.hexdigest() != item["sha256"]:
            raise ValueError("文件完整性校验失败：" + item["name"])
        part.replace(folder / item["name"])
        self.completed += 1
        seconds = time.monotonic() - before
        self.records.writerow([self.round, item["name"], item["size"], committed, seconds, digest.hexdigest(), "complete"])
        self.current_file = None
        LOG.info("第%s轮 %s 下载完成：%s字节，%.2fs，SHA256一致", self.round, item["name"], committed, seconds)

    def run(self):
        self.output.mkdir(parents=True, exist_ok=True)
        self.transport.wait_ready(self.config.get("file_registration_timeout", 60))
        manifests = [self.transport.request(lane, "file_manifest")[0]["files"] for lane in (0, 1)]
        if manifests[0] != manifests[1]:
            raise ValueError("两路文件清单不同，请确认两路连接同一个发送端。")
        manifest = manifests[0]
        if not isinstance(manifest, list) or not 1 <= len(manifest) <= 8:
            raise ValueError("文件清单格式无效。")
        names = set()
        for item in manifest:
            name = item.get("name")
            if not isinstance(name, str) or not name or name in (".", "..") or any(c in name for c in '/\\:') or name in names:
                raise ValueError("文件名无效或重复。")
            if type(item.get("size")) is not int or item["size"] < 0 or not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64:
                raise ValueError("文件大小或哈希格式无效。")
            names.add(name)
        self.started = self.last_sample = self.last_log = time.monotonic()
        self.last_bytes = self.transport.received_bytes.copy()
        self.reason, self.error = "rounds", ""
        self.streams = []
        def writer(name, fields):
            stream = (self.output / name).open("w", newline="", encoding="utf-8-sig")
            self.streams.append(stream)
            output = csv.writer(stream)
            output.writerow(fields)
            return output
        self.records = writer("files.csv", ["round", "file", "size", "committed_bytes", "seconds", "sha256", "status"])
        self.blocks = writer("blocks.csv", ["round", "file", "block", "lane", "size", "retries", "seconds"])
        self.controls = writer("control.csv", ["elapsed", "units", "cycle", "queue_lan", "queue_cpe", "g", "updates"])
        self.samples = writer("throughput.csv", ["elapsed", "lan_bytes", "cpe_bytes", "lan_mbps", "cpe_mbps", "queue_lan", "queue_cpe", "g"])
        saved = {k: v for k, v in self.config.items() if not k.startswith("_")}
        (self.output / "config.json").write_text(json.dumps(dict(config=saved, files=manifest), ensure_ascii=False, indent=2), encoding="utf-8")
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            while not self.config.get("file_rounds", 0) or self.round < self.config["file_rounds"]:
                self.round += 1
                for index, item in enumerate(manifest):
                    self.tick()
                    self.download_file(index, item, pool)
        except (Cancelled, KeyboardInterrupt):
            if self.reason != "duration":
                self.reason = "user_stop"
        except Exception as exc:
            self.reason, self.error = "error", str(exc)
            raise
        finally:
            self.stop.set()
            with self.transport.condition:
                self.transport.condition.notify_all()
            pool.shutdown(wait=True, cancel_futures=True)
            # 补记最后不足200ms的尾段，保证累计接收字节与summary一致。
            now = time.monotonic()
            current_bytes = self.transport.received_bytes.copy()
            seconds = max(now - self.last_sample, 0.000001)
            rates = [(current_bytes[i] - self.last_bytes[i]) * 8 / seconds / 1e6 for i in (0, 1)]
            self.samples.writerow([now - self.started, *current_bytes, *rates, *self.pending_counts, self.g])
            if self.current_file:
                current = self.current_file
                self.records.writerow([self.round, current["item"]["name"], current["item"]["size"], current["committed"],
                                       time.monotonic() - current["started"], current["digest"].hexdigest(),
                                       "failed" if self.error else "stopped"])
            result = dict(reason=self.reason, error=self.error, completed_files=self.completed,
                          rounds_started=self.round, assigned_blocks=self.assigned, received_bytes=self.transport.received_bytes,
                          retries=self.transport.retries, g=self.g, pid_updates=self.updates,
                          elapsed=time.monotonic() - self.started, results_dir=str(self.output))
            (self.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            for stream in self.streams:
                stream.close()
        return result
