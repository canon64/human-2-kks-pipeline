param(
    [ValidateSet("test", "apply")]
    [string]$Mode = "test",
    [string]$RepoPath = (Split-Path -Parent $PSCommandPath),
    [string]$Branch = "main",
    [switch]$GitCommitOnApply,
    [switch]$GitPushOnApply,
    [string]$CommitMessage = "配布反映"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Args
    )

    $output = & git -C $RepoPath @Args 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Args -join ' ') failed.`n$output"
    }
    return ($output -join "`n").Trim()
}

if (-not (Test-Path -LiteralPath $RepoPath)) {
    throw "RepoPath not found: $RepoPath"
}
if (-not (Test-Path -LiteralPath (Join-Path $RepoPath ".git"))) {
    throw "Not a git repository: $RepoPath"
}
if ($Branch -eq "master") {
    throw "Branch=master is blocked. Use main."
}

$status = Invoke-Git -Args @("status", "--short")
$currentBranch = Invoke-Git -Args @("branch", "--show-current")
$headInfo = Invoke-Git -Args @("rev-parse", "--short", "HEAD")

Write-Host "[deploy] mode=$Mode"
Write-Host "[deploy] repo=$RepoPath"
Write-Host "[deploy] current_branch=$currentBranch"
Write-Host "[deploy] head=$headInfo"
if ([string]::IsNullOrWhiteSpace($status)) {
    Write-Host "[deploy] working_tree=clean"
} else {
    Write-Host "[deploy] working_tree=dirty"
    Write-Host $status
}

if ($Mode -eq "test") {
    Write-Host "[deploy] test mode completed (no changes applied)."
    exit 0
}

Invoke-Git -Args @("config", "user.name", "canon64") | Out-Null
Invoke-Git -Args @("config", "user.email", "canon64@users.noreply.github.com") | Out-Null

if ($currentBranch -ne $Branch) {
    Invoke-Git -Args @("checkout", $Branch) | Out-Null
    $currentBranch = Invoke-Git -Args @("branch", "--show-current")
    Write-Host "[deploy] switched_branch=$currentBranch"
}

if ($GitCommitOnApply) {
    Invoke-Git -Args @("add", ".") | Out-Null
    $staged = Invoke-Git -Args @("diff", "--cached", "--name-only")
    if ([string]::IsNullOrWhiteSpace($staged)) {
        Write-Host "[deploy] no staged changes, commit skipped."
    } else {
        Invoke-Git -Args @("commit", "-m", $CommitMessage) | Out-Null
        Write-Host "[deploy] commit_done"
    }
}

if ($GitPushOnApply) {
    Invoke-Git -Args @("push", "origin", "$Branch`:$Branch") | Out-Null
    Write-Host "[deploy] push_done origin/$Branch"
} else {
    Write-Host "[deploy] push skipped (use -GitPushOnApply)."
}

Write-Host "[deploy] apply completed."
