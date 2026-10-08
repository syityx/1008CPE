param(
    [Parameter(Mandatory=$true)][ValidateSet('send','receive')][string]$Role,
    [string]$PythonPath,
    [switch]$CheckPython,
    [switch]$Video
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'

function Find-ExperimentPython([string]$PreferredPath) {
    # 不在-c中嵌套字符串引号，兼容Windows PowerShell 5.1的参数传递规则。
    $probe = 'import sys; sys.exit(10) if sys.version_info < (3,10) else print(sys.executable)'
    $candidates = @()
    if ($PreferredPath) { $candidates += @{Path=$PreferredPath; Args=@()} }
    foreach ($name in @('py','python','python3')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) {
            $arguments = if ($name -eq 'py') { @('-3') } else { @() }
            $candidates += @{Path=$command.Source; Args=$arguments}
        }
    }
    # Python未加入PATH时，尝试官方安装器登记的安装路径。
    foreach ($root in @('HKCU:\Software\Python\PythonCore','HKLM:\Software\Python\PythonCore','HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        foreach ($version in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
            $install = Get-Item -LiteralPath ($version.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
            if ($install) {
                $path = $install.GetValue('ExecutablePath')
                if (-not $path -and $install.GetValue('')) { $path = Join-Path $install.GetValue('') 'python.exe' }
                if ($path) { $candidates += @{Path=$path; Args=@()} }
            }
        }
    }
    $failures = @()
    foreach ($candidate in $candidates) {
        try {
            $arguments = $candidate.Args
            $output = & $candidate.Path @arguments -c $probe 2>$null
            $code = $LASTEXITCODE
            if ($code -eq 0 -and $output) {
                $path = ([string]($output | Select-Object -Last 1)).Trim()
                if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
            }
            $failures += "$($candidate.Path) (退出码=$code)"
        } catch {
            $failures += "$($candidate.Path) (调用失败)"
        }
    }
    throw ('未找到可运行的Python 3.10或更新版本。已尝试：' + ($failures -join '；'))
}

try {
    # 在提权之前定位当前用户的Python，再把完整路径传入管理员窗口。
    $python = Find-ExperimentPython $PythonPath
    if ($CheckPython) { Write-Output $python; exit 0 }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Role {1} -PythonPath "{2}"' -f $PSCommandPath, $Role, $python
        if ($Video) { $arguments += ' -Video' }
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments -Wait
        exit
    }
    Set-Location -LiteralPath $PSScriptRoot
    $ruleName = "1008CPE-$Role-UDP"
    $ports = if ($Role -eq 'send') { @(30002) } else { @(30002,30006) }
    # 只更新本工程规则，限制到实际Python可执行文件与实验UDP端口。
    if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -Name $ruleName
    }
    New-NetFirewallRule -Name $ruleName -DisplayName "1008CPE $Role UDP" `
        -Direction Inbound -Action Allow -Protocol UDP -LocalPort $ports `
        -Program $python -Profile Any | Out-Null
    Write-Host "使用Python：$python"
    # 默认启动文件循环下载；-Video保留原VLC实验入口。
    $entry = if ($Video) { "$Role/main.py" } else { "$Role/files.py" }
    & $python $entry
} catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    if ($CheckPython) { exit 1 }
}
Read-Host '按回车关闭窗口'
