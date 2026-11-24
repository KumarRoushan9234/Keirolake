# Publish a KeiroLake release to GitHub from your own account. Run it yourself; nothing here runs automatically.
#
#   powershell -ExecutionPolicy Bypass -File desktop\scripts\publish_release.ps1 -Repo KumarRoushan9234/Keirolake -Version 0.7.3
#
# Needs: the GitHub CLI (gh) signed in as the account that owns -Repo, the repository already created with the code
# pushed, and dist\release-<version>\ made by make_release.py. Add -Draft to create the release as a draft first.
param(
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$Version,
    [switch]$Draft
)
$ErrorActionPreference = "Stop"
$dir = Join-Path $PSScriptRoot "..\..\dist\release-$Version"
if (-not (Test-Path $dir)) { throw "Run: python desktop\scripts\make_release.py $Version" }

$account = (gh api user --jq .login)
$owner = $Repo.Split("/")[0]
if ($account -ne $owner) { throw "gh is signed in as '$account', not '$owner'. Run: gh auth login (or gh auth switch)" }
gh repo view $Repo --json name | Out-Null

# Verify every file against SHA256SUMS before uploading.
Get-Content (Join-Path $dir "SHA256SUMS") | ForEach-Object {
    $hash, $name = $_ -split "\s+", 2
    $actual = (Get-FileHash (Join-Path $dir $name) -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $hash) { throw "Checksum mismatch for $name" }
}

$files = Get-ChildItem $dir -File | Where-Object { $_.Name -ne "RELEASE_NOTES.md" } | ForEach-Object { $_.FullName }
$ghArgs = @("release", "create", "v$Version", "--repo", $Repo, "--title", "KeiroLake $Version", "--notes-file", (Join-Path $dir "RELEASE_NOTES.md"))
if ($Draft) { $ghArgs += "--draft" }
gh @ghArgs @files
Write-Host "Published: https://github.com/$Repo/releases/tag/v$Version"
