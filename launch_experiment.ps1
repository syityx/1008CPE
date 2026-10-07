param([Parameter(Mandatory=$true)][ValidateSet('send','receive')][string]$Role)
$ErrorActionPreference = 'Stop'
# 两端都需要开放实验UDP入站；接收端还要设置临时主机路由。
# 用户双击启动时会弹出Windows管理员确认，实验程序保留可见控制台。
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Role {1}' -f $PSCommandPath, $Role
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments -Wait
    exit
}
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
try {
    # py启动器可定位当前用户安装的Python；PATH中的python作为备用。
    $probe = 'import sys; assert sys.version_info >= (3,10), "需要Python 3.10或更新版本"; print(sys.executable)'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $python = & py -3 -c $probe
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $python = & python -c $probe
    } else {
        throw '未找到Python。请安装Python 3.10或更新版本，并启用py启动器或加入PATH。'
    }
    if ($LASTEXITCODE -ne 0 -or -not $python -or -not (Test-Path -LiteralPath $python)) {
        throw '无法启动Python 3.10或更新版本，请检查Python安装。'
    }
    $ruleName = "1008CPE-$Role-UDP"
    $ports = if ($Role -eq 'send') { @(30002) } else { @(30002,30006) }
    # 仅更新本工程自己创建的规则，限制到本次Python可执行文件和实验端口。
    if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -Name $ruleName
    }
    New-NetFirewallRule -Name $ruleName -DisplayName "1008CPE $Role UDP" `
        -Direction Inbound -Action Allow -Protocol UDP -LocalPort $ports `
        -Program $python -Profile Any | Out-Null
    & $python "$Role/main.py"
} catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
}
Read-Host '按回车关闭窗口'
