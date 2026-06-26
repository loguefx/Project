<#
.SYNOPSIS
  Cut a new ShowTVDownloader release: bump version, build the .exe, zip it, and
  publish a GitHub Release with the build attached. The deployed service's
  in-app updater finds this release and offers the update.

.EXAMPLE
  $env:GITHUB_TOKEN = "ghp_xxx"        # a PAT with 'repo' scope
  .\release.ps1 -Version 1.1.0 -Notes "Adds X, fixes Y"

.NOTES
  Requires git, a clean-ish working tree, and a GitHub token (param or env var).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$Version,
    [string]$Notes = "",
    [string]$Token = $env:GITHUB_TOKEN,
    [string]$Owner = "loguefx",
    [string]$Repo  = "Project",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

if (-not $Token) { throw "No GitHub token. Pass -Token or set `$env:GITHUB_TOKEN (PAT with 'repo' scope)." }
if ($Version -notmatch '^\d+\.\d+\.\d+') { throw "Version must look like 1.2.3" }
$tag = "v$Version"

Write-Host "==> Bumping version.py to $Version"
$verFile = Join-Path $root "version.py"
$content = Get-Content $verFile -Raw
$content = [regex]::Replace($content, '__version__\s*=\s*".*?"', "__version__ = `"$Version`"")
Set-Content $verFile $content -NoNewline -Encoding utf8

Write-Host "==> Committing + pushing version bump"
git add version.py
git commit -m "Release $tag" | Out-Null
git push origin $Branch

Write-Host "==> Building..."
& (Join-Path $root "build.ps1") -Clean
if ($LASTEXITCODE -ne 0) { throw "Build failed." }

$buildDir = Join-Path $root "dist\ShowTVDownloader"
$zipName  = "ShowTVDownloader-$tag.zip"
$zipPath  = Join-Path $root "dist\$zipName"
Write-Host "==> Zipping build -> $zipName"
Remove-Item $zipPath -ErrorAction SilentlyContinue
# Include the top-level ShowTVDownloader\ folder so the updater can locate the exe.
Compress-Archive -Path $buildDir -DestinationPath $zipPath -Force

$headers = @{
    Authorization = "Bearer $Token"
    Accept        = "application/vnd.github+json"
    "User-Agent"  = "ShowTVDownloader-Release"
}

Write-Host "==> Creating GitHub release $tag"
$body = @{
    tag_name         = $tag
    target_commitish = $Branch
    name             = $tag
    body             = $Notes
    draft            = $false
    prerelease       = $false
} | ConvertTo-Json

$rel = Invoke-RestMethod -Method Post -Headers $headers `
    -Uri "https://api.github.com/repos/$Owner/$Repo/releases" -Body $body
Write-Host "    release id = $($rel.id)"

Write-Host "==> Uploading asset $zipName"
$uploadUrl = "https://uploads.github.com/repos/$Owner/$Repo/releases/$($rel.id)/assets?name=$zipName"
$uploadHeaders = @{
    Authorization  = "Bearer $Token"
    "Content-Type" = "application/zip"
    "User-Agent"   = "ShowTVDownloader-Release"
}
Invoke-RestMethod -Method Post -Headers $uploadHeaders -Uri $uploadUrl -InFile $zipPath | Out-Null

Write-Host ""
Write-Host "==> Released $tag" -ForegroundColor Green
Write-Host "    $($rel.html_url)"
Write-Host "    Deployed services will now see this version under Settings -> Software Updates."
