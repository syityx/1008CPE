# 以管理员 PowerShell 运行：只给云端和发送端添加 /32 路由，不改默认网关。
# ActiveStore 路由重启后失效；每次实验开始时重新运行即可。
param(
    [string]$LanIp,
    [string]$SenderIp,
    [string]$CpeIp = '192.168.2.180',
    [string]$CpeGateway = '192.168.2.230',
    [string]$CloudIp
)
$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
if (-not $CloudIp) { $CloudIp = $config.cloud_host }
if (-not $LanIp) { $LanIp = $config.lan_bind_ip }
if (-not $SenderIp) { $SenderIp = $config.sender_host }
if (-not $LanIp -or -not $SenderIp) {
    throw '请填写 -LanIp 接收端WiFi或手机USB网卡地址 和 -SenderIp 发送端WiFi地址。'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw '添加路由需要管理员 PowerShell。'
}
$lanAdapter = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $LanIp)
$cpeAdapter = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $CpeIp)
if ($lanAdapter.Count -ne 1 -or $cpeAdapter.Count -ne 1) {
    throw '无法唯一定位两个本地网卡，请核对输入的 IPv4 地址。'
}
if ($lanAdapter[0].InterfaceIndex -eq $cpeAdapter[0].InterfaceIndex) {
    throw '两路必须是不同网卡：WiFi/手机USB 与 CPE以太网。'
}
# 直接WiFi：发送端在同一网段，使用on-link路由，不绕到路由器网关。
# 手机USB：发送端在手机上游网段，使用USB接口的网关。
function Is-SameSubnet([string]$First, [string]$Second, [int]$PrefixLength) {
    $firstBytes = [System.Net.IPAddress]::Parse($First).GetAddressBytes()
    $secondBytes = [System.Net.IPAddress]::Parse($Second).GetAddressBytes()
    for ($bit = 0; $bit -lt $PrefixLength; $bit++) {
        $byteIndex = [int][Math]::Floor($bit / 8)
        $mask = 1 -shl (7 - ($bit % 8))
        if (($firstBytes[$byteIndex] -band $mask) -ne ($secondBytes[$byteIndex] -band $mask)) { return $false }
    }
    return $true
}
if (Is-SameSubnet $LanIp $SenderIp $lanAdapter[0].PrefixLength) {
    $lanGateway = '0.0.0.0'
    Write-Host '发送端在同一网段：局域网直连模式（适用于直接WiFi）。'
} else {
    $lanConfiguration = Get-NetIPConfiguration -InterfaceIndex $lanAdapter[0].InterfaceIndex
    $lanGateway = $lanConfiguration.IPv4DefaultGateway.NextHop
    if (-not $lanGateway) { throw '局域网接口没有网关，请检查手机USB共享或网络设置。' }
    Write-Host '发送端在其他网段：局域网经网关模式（适用于手机USB）。'
}

function Ensure-HostRoute([string]$Target, [string]$Gateway, [int]$IfIndex) {
    $prefix = "$Target/32"
    $existing = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0) {
        $correct = @($existing | Where-Object { $_.NextHop -eq $Gateway -and $_.InterfaceIndex -eq $IfIndex })
        if ($correct.Count -eq $existing.Count) {
            Write-Host "$prefix 已配置到正确接口。"
            return
        }
        throw "$prefix 已有其他路由；请先核对现有规则，本脚本不覆盖它。"
    }
    New-NetRoute -DestinationPrefix $prefix -InterfaceIndex $IfIndex -NextHop $Gateway `
        -RouteMetric 5 -PolicyStore ActiveStore | Out-Null
    Write-Host "已添加 $prefix -> $Gateway，网卡编号 $IfIndex"
}

Ensure-HostRoute $CloudIp $CpeGateway $cpeAdapter[0].InterfaceIndex
Ensure-HostRoute $SenderIp $lanGateway $lanAdapter[0].InterfaceIndex
Write-Host '路由完成：阿里云走CPE，发送端走指定局域网接口；默认网关未修改。'
