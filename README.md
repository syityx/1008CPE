# Wi-Fi与CPE双链路视频实验

发送端笔记本连接路由器Wi-Fi。接收端台式机通过手机USB共享同一Wi-Fi，并通过以太网连接CPE。蜂窝分支使用云服务器UDP中继，云地址由本地配置指定。云端支持前台运行或systemd常驻服务。

**也支持接收电脑直接连接Wi-Fi。** 接收端的 `lan_bind_ip` / `--lan-ip` 此时填写本机Wi-Fi IPv4，不再填写USB地址。局域网注册在直接Wi-Fi上同样有效，无需手机、NAT或端口映射。程序的包头、分流和合流不变；路由脚本会自动识别发送端是否位于同一网段，选择本地直连或USB网关。

```text
VLC -> 发送分流器（localhost:49998）
         ├─ 链路0：笔记本Wi-Fi -> 手机Wi-Fi/NAT -> USB -> 接收合流器
         └─ 链路1：阿里云:30006 -> 云端:30007 -> 运营商/CPE -> 接收合流器
合流器 -> localhost:60001 -> 接收端VLC
```

## 文件与依赖

- `send/main.py`：编号、双发送队列、LAN注册与缓存反馈处理。
- `send/fuzzy_pid.py`：原工程 `0623fuzzypid.py` 的模糊PID规则与计算方法。
- `receive/main.py`：两路注册/保活、按序合流、缓存反馈。
- `receive/configure_network.ps1`：接收端指定两路出口的临时路由脚本。
- `cloud_relay.py`、`cloud_config.json`：阿里云前台中继程序及配置。
- `protocol.py`：原视频包头、缓存反馈、认证信封。
- `test_experiment.py`：本地自动验证，不依赖VLC或真实云服务器，也不读取真实部署配置。
- `initialize_config.py`：从模板生成三端配置和共享随机令牌。
- `deploy/1008cpe-relay.service`：云端常驻服务，支持开机启动和退出后自动重启。

使用 Python 3.10 或更新版本，全部为标准库，无需安装 numpy。复制到另一台电脑时，保留整个 `1008CPE` 目录结构，不能只复制 `main.py`。真实配置和个人实验记录已加入忽略规则，不提交到公开仓库。

## 先填写真实地址

公开仓库只包含 `.example.json` 配置模板，不包含真实部署令牌和云服务器地址。首次使用时，在项目根目录运行：

```powershell
python initialize_config.py --cloud-host "你的云服务器公网IPv4"
```

程序生成 `send/config.json`、`receive/config.json`、`cloud_config.json`，并在三处写入相同的随机实验令牌。已有配置不会被覆盖。请把这同一套配置分别复制到两端与云端，不要在三台设备上独立生成不同令牌。已配置的本地工程可跳过初始化。

更换令牌时三处一起修改；不要把SSH私钥放入工程目录。实验令牌用于HMAC认证，不提供视频内容加密。

发送端 `send/config.json`：

- `lan_bind_ip`：建议填笔记本Wi-Fi地址；默认 `0.0.0.0` 在所有本地接口监听。
- `cloud_host`：初始化时填写的云服务器公网IPv4。

接收端 `receive/config.json`：

- `lan_bind_ip`：填写接收端Wi-Fi或手机USB网络适配器的IPv4，当前留空，防止误填其他网卡。
- `sender_host`：填写笔记本Wi-Fi IPv4，当前留空。
- `cpe_bind_ip`：示例为 `192.168.2.180`，使用时应填写接收电脑连接CPE的实际以太网IPv4。

也可不修改文件，启动时使用 `--lan-ip`、`--sender-ip`、`--cpe-ip`。配置填写错误时程序会报错退出，不会自动选择其他链路。

## 接收端路由与防火墙

只绑定源地址还不足以确保Windows选中预期出口。接收端应明确：

- 云服务器公网IPv4 `/32` -> CPE网关 -> 以太网；网关与本地地址应按实际网络调整。
- 笔记本Wi-Fi地址 `/32` -> 指定局域网接口；直接Wi-Fi同网段时on-link直连，手机USB不同网段时经手机网关。

在管理员PowerShell中运行（将占位符替换为真实地址）：

```powershell
cd "你的项目目录"
powershell -NoProfile -ExecutionPolicy Bypass -File .\receive\configure_network.ps1 -LanIp "手机USB网卡IP" -SenderIp "笔记本WiFiIP"
```

脚本只添加两个目标的临时路由，不改默认网关，不覆盖冲突规则；电脑重启后需要重新运行。若手机USB网关/地址改变，先检查旧的目标路由，再重新配置。可以使用下面的命令核对出口：

```powershell
Find-NetRoute -RemoteIPAddress "你的云服务器公网IPv4"
Find-NetRoute -RemoteIPAddress "笔记本WiFiIP"
```

Windows防火墙需要允许发送端LAN UDP 30002，以及接收端UDP 30002、30006入站（或允许本实验的Python程序）。阿里云轻量服务器控制台防火墙需要允许 **UDP 30006、UDP 30007**；TCP 22只用于SSH。系统防火墙和云控制台防火墙是两层，SSH可连接不代表UDP已放行。

## 启动顺序

1. 云服务器启动中继。如果已安装常驻服务，运行：

   ```bash
   systemctl start 1008cpe-relay.service
   systemctl status 1008cpe-relay.service --no-pager
   ```

   常驻服务不依赖SSH窗口。首次安装时，先将云端代码与配置放在 `/root/1008CPE`，再运行：

   ```bash
   install -m 644 deploy/1008cpe-relay.service /etc/systemd/system/1008cpe-relay.service
   systemctl daemon-reload
   systemctl enable --now 1008cpe-relay.service
   ```

   前台调试也可使用下面的命令，但先停止常驻服务，避免争用端口：

   ```bash
   cd /root/1008CPE
   python3 cloud_relay.py
   ```

   前台运行时保持SSH窗口打开，按Ctrl+C停止。常驻服务管理命令：`systemctl stop 1008cpe-relay.service`（停止）、`systemctl restart 1008cpe-relay.service`（重启）、`journalctl -u 1008cpe-relay.service -n 50 --no-pager`（查看日志）。

2. 笔记本启动发送端：

   ```powershell
   python send\main.py --lan-ip "笔记本WiFiIP"
   ```

3. 台式机设置两条路由后启动接收端：

   ```powershell
   python receive\main.py --lan-ip "手机USB网卡IP" --sender-ip "笔记本WiFiIP"
   ```

4. 等待接收端出现两条“注册成功”，状态为 `注册LAN/CPE=True/True`。台式机VLC打开：

   ```text
   udp://@:60001
   ```

5. 笔记本VLC选择视频文件，通过UDP向 `127.0.0.1:49998` 推流，使用MPEG-TS封装。程序只接受最大1316字节的视频负载，超长数据报会计为丢弃；优先使用VLC常见的7×188字节TS包。若有转码，码率先从两路都能承受的较低值开始。

接收端直接Wi-Fi的启动示例：`python receive\main.py --lan-ip "接收电脑WiFiIP" --sender-ip "笔记本WiFiIP"`；路由脚本同样将 `-LanIp` 改为接收电脑Wi-Fi IP。

## 保留的原算法与修正

视频包仍为 `[media:uint8][sequence:uint32大端][原视频负载]`；media=0局域网、media=1云端/CPE。每组10包按 `位置 < G` 分到链路1，初始G=5。统计窗口仍为20包、暖机条件仍为窗口计数>10。模糊规则表、0.01误差缩放、PID增益限幅、整数输出、`G=clip(5+4.5*u,0,10)` 保留。原代码已关闭的随机容量限制不再保留。

缓存反馈仍为 `[121:uint8][count0:uint16][count1:uint16]`，现在count0/1正确对应链路0/1。通过LAN注册socket返回，不再使用单独60002端口。反馈周期默认20ms、保活默认3s、注册有效期15s；策略窗口按视频包数计算，不按秒计算。断开LAN会失去反馈，G保持最后值。

合流仍采用优先序号队列，距离上次输出超过100ms时跳过缺失序号。序号/链路/负载合并为同一条队列记录，修复原双队列错位与嵌套元组问题。增加迟到/重复包丢弃、32位回绕处理、有限队列、异常退出和超时提示。去掉原工程写死的监控文件路径。发送端重启后序号重新从0开始，**接收端也应重新启动**。

云端入口只比原视频包增加36字节认证信封，云端去除信封后从UDP 30007转发原包。局域网与云端注册均不需要手动设置手机/CPE端口映射，前提是网络允许向注册的同一对端回包。

为了对照原模糊PID，保留了较粗的调节粒度：例如u=1得到G=9.5，比较规则会让10个包都走链路1。验证网络时可用 `python send\main.py --fixed-g 5` 暂时固定一半一半；正常运行不加这个参数。

## 验证与故障定位

运行 `python test_experiment.py` 验证两路UDP收发、云端转发、合流顺序、包头/反馈、超时跳过、重复/迟到包处理与PID一致性。

当前版本完成10项本地测试，覆盖配置初始化、300包双路合流、包头/反馈、认证、超时、重复/迟到包、序号回绕以及模糊PID参考结果。模糊PID原计算方法保持一致。

实验环境还完成了真实CPE到云服务器的注册与原包回传测试。网络环境变化后仍需重新验证路由、防火墙和UDP回程。完整双机VLC播放需在两端填写实际地址后验证。

确认云端中继运行后，可在接收电脑运行 `python cloud_probe.py --bind-ip 192.168.2.180`。探测成功后再启动正式接收端，它会重新登记自己的地址。正式实验运行时不要同时执行探测，以免临时替换云端接收目标。

- LAN注册失败：检查笔记本IP、USB网卡IP、到笔记本的路由、手机是否支持共享Wi-Fi、路由器客户端隔离以及Windows防火墙。
- CPE注册失败：检查云端中继是否手动运行、UDP 30007是否放行、阿里云目标路由是否走CPE。
- CPE注册成功但无蜂窝视频：检查UDP 30006是否放行，发送端云端发送计数、中继转发计数、接收端链路1收包计数。
- 视频卡顿：查看跳过/迟到/溢出计数，并核对VLC封装与码率。公网中继增加时延和抖动，100ms排序超时可能需要实验调整。
- 本地测试通过只证明程序逻辑正常；`True/True`说明注册通信正常，但确认走两种网络仍需核对路由，必要时断开一个接口观察对应收包计数。
