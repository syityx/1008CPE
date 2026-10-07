"""自动识别Windows两路接口，并设置仅针对云端/发送端的临时主机路由。

固定地址由config.json给出；本地接口使用auto。多个候选时在控制台选择，
不把虚拟网卡或CPE接口误当作Wi-Fi/USB链路。修改路由需要管理员权限。
"""
import ipaddress
import json
import logging
import os
import socket
import subprocess
import sys

LOG = logging.getLogger("network")


def windows_interfaces():
    # PowerShell脚本固定，网络地址通过JSON读取，不拼接为可执行命令。
    script = r"""
    $OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()
    $rows = @(foreach ($item in Get-NetIPConfiguration) {
        if ($item.NetAdapter.Status -ne 'Up') { continue }
        foreach ($address in $item.IPv4Address) {
            if ($address.IPAddress -like '169.254.*') { continue }
            [pscustomobject]@{
                ip = $address.IPAddress
                prefix = $address.PrefixLength
                index = $item.InterfaceIndex
                name = $item.InterfaceAlias
                description = $item.NetAdapter.InterfaceDescription
                gateway = ($item.IPv4DefaultGateway | Select-Object -First 1).NextHop
                wifi = ($item.NetAdapter.NdisPhysicalMedium -eq 9 -or
                        $item.InterfaceAlias -match 'Wi-Fi|WLAN|无线' -or
                        $item.NetAdapter.InterfaceDescription -match 'Wireless|Wi-Fi|802\.11')
            }
        }
    })
    ConvertTo-Json -InputObject $rows -Compress
    """
    # 不启动可见辅助窗口，主程序控制台负责显示检测结果。
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, encoding="utf-8", errors="replace",
                            timeout=20, creationflags=0x08000000)
    if result.returncode:
        raise ValueError("无法读取Windows网卡：" + result.stderr.strip())
    rows = json.loads(result.stdout.lstrip("\ufeff") or "[]")
    return rows if isinstance(rows, list) else [rows]


def is_candidate(row):
    name = (row.get("name", "") + " " + row.get("description", "")).lower()
    return bool(row.get("gateway")) and not any(
        word in name for word in ("vmware", "vethernet", "wsl", "hyper-v", "virtualbox", "loopback"))


def select_interface(rows, configured_ip, label, preferred_gateway=None, exclude_index=None, prefer_wifi=False):
    candidates = [row for row in rows if row["index"] != exclude_index]
    if configured_ip and configured_ip not in ("auto", "0.0.0.0"):
        candidates = [row for row in candidates if row["ip"] == configured_ip]
    else:
        candidates = [row for row in candidates if is_candidate(row)]
        if preferred_gateway:
            preferred = [row for row in candidates if row["gateway"] == preferred_gateway]
            if preferred:
                candidates = preferred
        elif prefer_wifi:
            preferred = [row for row in candidates if row.get("wifi")]
            if preferred:
                candidates = preferred
    if not candidates:
        raise ValueError(f"未找到{label}接口。请连接网络，或用命令行明确指定本机IPv4。")
    if len(candidates) == 1:
        return candidates[0]
    LOG.info("%s有多个候选，请选择实际连接的接口：", label)
    for index, row in enumerate(candidates, 1):
        print(f"{index}. {row['name']}  {row['ip']}  网关={row.get('gateway')}")
    if not sys.stdin.isatty():
        raise ValueError(f"无法自动唯一识别{label}，请用命令行指定IPv4。")
    try:
        index = int(input("输入接口编号："))
        if not 1 <= index <= len(candidates):
            raise ValueError
        return candidates[index - 1]
    except (ValueError, EOFError):
        raise ValueError("接口选择无效。") from None


def prepare_sender(config):
    if config.get("lan_bind_ip") not in ("auto", "", "0.0.0.0"):
        return
    if sys.platform == "win32":
        selected = select_interface(windows_interfaces(), "auto", "发送端Wi-Fi", prefer_wifi=True)
        config["lan_bind_ip"] = selected["ip"]
    else:
        # 非Windows发送端通过到云端的系统路由识别本地地址。
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((config["cloud_host"], config.get("cloud_control_port", 30007)))
            config["lan_bind_ip"] = sock.getsockname()[0]
    LOG.info("发送端局域网地址自动识别为 %s", config["lan_bind_ip"])


def prepare_receiver(config):
    if sys.platform != "win32":
        if any(config.get(key) in ("auto", "", "0.0.0.0") for key in ("lan_bind_ip", "cpe_bind_ip")):
            raise ValueError("非Windows接收端请用 --lan-ip 和 --cpe-ip 指定接口，并设置两路出口。")
        return
    rows = windows_interfaces()
    cpe = select_interface(rows, config.get("cpe_bind_ip"), "CPE以太网",
                           preferred_gateway=config.get("cpe_gateway", "192.168.2.230"))
    lan = select_interface(rows, config.get("lan_bind_ip"), "局域网Wi-Fi/手机USB",
                           exclude_index=cpe["index"], prefer_wifi=True)
    config["cpe_bind_ip"], config["lan_bind_ip"] = cpe["ip"], lan["ip"]
    config["_cpe_interface"], config["_lan_interface"] = cpe, lan
    LOG.info("局域网=%s %s；CPE=%s %s", lan["name"], lan["ip"], cpe["name"], cpe["ip"])
    if config.get("auto_routes", True):
        ensure_host_route(config["cloud_host"], cpe["gateway"], cpe["index"])


def ensure_sender_route(config, sender_ip):
    if sys.platform != "win32" or not config.get("auto_routes", True):
        return
    lan = config["_lan_interface"]
    network = ipaddress.ip_network(f"{lan['ip']}/{lan['prefix']}", strict=False)
    gateway = "0.0.0.0" if ipaddress.ip_address(sender_ip) in network else lan["gateway"]
    ensure_host_route(sender_ip, gateway, lan["index"])


def ensure_host_route(target, gateway, interface_index):
    ipaddress.IPv4Address(target)
    ipaddress.IPv4Address(gateway)
    script = r"""
    $ErrorActionPreference = 'Stop'
    $OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()
    $route = $env:CPE_EXPERIMENT_ROUTE | ConvertFrom-Json
    $prefix = "$($route.target)/32"
    $existing = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix -ErrorAction SilentlyContinue)
    $correct = @($existing | Where-Object { $_.InterfaceIndex -eq $route.index -and $_.NextHop -eq $route.gateway })
    if ($existing.Count -gt 0 -and $correct.Count -eq $existing.Count) { exit 0 }
    if ($existing.Count -gt 0) { throw "目标 $prefix 已有冲突路由，请检查现有规则。" }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '自动设置出口需要管理员权限，请使用receive/start.cmd，或以管理员身份运行。'
    }
    New-NetRoute -DestinationPrefix $prefix -InterfaceIndex $route.index -NextHop $route.gateway `
        -RouteMetric 5 -PolicyStore ActiveStore | Out-Null
    """
    env = dict(os.environ, CPE_EXPERIMENT_ROUTE=json.dumps(
        {"target": target, "gateway": gateway, "index": int(interface_index)}))
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script], env=env,
                            capture_output=True, encoding="utf-8", errors="replace",
                            timeout=20, creationflags=0x08000000)
    if result.returncode:
        raise ValueError("自动路由设置失败：" + result.stderr.strip())
    LOG.info("出口已确认：%s -> 网卡%s 网关%s", target, interface_index, gateway)
