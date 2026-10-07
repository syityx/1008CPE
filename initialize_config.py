"""从公开示例配置生成三端本地配置，自动生成相同的随机实验令牌。

运行：python initialize_config.py --cloud-host 你的云服务器公网IPv4
已有本地配置不会被覆盖；本地config.json已加入.gitignore。
"""
import argparse
import json
import secrets
import socket
from pathlib import Path

CONFIG_PATHS = ("send/config.json", "receive/config.json", "cloud_config.json")


def initialize_configs(root, cloud_host=None):
    root = Path(root)
    existing = {}
    for relative in CONFIG_PATHS:
        path = root / relative
        if path.exists():
            existing[relative] = json.loads(path.read_text(encoding="utf-8-sig"))
    if len(existing) == len(CONFIG_PATHS):
        return []
    tokens = {cfg.get("token", "") for cfg in existing.values()}
    if tokens and (len(tokens) != 1 or len(next(iter(tokens))) < 16):
        raise ValueError("已有配置的令牌无效或不一致，请先检查，程序不会覆盖它们。")
    token = next(iter(tokens)) if tokens else secrets.token_hex(24)
    hosts = {cfg["cloud_host"] for cfg in existing.values() if "cloud_host" in cfg}
    if len(hosts) > 1:
        raise ValueError("已有两端配置的云服务器地址不一致。")
    if hosts:
        saved_host = next(iter(hosts))
        if cloud_host and cloud_host != saved_host:
            raise ValueError("指定的云地址与已有配置不同，程序不会修改已有配置。")
        cloud_host = saved_host
    if not cloud_host:
        raise ValueError("首次初始化请指定 --cloud-host 云服务器公网IPv4。")
    try:
        socket.inet_pton(socket.AF_INET, cloud_host)
    except OSError:
        raise ValueError("--cloud-host 必须是有效的IPv4地址。") from None
    planned = []
    # 先读取全部模板，再写文件；缺少模板时不生成半套配置。
    for relative in CONFIG_PATHS:
        if relative in existing:
            continue
        path = root / relative
        example = path.with_name(path.stem + ".example.json")
        cfg = json.loads(example.read_text(encoding="utf-8-sig"))
        cfg["token"] = token
        if "cloud_host" in cfg:
            cfg["cloud_host"] = cloud_host
        planned.append((path, cfg))
    for path, cfg in planned:
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return [str(path.relative_to(root)) for path, _ in planned]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud-host", help="云服务器公网IPv4")
    args = parser.parse_args()
    try:
        created = initialize_configs(Path(__file__).resolve().parent, args.cloud_host)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if created:
        print("已生成：" + "、".join(created))
        print("三端已使用相同的随机令牌。请把这套配置分别提供给两端和云端，不要各自重复生成。")
    else:
        print("三份本地配置均已存在，未修改。")


if __name__ == "__main__":
    main()
