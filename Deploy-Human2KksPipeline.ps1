param(
    [ValidateSet('source', 'zip', 'both')]
    [string]$Mode = 'both',

    [ValidateSet('test', 'production')]
    [string]$DeployProfile = 'test',

    [string]$ProjectRoot = 'F:\kks\work\tools\human_2_KKS_pipeline_clean_work',
    [string]$OutputDir = 'F:\kks\work\_tmp\human_2_KKS_pipeline_deploy',
    [string]$ZipName = '',

    [string]$SourceDeployRoot = 'F:\kks\work\_tmp\human_2_KKS_pipeline_release_work',
    [string]$SourceDeployTestRootBase = 'F:\kks\work\_tmp\human_2_KKS_pipeline_deploy\_source_deploy_test',

    # Set after creating the GitHub repository.
    [string]$ProductionRepoUrl = '',
    [string]$ExpectedProductionRemote = '',

    [switch]$GitCommitOnProduction,
    [switch]$GitPushOnProduction,
    [string]$GitCommitMessage = 'human_2_KKS_pipeline更新',
    [string]$GitBranch = '',

    # This script is source-only. Binary outputs are excluded.
    [string[]]$AlwaysExcludePatterns = @('*.dll'),

    [switch]$NoGitIgnore
)

$ErrorActionPreference = 'Stop'

function Assert-PathWithinRoot {
    param(
        [string]$PathValue,
        [string]$ExpectedRoot,
        [string]$Label
    )

    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        throw "$Label is empty."
    }

    $fullPath = [System.IO.Path]::GetFullPath($PathValue).TrimEnd('\', '/')
    $fullRoot = [System.IO.Path]::GetFullPath($ExpectedRoot).TrimEnd('\', '/')
    if ($fullPath.Equals($fullRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        return
    }

    $fullRootWithSep = $fullRoot + '\'
    if (-not $fullPath.StartsWith($fullRootWithSep, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ("{0} must be under {1} actual={2}" -f $Label, $fullRootWithSep, $fullPath)
    }
}

function Get-RelativePath {
    param(
        [string]$BasePath,
        [string]$TargetPath
    )

    $baseFull = [System.IO.Path]::GetFullPath($BasePath)
    $targetFull = [System.IO.Path]::GetFullPath($TargetPath)
    if (-not $baseFull.EndsWith('\')) {
        $baseFull += '\'
    }

    $baseUri = New-Object System.Uri($baseFull)
    $targetUri = New-Object System.Uri($targetFull)
    $relativeUri = $baseUri.MakeRelativeUri($targetUri)
    $relativePath = [System.Uri]::UnescapeDataString($relativeUri.ToString())
    return $relativePath.Replace('/', '\')
}

function Test-IgnoredByGit {
    param(
        [string]$RelativePath,
        [string]$RootDir
    )

    if ($NoGitIgnore) {
        return $false
    }

    if (-not (Test-Path -LiteralPath (Join-Path $RootDir '.git'))) {
        return $false
    }

    $null = & git -C $RootDir check-ignore --no-index -q -- $RelativePath 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Get-DeployFileList {
    param(
        [string]$RootDir
    )

    $files = New-Object System.Collections.Generic.List[object]
    $all = Get-ChildItem -Path $RootDir -Recurse -File -Force
    foreach ($file in $all) {
        $relative = (Get-RelativePath -BasePath $RootDir -TargetPath $file.FullName).Replace('\', '/')
        if ($relative.StartsWith('.git/')) {
            continue
        }

        $excluded = $false
        foreach ($pattern in $AlwaysExcludePatterns) {
            if ($file.Name -like $pattern -or $relative -like $pattern) {
                $excluded = $true
                break
            }
        }
        if ($excluded) {
            continue
        }

        if (Test-IgnoredByGit -RelativePath $relative -RootDir $RootDir) {
            continue
        }

        $files.Add([PSCustomObject]@{
                FullPath = $file.FullName
                RelativePath = $relative
            }) | Out-Null
    }

    return $files
}

function Sync-DeployFiles {
    param(
        [string]$FromRoot,
        [string]$ToRoot,
        [System.Collections.Generic.List[object]]$Files
    )

    New-Item -ItemType Directory -Path $ToRoot -Force | Out-Null

    $manifest = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($item in $Files) {
        $manifest.Add($item.RelativePath) | Out-Null
        $destPath = Join-Path $ToRoot ($item.RelativePath.Replace('/', '\'))
        $destDir = Split-Path -Parent $destPath
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null
        Copy-Item -LiteralPath $item.FullPath -Destination $destPath -Force
    }

    $existing = Get-ChildItem -Path $ToRoot -Recurse -File -Force
    foreach ($file in $existing) {
        $relative = (Get-RelativePath -BasePath $ToRoot -TargetPath $file.FullName).Replace('\', '/')
        if ($relative.StartsWith('.git/')) {
            continue
        }
        if (-not $manifest.Contains($relative)) {
            Remove-Item -LiteralPath $file.FullName -Force
        }
    }

    $dirs = Get-ChildItem -Path $ToRoot -Recurse -Directory -Force | Sort-Object FullName -Descending
    foreach ($dir in $dirs) {
        $relativeDir = (Get-RelativePath -BasePath $ToRoot -TargetPath $dir.FullName).Replace('\', '/')
        if ($relativeDir -eq '.git' -or $relativeDir.StartsWith('.git/')) {
            continue
        }
        if (-not (Get-ChildItem -Path $dir.FullName -Force | Select-Object -First 1)) {
            Remove-Item -LiteralPath $dir.FullName -Force
        }
    }
}

function Invoke-GitCommitUtf8Message {
    param(
        [string]$RepoPath,
        [string]$CommitMessage
    )

    if ([string]::IsNullOrWhiteSpace($CommitMessage)) {
        throw 'Commit message is empty.'
    }

    $commitMessagePath = Join-Path $RepoPath '.__tmp_commit_message_utf8.txt'
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)

    try {
        [System.IO.File]::WriteAllText($commitMessagePath, ($CommitMessage + [Environment]::NewLine), $utf8NoBom)
        $null = & git -C $RepoPath -c i18n.commitEncoding=utf-8 -c i18n.logOutputEncoding=utf-8 commit -F $commitMessagePath
        return $LASTEXITCODE
    }
    finally {
        if (Test-Path -LiteralPath $commitMessagePath) {
            Remove-Item -LiteralPath $commitMessagePath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Resolve-PushBranch {
    param(
        [string]$RepoPath,
        [string]$RequestedBranch
    )

    $branch = $RequestedBranch
    if ([string]::IsNullOrWhiteSpace($branch)) {
        $branch = (& git -C $RepoPath rev-parse --abbrev-ref HEAD 2>$null)
        if ($branch) {
            $branch = $branch.Trim()
        }
    }

    if ([string]::IsNullOrWhiteSpace($branch) -or $branch -eq 'HEAD') {
        throw "Could not resolve push branch for $RepoPath"
    }

    return $branch
}

function Ensure-ProductionRepo {
    param(
        [string]$RepoPath,
        [string]$RepoUrl,
        [string]$ExpectedRemoteToken
    )

    if (-not (Test-Path -LiteralPath $RepoPath)) {
        if ([string]::IsNullOrWhiteSpace($RepoUrl)) {
            throw "Production repo not found at $RepoPath. Set -ProductionRepoUrl after creating repository."
        }
        $parentDir = Split-Path -Parent $RepoPath
        if ([string]::IsNullOrWhiteSpace($parentDir) -or -not (Test-Path -LiteralPath $parentDir)) {
            throw "Production repo parent not found: $parentDir"
        }

        Write-Output ('Production repo not found. Cloning: {0} -> {1}' -f $RepoUrl, $RepoPath)
        & git clone $RepoUrl $RepoPath
        if ($LASTEXITCODE -ne 0) {
            throw "git clone failed: $RepoUrl -> $RepoPath"
        }
    }

    if (-not (Test-Path -LiteralPath (Join-Path $RepoPath '.git'))) {
        throw "Production source deploy root is not a git repository: $RepoPath"
    }

    if (-not [string]::IsNullOrWhiteSpace($ExpectedRemoteToken)) {
        $originUrl = (& git -C $RepoPath remote get-url origin 2>$null)
        if (-not $originUrl) {
            throw "origin remote not found in $RepoPath"
        }
        if ($originUrl -notmatch [Regex]::Escape($ExpectedRemoteToken)) {
            throw "origin remote mismatch. origin=$originUrl expected~$ExpectedRemoteToken"
        }
    }
}

$canonicalWorkRoot = 'F:\kks\work'
Assert-PathWithinRoot -PathValue $ProjectRoot -ExpectedRoot $canonicalWorkRoot -Label 'ProjectRoot'
Assert-PathWithinRoot -PathValue $OutputDir -ExpectedRoot $canonicalWorkRoot -Label 'OutputDir'
Assert-PathWithinRoot -PathValue $SourceDeployTestRootBase -ExpectedRoot $canonicalWorkRoot -Label 'SourceDeployTestRootBase'
Assert-PathWithinRoot -PathValue $SourceDeployRoot -ExpectedRoot $canonicalWorkRoot -Label 'SourceDeployRoot'

if ($DeployProfile -eq 'test' -and ($GitCommitOnProduction -or $GitPushOnProduction)) {
    throw 'GitCommitOnProduction / GitPushOnProduction cannot be used with test profile.'
}

if ([string]::IsNullOrWhiteSpace($ZipName)) {
    $ZipName = ('human_2_KKS_pipeline_{0}.zip' -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

$runStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$sourceFiles = Get-DeployFileList -RootDir $ProjectRoot
if ($sourceFiles.Count -le 0) {
    throw 'No files included. Check .gitignore and project path.'
}

$effectiveSourceDeployRoot = $null
$zipPath = $null
$zipIncludedCount = 0
$sourceIncludedCount = 0

if ($Mode -eq 'source' -or $Mode -eq 'both') {
    if ($DeployProfile -eq 'production') {
        Ensure-ProductionRepo `
            -RepoPath $SourceDeployRoot `
            -RepoUrl $ProductionRepoUrl `
            -ExpectedRemoteToken $ExpectedProductionRemote
        $effectiveSourceDeployRoot = $SourceDeployRoot
    }
    else {
        $effectiveSourceDeployRoot = Join-Path $SourceDeployTestRootBase ('run_{0}' -f $runStamp)
    }

    Sync-DeployFiles -FromRoot $ProjectRoot -ToRoot $effectiveSourceDeployRoot -Files $sourceFiles
    $sourceIncludedCount = $sourceFiles.Count

    if ($DeployProfile -eq 'production' -and ($GitCommitOnProduction -or $GitPushOnProduction)) {
        & git -C $effectiveSourceDeployRoot config user.name canon64
        & git -C $effectiveSourceDeployRoot config user.email canon64@users.noreply.github.com

        & git -C $effectiveSourceDeployRoot add .
        $statusAfterAdd = & git -C $effectiveSourceDeployRoot status --porcelain
        if ($statusAfterAdd) {
            $commitExitCode = Invoke-GitCommitUtf8Message -RepoPath $effectiveSourceDeployRoot -CommitMessage $GitCommitMessage
            if ($commitExitCode -ne 0) {
                throw 'Failed to commit source deploy changes.'
            }
        }
        else {
            Write-Output 'Production source deploy: no changes to commit.'
        }

        if ($GitPushOnProduction) {
            $pushBranch = Resolve-PushBranch -RepoPath $effectiveSourceDeployRoot -RequestedBranch $GitBranch
            & git -C $effectiveSourceDeployRoot push origin $pushBranch
            if ($LASTEXITCODE -ne 0) {
                throw "Production source deploy: failed to push branch $pushBranch"
            }
        }
    }
}

if ($Mode -eq 'zip' -or $Mode -eq 'both') {
    $stagingRoot = Join-Path $OutputDir ('_staging_h2kks_{0}' -f $runStamp)
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null

    $zipRoot = Join-Path $stagingRoot 'human_2_KKS_pipeline'
    New-Item -ItemType Directory -Path $zipRoot -Force | Out-Null

    foreach ($item in $sourceFiles) {
        $destPath = Join-Path $zipRoot ($item.RelativePath.Replace('/', '\'))
        $destDir = Split-Path -Parent $destPath
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null
        Copy-Item -LiteralPath $item.FullPath -Destination $destPath -Force
    }

    $zipPath = Join-Path $OutputDir $ZipName
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }

    Compress-Archive -Path $zipRoot -DestinationPath $zipPath -CompressionLevel Optimal
    $zipIncludedCount = $sourceFiles.Count
}

Write-Output ('Mode: {0}' -f $Mode)
Write-Output ('DeployProfile: {0}' -f $DeployProfile)
Write-Output ('ProjectRoot: {0}' -f $ProjectRoot)

if ($effectiveSourceDeployRoot) {
    Write-Output ('Source deploy root: {0}' -f $effectiveSourceDeployRoot)
    Write-Output ('Included source files: {0}' -f $sourceIncludedCount)
    if (Test-Path -LiteralPath (Join-Path $effectiveSourceDeployRoot '.git')) {
        Write-Output 'Git status (source deploy root):'
        & git -C $effectiveSourceDeployRoot status --short
    }
}

if ($zipPath) {
    Write-Output ('Zip created: {0}' -f $zipPath)
    Write-Output ('Included zip files: {0}' -f $zipIncludedCount)
}
