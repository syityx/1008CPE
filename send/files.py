"""发送端文件模式：提供本机文件，响应Wi-Fi/CPE两路下载请求。"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import network_setup
import protocol as wire
from file_transfer import FileSender


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--files-dir", default=str(Path(__file__).with_name("files")), help="文件目录，空目录自动生成1/3/5MB测试文件")
    parser.add_argument("--lan-ip", help="手动指定Wi-Fi地址，默认自动")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = wire.load_config(args.config)
    app = None
    try:
        if args.lan_ip:
            config["lan_bind_ip"] = args.lan_ip
        network_setup.prepare_sender(config)
        app = FileSender(config, args.files_dir)
        app.start()
        while not app.stop.wait(0.5):
            pass
        return 1
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        logging.error("文件发送端启动失败：%s", exc)
        return 1
    finally:
        if app:
            app.close()


if __name__ == "__main__":
    raise SystemExit(main())
