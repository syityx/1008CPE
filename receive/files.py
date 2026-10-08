"""接收端文件模式：自动发现发送端，两路分块下载，校验后循环下一文件。"""
import argparse
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import network_setup
import protocol as wire
from file_transfer import DownloadTransport, FileDownloader, Cancelled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--lan-ip")
    parser.add_argument("--cpe-ip")
    parser.add_argument("--sender-ip")
    parser.add_argument("--mode", choices=("fixed", "fuzzy"), help="fixed为固定五五，fuzzy沿用原PID")
    parser.add_argument("--rounds", type=int, help="下载轮数，0为不限")
    parser.add_argument("--duration", type=float, help="下载总秒数，0为不限")
    parser.add_argument("--block-kib", type=int)
    parser.add_argument("--output", help="结果目录，默认receive/results下新建实验目录")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = wire.load_config(args.config)
    app = None
    try:
        overrides = dict(lan_bind_ip=args.lan_ip, cpe_bind_ip=args.cpe_ip, sender_host=args.sender_ip,
                         file_mode=args.mode, file_rounds=args.rounds, file_duration=args.duration, file_block_kib=args.block_kib)
        config.update({key: value for key, value in overrides.items() if value is not None})
        if config.get("file_rounds", 0) < 0 or config.get("file_duration", 0) < 0:
            raise ValueError("轮数与时长不能为负数。")
        network_setup.prepare_receiver(config)
        if config["sender_host"] not in ("auto", "", "0.0.0.0"):
            network_setup.ensure_sender_route(config, config["sender_host"])
        name = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        output = Path(args.output) if args.output else Path(__file__).with_name("results") / name
        app = DownloadTransport(config)
        downloader = FileDownloader(config, app, output)
        app.start()
        logging.info("结果目录：%s；按Ctrl+C停止并保存", output)
        result = downloader.run()
        logging.info("下载结束：%s", result)
        return 0
    except (Cancelled, KeyboardInterrupt):
        return 0
    except (OSError, ValueError, KeyError) as exc:
        logging.error("文件下载失败：%s", exc)
        return 1
    finally:
        if app:
            app.close()


if __name__ == "__main__":
    raise SystemExit(main())
