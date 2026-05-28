$ErrorActionPreference = "Stop"

$Repo = "suyuwithoutPainkillers/ChatScreenExporter"
$Root = Split-Path -Parent $PSScriptRoot

$authStatus = gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "GitHub CLI has not logged in yet. Run: gh auth login" -ForegroundColor Yellow
    Write-Host $authStatus
    exit 1
}

$releases = @(
    @{ Tag = "v1.0"; File = "ChatScreenExporter_V1.0_基础版.exe"; Title = "V1.0 基础版"; Notes = "基础可运行版本。" },
    @{ Tag = "v1.1"; File = "ChatScreenExporter_V1.1_跳过照片.exe"; Title = "V1.1 跳过照片"; Notes = "遇到照片消息时跳过，不写入 TXT。" },
    @{ Tag = "v1.2"; File = "ChatScreenExporter_V1.2_AI流水线加速.exe"; Title = "V1.2 AI流水线加速"; Notes = "加入 AI 流水线加速，提升截图识别吞吐。" },
    @{ Tag = "v1.3"; File = "ChatScreenExporter_V1.3_防漏加速.exe"; Title = "V1.3 防漏加速"; Notes = "优化防漏重叠与扫描速度。" },
    @{ Tag = "v1.4"; File = "ChatScreenExporter_V1.4_批量AI加速.exe"; Title = "V1.4 批量AI加速"; Notes = "批量 AI 识别加速版本。" },
    @{ Tag = "v1.5"; File = "ChatScreenExporter_V1.5_停止保存加速.exe"; Title = "V1.5 停止保存加速"; Notes = "优化停止并保存流程。" },
    @{ Tag = "v1.6"; File = "ChatScreenExporter_V1.6_版本号显示.exe"; Title = "V1.6 版本号显示"; Notes = "界面中显示版本号，便于区分构建。" },
    @{ Tag = "v1.7"; File = "ChatScreenExporter_V1.7_安全滚动不点击.exe"; Title = "V1.7 安全滚动不点击"; Notes = "尝试降低滚动时误点击风险。" },
    @{ Tag = "v1.8"; File = "ChatScreenExporter_V1.8_无点击整屏翻页.exe"; Title = "V1.8 无点击整屏翻页"; Notes = "尝试无点击整屏翻页。" },
    @{ Tag = "v1.9"; File = "ChatScreenExporter_V1.9_V1.4速度无点击翻页.exe"; Title = "V1.9 V1.4速度无点击翻页"; Notes = "在无点击思路下接近 V1.4 的翻页速度。" },
    @{ Tag = "v2.0"; File = "ChatScreenExporter_V2.0_回到V1.4翻页速度.exe"; Title = "V2.0 回到V1.4翻页速度"; Notes = "恢复 V1.4 风格的高速整屏翻页。" },
    @{ Tag = "v2.1"; File = "ChatScreenExporter_V2.1_异常容错增强.exe"; Title = "V2.1 异常容错增强"; Notes = "增强 AI 异常容错，避免少量超时直接中断。" },
    @{ Tag = "v2.2"; File = "ChatScreenExporter_V2.2_Stitch现代界面.exe"; Title = "V2.2 Stitch现代界面"; Notes = "接入 Stitch 设计后的现代化界面。" },
    @{ Tag = "v2.3"; File = "ChatScreenExporter_V2.3_输入框修复.exe"; Title = "V2.3 输入框修复"; Notes = "修复新版界面输入框无法正常输入的问题。" },
    @{ Tag = "v2.4"; File = "ChatScreenExporter_V2.4_明显开始按钮无黑框.exe"; Title = "V2.4 明显开始按钮无黑框"; Notes = "增加醒目的开始导出按钮，并使用无控制台窗口模式打包。" },
    @{ Tag = "v2.4.1"; File = "ChatScreenExporter_V2.4.1_边界停止提醒.exe"; Title = "V2.4.1 边界停止提醒"; Notes = "运行窗口增加边界停止提醒，鼠标移到屏幕边界即可停止并保存。" },
    @{ Tag = "v2.4.2"; File = "ChatScreenExporter_V2.4.2_软件图标.exe"; Title = "V2.4.2 软件图标"; Notes = "加入软件图标并嵌入 exe。" },
    @{ Tag = "v2.4.3"; File = "ChatScreenExporter_V2.4.3_侧边栏功能.exe"; Title = "V2.4.3 侧边栏功能"; Notes = "完善侧边栏页面：导出设置、聊天记录、文件格式、偏好设置。" },
    @{ Tag = "v2.4.4"; File = "ChatScreenExporter_V2.4.4_作者署名.exe"; Title = "V2.4.4 作者署名"; Notes = "在界面左下角添加作者署名：SU。" },
    @{ Tag = "v2.4.5"; File = "ChatScreenExporter_V2.4.5_替换图标_最新版.exe"; Title = "V2.4.5 替换图标"; Notes = "替换为新的 Stitch 图标，当前推荐下载版本。"; Latest = $true }
)

foreach ($release in $releases) {
    $assetPath = Join-Path $Root $release.File
    if (!(Test-Path -LiteralPath $assetPath)) {
        Write-Host "Missing asset: $($release.File)" -ForegroundColor Red
        exit 1
    }

    gh release view $release.Tag --repo $Repo *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Skip existing release $($release.Tag)" -ForegroundColor DarkYellow
        continue
    }

    Write-Host "Creating release $($release.Tag) -> $($release.File)" -ForegroundColor Cyan
    $notes = @"
$($release.Notes)

下载文件：$($release.File)
"@
    $args = @(
        "release", "create", $release.Tag,
        "--repo", $Repo,
        "--target", "main",
        "--title", $release.Title,
        "--notes", $notes
    )
    if ($release.Latest) {
        $args += "--latest"
    } else {
        $args += "--latest=false"
    }
    $args += $assetPath
    & gh @args
}

Write-Host "All requested releases have been processed." -ForegroundColor Green
