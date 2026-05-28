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
    @{ Tag = "v1.0"; Pattern = "ChatScreenExporter_V1.0_*.exe"; Title = "V1.0 Basic release"; Notes = "Initial runnable build." },
    @{ Tag = "v1.1"; Pattern = "ChatScreenExporter_V1.1_*.exe"; Title = "V1.1 Skip images"; Notes = "Skips photo/image messages when exporting text." },
    @{ Tag = "v1.2"; Pattern = "ChatScreenExporter_V1.2_*.exe"; Title = "V1.2 AI pipeline speedup"; Notes = "Adds AI pipeline acceleration." },
    @{ Tag = "v1.3"; Pattern = "ChatScreenExporter_V1.3_*.exe"; Title = "V1.3 Overlap speedup"; Notes = "Improves overlap handling and scanning speed." },
    @{ Tag = "v1.4"; Pattern = "ChatScreenExporter_V1.4_*.exe"; Title = "V1.4 Batch AI speedup"; Notes = "Adds batch AI recognition acceleration." },
    @{ Tag = "v1.5"; Pattern = "ChatScreenExporter_V1.5_*.exe"; Title = "V1.5 Faster stop and save"; Notes = "Optimizes stopping while keeping recognized content." },
    @{ Tag = "v1.6"; Pattern = "ChatScreenExporter_V1.6_*.exe"; Title = "V1.6 Version display"; Notes = "Shows version numbers in the app." },
    @{ Tag = "v1.7"; Pattern = "ChatScreenExporter_V1.7_*.exe"; Title = "V1.7 Safer scrolling"; Notes = "Reduces accidental clicks while scrolling." },
    @{ Tag = "v1.8"; Pattern = "ChatScreenExporter_V1.8_*.exe"; Title = "V1.8 No-click page scroll"; Notes = "Experiments with no-click full-page scrolling." },
    @{ Tag = "v1.9"; Pattern = "ChatScreenExporter_V1.9_*.exe"; Title = "V1.9 V1.4 speed no-click scroll"; Notes = "Brings no-click scrolling closer to V1.4 speed." },
    @{ Tag = "v2.0"; Pattern = "ChatScreenExporter_V2.0_*.exe"; Title = "V2.0 Restore V1.4 page speed"; Notes = "Restores fast full-page scrolling behavior." },
    @{ Tag = "v2.1"; Pattern = "ChatScreenExporter_V2.1_*.exe"; Title = "V2.1 Error tolerance"; Notes = "Improves AI timeout and recognition error tolerance." },
    @{ Tag = "v2.2"; Pattern = "ChatScreenExporter_V2.2_*.exe"; Title = "V2.2 Modern Stitch UI"; Notes = "Adds the modern UI based on Stitch designs." },
    @{ Tag = "v2.3"; Pattern = "ChatScreenExporter_V2.3_*.exe"; Title = "V2.3 Input field fix"; Notes = "Fixes text input fields in the modern UI." },
    @{ Tag = "v2.4"; Pattern = "ChatScreenExporter_V2.4_*.exe"; Title = "V2.4 Clear start button and no console"; Notes = "Adds clearer start buttons and removes the console window." },
    @{ Tag = "v2.4.1"; Pattern = "ChatScreenExporter_V2.4.1_*.exe"; Title = "V2.4.1 Edge stop reminder"; Notes = "Adds screen-edge stop reminder and behavior." },
    @{ Tag = "v2.4.2"; Pattern = "ChatScreenExporter_V2.4.2_*.exe"; Title = "V2.4.2 App icon"; Notes = "Embeds an application icon." },
    @{ Tag = "v2.4.3"; Pattern = "ChatScreenExporter_V2.4.3_*.exe"; Title = "V2.4.3 Sidebar pages"; Notes = "Completes sidebar pages for settings, history, format, and preferences." },
    @{ Tag = "v2.4.4"; Pattern = "ChatScreenExporter_V2.4.4_*.exe"; Title = "V2.4.4 Author credit"; Notes = "Adds author credit: SU." },
    @{ Tag = "v2.4.5"; Pattern = "ChatScreenExporter_V2.4.5_*.exe"; Title = "V2.4.5 Updated icon"; Notes = "Updates the app icon. Recommended latest download."; Latest = $true }
)

foreach ($release in $releases) {
    $matches = @(Get-ChildItem -Path $Root -Filter $release.Pattern -File | Sort-Object Name)
    if ($matches.Count -ne 1) {
        Write-Host "Expected exactly one asset for pattern $($release.Pattern), found $($matches.Count)." -ForegroundColor Red
        $matches | ForEach-Object { Write-Host " - $($_.Name)" }
        exit 1
    }

    $assetPath = $matches[0].FullName
    $assetName = $matches[0].Name

    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    gh release view $release.Tag --repo $Repo *> $null
    $viewExitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldErrorActionPreference
    if ($viewExitCode -eq 0) {
        Write-Host "Skip existing release $($release.Tag)" -ForegroundColor DarkYellow
        continue
    }

    Write-Host "Creating release $($release.Tag) -> $assetName" -ForegroundColor Cyan
    $notes = @"
$($release.Notes)

Asset: $assetName
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
