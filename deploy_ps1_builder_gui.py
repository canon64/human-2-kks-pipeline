from __future__ import annotations

import re
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


@dataclass(frozen=True)
class TemplatePreset:
    key: str
    label: str
    source_required: dict[str, list[str]]
    release_required: dict[str, list[str]]
    release_extra_patterns: list[str]
    include_git_snapshot: bool = True


PRESETS: dict[str, TemplatePreset] = {
    "python": TemplatePreset(
        key="python",
        label="Python",
        source_required={
            ".": ["README.md", "main.py", "launch.bat", "setup.bat", "requirements.txt"],
        },
        release_required={
            ".": ["README.md", "main.py", "launch.bat", "setup.bat", "requirements.txt"],
        },
        release_extra_patterns=[],
        include_git_snapshot=True,
    ),
    "csharp_dll": TemplatePreset(
        key="csharp_dll",
        label="C# DLL",
        source_required={
            ".": ["README.md", "*.csproj"],
        },
        release_required={
            ".": ["README.md"],
            "bin/Release": ["*.dll"],
        },
        release_extra_patterns=["bin/Release/*.dll"],
        include_git_snapshot=True,
    ),
    "html": TemplatePreset(
        key="html",
        label="HTML",
        source_required={
            ".": ["README.md", "index.html"],
        },
        release_required={
            ".": ["README.md", "index.html"],
        },
        release_extra_patterns=[],
        include_git_snapshot=True,
    ),
    "jar": TemplatePreset(
        key="jar",
        label="JAR",
        source_required={
            ".": ["README.md"],
        },
        release_required={
            ".": ["README.md"],
            "target": ["*.jar"],
        },
        release_extra_patterns=["target/*.jar"],
        include_git_snapshot=True,
    ),
}


def _slugify(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "project"


def _repo_name_from_url(url: str) -> str:
    cleaned = (url or "").strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    if "/" not in cleaned:
        return "project"
    return _slugify(cleaned.rsplit("/", 1)[-1])


def _repo_full_name_from_url(url: str) -> str:
    cleaned = (url or "").strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    # https://github.com/owner/repo or git@github.com:owner/repo
    m = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+)$", cleaned, re.IGNORECASE)
    if not m:
        return ""
    owner = m.group("owner").strip()
    repo = m.group("repo").strip()
    return f"{owner}/{repo}"


def _map_to_text(data: dict[str, list[str]]) -> str:
    rows: list[str] = []
    for folder, patterns in data.items():
        for pattern in patterns:
            rows.append(f"{folder}|{pattern}")
    return "\n".join(rows)


def _text_to_map(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for raw in (text or "").splitlines():
        row = raw.strip()
        if not row or row.startswith("#"):
            continue
        if "|" not in row:
            raise ValueError(f"無効な行です（folder|pattern 形式）: {row}")
        folder, pattern = row.split("|", 1)
        folder = folder.strip() or "."
        pattern = pattern.strip()
        if not pattern:
            raise ValueError(f"patternが空です: {row}")
        result.setdefault(folder, []).append(pattern)
    return result


def _text_to_list(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip() and not line.strip().startswith("#")]


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ps_array(values: list[str], indent: int = 0) -> str:
    pad = " " * indent
    inner = ", ".join(_ps_quote(v) for v in values)
    return f"{pad}@({inner})"


def _ps_required_map(name: str, mapping: dict[str, list[str]]) -> str:
    lines = [f"${name} = @{{"]
    for folder in sorted(mapping.keys(), key=lambda x: (x != ".", x.lower())):
        patterns = mapping[folder]
        arr = _ps_array(patterns)
        lines.append(f"  {_ps_quote(folder)} = {arr}")
    lines.append("}")
    return "\n".join(lines)


def _build_ps1_text(
    *,
    project_name: str,
    repo_url: str,
    repo_full_name: str,
    work_dir: str,
    branch: str,
    source_required: dict[str, list[str]],
    release_required: dict[str, list[str]],
    release_extra_patterns: list[str],
    include_git_snapshot: bool,
    artifact_type: str,
) -> str:
    source_block = _ps_required_map("SourceRequiredByFolder", source_required)
    release_block = _ps_required_map("ReleaseRequiredByFolder", release_required)
    extra_block = _ps_array(release_extra_patterns)

    return f"""param(
    [ValidateSet('test','production')]
    [string]$DeployProfile = 'test',

    [ValidateSet('source','release','both')]
    [string]$Mode = 'both',

    [string]$Version = '',

    [switch]$GitCommitOnProduction,
    [switch]$GitPushOnProduction
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectName = {_ps_quote(project_name)}
$RepoUrl = {_ps_quote(repo_url)}
$RepoFullName = {_ps_quote(repo_full_name)}
$WorkDir = {_ps_quote(work_dir)}
$DefaultBranch = {_ps_quote(branch)}
$ArtifactType = {_ps_quote(artifact_type)}
$IncludeGitSnapshotInRelease = {'$true' if include_git_snapshot else '$false'}
{source_block}
{release_block}
$ReleaseExtraIncludePatterns = {extra_block}

function Write-Info([string]$msg) {{
    Write-Host "[deploy] $msg"
}}

function Require-Command([string]$name) {{
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {{
        throw "Required command not found: $name"
    }}
}}

function Normalize-Version([string]$value, [string]$profile) {{
    $v = ($value ?? '').Trim()
    if ([string]::IsNullOrWhiteSpace($v)) {{
        if ($profile -eq 'production') {{
            throw "Version is required in production mode."
        }}
        return "test-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
    }}
    if ($v.StartsWith('v')) {{
        return $v
    }}
    return "v" + $v
}}

function Resolve-Abs([string]$base, [string]$path) {{
    if ([string]::IsNullOrWhiteSpace($path)) {{
        return (Resolve-Path -LiteralPath $base).Path
    }}
    if ([System.IO.Path]::IsPathRooted($path)) {{
        return (Resolve-Path -LiteralPath $path).Path
    }}
    return (Resolve-Path -LiteralPath (Join-Path $base $path)).Path
}}

function Find-MatchingFiles([string]$targetDir, [string]$pattern) {{
    if (-not (Test-Path -LiteralPath $targetDir)) {{
        return @()
    }}
    if ($pattern -match '[\\*\\?\\[]') {{
        return @(Get-ChildItem -Path (Join-Path $targetDir $pattern) -File -Recurse -ErrorAction SilentlyContinue)
    }}
    $literal = Join-Path $targetDir $pattern
    if (Test-Path -LiteralPath $literal) {{
        $item = Get-Item -LiteralPath $literal -ErrorAction Stop
        if ($item.PSIsContainer) {{
            return @(Get-ChildItem -LiteralPath $literal -File -Recurse -ErrorAction SilentlyContinue)
        }}
        return @($item)
    }}
    return @()
}}

function Assert-RequiredMap([string]$baseDir, [hashtable]$requiredMap, [string]$scope) {{
    $missing = New-Object System.Collections.Generic.List[string]
    foreach ($folder in $requiredMap.Keys) {{
        $targetDir = if ($folder -eq '.' -or [string]::IsNullOrWhiteSpace($folder)) {{ $baseDir }} else {{ Join-Path $baseDir $folder }}
        foreach ($pattern in $requiredMap[$folder]) {{
            $matches = Find-MatchingFiles -targetDir $targetDir -pattern $pattern
            if ($matches.Count -eq 0) {{
                $missing.Add("$folder|$pattern")
            }}
        }}
    }}
    if ($missing.Count -gt 0) {{
        throw "Missing required files for $scope: " + ($missing -join ', ')
    }}
}}

function Ensure-ReadmeExists([string]$baseDir) {{
    $readmePath = Join-Path $baseDir 'README.md'
    if (-not (Test-Path -LiteralPath $readmePath)) {{
        throw "README.md is required at repository root for source page."
    }}
}}

function Invoke-SourceDeploy() {{
    Ensure-ReadmeExists -baseDir $WorkDir
    Assert-RequiredMap -baseDir $WorkDir -requiredMap $SourceRequiredByFolder -scope 'source'

    if ($DeployProfile -eq 'test') {{
        Write-Info "source deploy test passed (no git push)."
        return
    }}

    Push-Location $WorkDir
    try {{
        if ($GitCommitOnProduction) {{
            git add .
            $changes = git status --porcelain
            if (-not [string]::IsNullOrWhiteSpace(($changes -join ''))) {{
                $commitVersion = Normalize-Version -value $Version -profile $DeployProfile
                git commit -m ("deploy " + $commitVersion)
                Write-Info "git commit created."
            }} else {{
                Write-Info "no changes to commit."
            }}
        }}

        if ($GitPushOnProduction) {{
            git push origin $DefaultBranch
            Write-Info "git push completed."
        }} else {{
            Write-Info "GitPushOnProduction is off. skip push."
        }}
    }}
    finally {{
        Pop-Location
    }}
}}

function Copy-ExtraArtifacts([string]$baseDir, [string]$packageRoot, [string[]]$patterns) {{
    foreach ($pattern in $patterns) {{
        $full = Join-Path $baseDir $pattern
        $items = @(Get-ChildItem -Path $full -File -Recurse -ErrorAction SilentlyContinue)
        foreach ($item in $items) {{
            $relative = $item.FullName.Substring($baseDir.Length).TrimStart('\\','/')
            $dest = Join-Path $packageRoot $relative
            $destDir = Split-Path -Parent $dest
            New-Item -ItemType Directory -Path $destDir -Force | Out-Null
            Copy-Item -LiteralPath $item.FullName -Destination $dest -Force
        }}
    }}
}}

function New-ReleaseZip() {{
    Ensure-ReadmeExists -baseDir $WorkDir
    Assert-RequiredMap -baseDir $WorkDir -requiredMap $ReleaseRequiredByFolder -scope 'release'

    $tag = Normalize-Version -value $Version -profile $DeployProfile
    $versionText = if ($tag.StartsWith('v')) {{ $tag.Substring(1) }} else {{ $tag }}
    $packageFolderName = "$ProjectName-v$versionText"

    $artifactsDir = Join-Path $WorkDir 'release_artifacts'
    New-Item -ItemType Directory -Path $artifactsDir -Force | Out-Null
    $zipPath = Join-Path $artifactsDir ($packageFolderName + '.zip')
    if (Test-Path -LiteralPath $zipPath) {{
        Remove-Item -LiteralPath $zipPath -Force
    }}

    $stage = Join-Path ([System.IO.Path]::GetTempPath()) ('deploy_stage_' + [System.Guid]::NewGuid().ToString('N'))
    $packageRoot = Join-Path $stage $packageFolderName
    New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null

    try {{
        if ($IncludeGitSnapshotInRelease) {{
            $archiveZip = Join-Path $stage '_tracked.zip'
            git -C $WorkDir archive --format=zip --prefix="$packageFolderName/" --output="$archiveZip" HEAD
            Expand-Archive -Path $archiveZip -DestinationPath $stage -Force
            Remove-Item -LiteralPath $archiveZip -Force
        }}

        Copy-ExtraArtifacts -baseDir $WorkDir -packageRoot $packageRoot -patterns $ReleaseExtraIncludePatterns
        Compress-Archive -Path $packageRoot -DestinationPath $zipPath -Force
    }}
    finally {{
        if (Test-Path -LiteralPath $stage) {{
            Remove-Item -LiteralPath $stage -Recurse -Force
        }}
    }}

    return @{{ Tag = $tag; ZipPath = $zipPath; VersionText = $versionText }}
}}

function Publish-Release([string]$tag, [string]$zipPath) {{
    if ([string]::IsNullOrWhiteSpace($RepoFullName)) {{
        throw "Repo full name cannot be resolved from URL. Use github.com/<owner>/<repo> URL."
    }}
    if ($DeployProfile -eq 'test') {{
        Write-Info ("release test passed. zip created: " + $zipPath)
        return
    }}

    $assetName = [System.IO.Path]::GetFileName($zipPath)
    gh release view $tag -R $RepoFullName *> $null
    if ($LASTEXITCODE -eq 0) {{
        gh release edit $tag -R $RepoFullName --target $DefaultBranch *> $null
        gh release delete-asset $tag $assetName -R $RepoFullName -y *> $null
        gh release upload $tag $zipPath -R $RepoFullName
        Write-Info "release updated."
    }}
    else {{
        gh release create $tag $zipPath -R $RepoFullName --target $DefaultBranch --title ("$ProjectName " + $tag) --notes ("Auto deployment (" + $ArtifactType + ").")
        Write-Info "release created."
    }}
}}

Require-Command git
Require-Command gh

$WorkDir = Resolve-Abs -base $PSScriptRoot -path $WorkDir
if (-not (Test-Path -LiteralPath $WorkDir)) {{
    throw "WorkDir not found: $WorkDir"
}}

if ($Mode -eq 'source' -or $Mode -eq 'both') {{
    Invoke-SourceDeploy
}}

if ($Mode -eq 'release' -or $Mode -eq 'both') {{
    $pkg = New-ReleaseZip
    Publish-Release -tag $pkg.Tag -zipPath $pkg.ZipPath
}}

Write-Info "done."
"""


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Deploy PS1 Builder")
        self.geometry("980x760")

        self.repo_url_var = tk.StringVar()
        self.work_dir_var = tk.StringVar(value=str(Path.cwd()))
        self.project_name_var = tk.StringVar(value=_repo_name_from_url(""))
        self.branch_var = tk.StringVar(value="main")
        self.output_dir_var = tk.StringVar(value=str(Path.cwd() / "scripts"))
        self.preset_var = tk.StringVar(value="python")
        self.include_git_snapshot_var = tk.BooleanVar(value=True)

        self._build_ui()
        self._apply_preset("python")

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(root, text="GitHub URL").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        repo_entry = ttk.Entry(root, textvariable=self.repo_url_var)
        repo_entry.grid(row=row, column=1, sticky="ew", pady=4)
        repo_entry.bind("<FocusOut>", lambda _e: self._sync_project_name())
        row += 1

        ttk.Label(root, text="作業ディレクトリ").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(root, textvariable=self.work_dir_var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(root, text="参照", command=self._pick_work_dir).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
        row += 1

        ttk.Label(root, text="プロジェクト名").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(root, textvariable=self.project_name_var).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1

        ttk.Label(root, text="ブランチ").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(root, textvariable=self.branch_var).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1

        ttk.Label(root, text="PS1出力フォルダ").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(root, textvariable=self.output_dir_var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(root, text="参照", command=self._pick_output_dir).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
        row += 1

        ttk.Label(root, text="テンプレート").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        preset_combo = ttk.Combobox(root, textvariable=self.preset_var, state="readonly")
        preset_combo["values"] = tuple(PRESETS.keys())
        preset_combo.grid(row=row, column=1, sticky="ew", pady=4)
        preset_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_preset(self.preset_var.get()))
        ttk.Button(root, text="テンプレート適用", command=lambda: self._apply_preset(self.preset_var.get())).grid(
            row=row, column=2, sticky="w", padx=(8, 0), pady=4
        )
        row += 1

        ttk.Checkbutton(
            root,
            text="リリースZIPにGit管理ファイルのスナップショットを含める",
            variable=self.include_git_snapshot_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 8))
        row += 1

        ttk.Label(root, text="source必須ファイル（folder|pattern 形式）").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.source_required_text = tk.Text(root, height=9)
        self.source_required_text.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(0, 8))
        row += 1

        ttk.Label(root, text="release必須ファイル（folder|pattern 形式）").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.release_required_text = tk.Text(root, height=9)
        self.release_required_text.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(0, 8))
        row += 1

        ttk.Label(root, text="release追加同梱パターン（1行1パターン）").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.release_extra_text = tk.Text(root, height=6)
        self.release_extra_text.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(0, 10))
        row += 1

        button_row = ttk.Frame(root)
        button_row.grid(row=row, column=0, columnspan=3, sticky="ew")
        button_row.columnconfigure(0, weight=1)
        ttk.Button(button_row, text="現在テンプレートでPS1生成", command=self._generate_current).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(button_row, text="Python + C# DLL を同時生成", command=self._generate_python_and_csharp).grid(
            row=0, column=1, sticky="ew"
        )

        root.rowconfigure(7, weight=1)
        root.rowconfigure(9, weight=1)
        root.rowconfigure(11, weight=1)

    def _sync_project_name(self) -> None:
        current = self.project_name_var.get().strip()
        if current:
            return
        self.project_name_var.set(_repo_name_from_url(self.repo_url_var.get()))

    def _pick_work_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.work_dir_var.get() or str(Path.cwd()))
        if not path:
            return
        self.work_dir_var.set(path)

    def _pick_output_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(Path.cwd()))
        if not path:
            return
        self.output_dir_var.set(path)

    def _apply_preset(self, key: str) -> None:
        preset = PRESETS.get(key)
        if preset is None:
            return
        self.include_git_snapshot_var.set(preset.include_git_snapshot)
        self.source_required_text.delete("1.0", tk.END)
        self.source_required_text.insert("1.0", _map_to_text(preset.source_required))
        self.release_required_text.delete("1.0", tk.END)
        self.release_required_text.insert("1.0", _map_to_text(preset.release_required))
        self.release_extra_text.delete("1.0", tk.END)
        self.release_extra_text.insert("1.0", "\n".join(preset.release_extra_patterns))

    def _collect_inputs(self, preset_key: str) -> tuple[str, str, str, str, dict[str, list[str]], dict[str, list[str]], list[str], bool]:
        repo_url = self.repo_url_var.get().strip()
        if not repo_url:
            raise ValueError("GitHub URL を入力してください。")
        repo_full_name = _repo_full_name_from_url(repo_url)
        if not repo_full_name:
            raise ValueError("GitHub URL は github.com/<owner>/<repo> 形式で入力してください。")

        work_dir = self.work_dir_var.get().strip()
        if not work_dir:
            raise ValueError("作業ディレクトリを入力してください。")

        project_name = _slugify(self.project_name_var.get().strip() or _repo_name_from_url(repo_url))
        branch = (self.branch_var.get().strip() or "main")

        source_required = _text_to_map(self.source_required_text.get("1.0", tk.END))
        release_required = _text_to_map(self.release_required_text.get("1.0", tk.END))
        release_extra = _text_to_list(self.release_extra_text.get("1.0", tk.END))

        # README.md は強制必須
        source_required.setdefault(".", [])
        if "README.md" not in source_required["."]:
            source_required["."].insert(0, "README.md")

        return (
            project_name,
            repo_url,
            repo_full_name,
            branch,
            source_required,
            release_required,
            release_extra,
            self.include_git_snapshot_var.get(),
        )

    def _write_script(self, preset_key: str) -> Path:
        (
            project_name,
            repo_url,
            repo_full_name,
            branch,
            source_required,
            release_required,
            release_extra,
            include_git_snapshot,
        ) = self._collect_inputs(preset_key)

        work_dir = str(Path(self.work_dir_var.get().strip()).resolve())
        out_dir = Path(self.output_dir_var.get().strip() or (Path(work_dir) / "scripts")).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        suffix = PRESETS[preset_key].key
        file_name = f"Deploy-{project_name}-{suffix}.ps1"
        out_path = out_dir / file_name

        ps_text = _build_ps1_text(
            project_name=project_name,
            repo_url=repo_url,
            repo_full_name=repo_full_name,
            work_dir=work_dir,
            branch=branch,
            source_required=source_required,
            release_required=release_required,
            release_extra_patterns=release_extra,
            include_git_snapshot=include_git_snapshot,
            artifact_type=suffix,
        )
        out_path.write_text(ps_text, encoding="utf-8")
        return out_path

    def _generate_current(self) -> None:
        preset_key = self.preset_var.get().strip() or "python"
        if preset_key not in PRESETS:
            messagebox.showerror("エラー", f"未対応テンプレートです: {preset_key}")
            return
        try:
            out_path = self._write_script(preset_key)
        except Exception as exc:
            messagebox.showerror("生成失敗", str(exc))
            return
        messagebox.showinfo("生成完了", f"PS1を生成しました:\n{out_path}")

    def _generate_python_and_csharp(self) -> None:
        try:
            outputs = [self._write_script("python"), self._write_script("csharp_dll")]
        except Exception as exc:
            messagebox.showerror("生成失敗", str(exc))
            return
        text = "\n".join(str(p) for p in outputs)
        messagebox.showinfo("生成完了", f"2本生成しました:\n{text}")


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

