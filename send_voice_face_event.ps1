param(
    [string]$PipeName = "kks_voice_face_events",
    [string]$Json = "",
    [string]$JsonFile = "",
    [string]$TargetHost = "",
    [int]$TargetPort = 18765,
    [string]$TargetEndpoint = "/voice-face-event",
    [string]$TargetToken = "",
    [switch]$RemoteHttp,
    [int]$Main = 0,
    [string]$AudioPath = "",
    [string]$AssetBundle = "abdata\\sound\\data\\pcm\\c13\\h\\01.unity3d",
    [string]$AssetName = "h_ko_13_03_001",
    [int]$Face = -1,
    [string]$FacePresetId = "",
    [string]$FacePresetName = "",
    [switch]$FacePresetRandom,
    [string]$Faces = "",
    [double]$Volume = -1.0,
    [double]$Pitch = -1.0,
    [switch]$KeepCurrentFace,
    [string]$FaceMode = "",
    [switch]$Stop,
    [switch]$Interactive,
    [switch]$NoSend,
    [int]$ConnectTimeoutMs = 10000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
try {
    Add-Type -AssemblyName System.Net.Http -ErrorAction Stop | Out-Null
}
catch {
    # On newer PowerShell editions this assembly is already loaded.
}

function Normalize-PipeName([string]$rawPipeName) {
    if ([string]::IsNullOrWhiteSpace($rawPipeName)) {
        return "kks_voice_face_events"
    }

    $value = $rawPipeName.Trim()
    $prefix = "\\.\pipe\"
    if ($value.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        $value = $value.Substring($prefix.Length).Trim()
    }

    if ($value.Equals("kks_voice_face_events_diag_0423", [System.StringComparison]::OrdinalIgnoreCase)) {
        return "kks_voice_face_events"
    }

    if ([string]::IsNullOrWhiteSpace($value)) {
        return "kks_voice_face_events"
    }

    return $value
}

$PipeName = Normalize-PipeName $PipeName

function Parse-Faces([string]$rawFaces) {
    $items = @()
    if ([string]::IsNullOrWhiteSpace($rawFaces)) {
        return ,$items
    }

    foreach ($part in $rawFaces.Split(",")) {
        $token = $part.Trim()
        if ($token.Length -eq 0) {
            continue
        }

        $value = 0
        if (-not [int]::TryParse($token, [ref]$value)) {
            throw "faces parse failed: '$token'"
        }

        $items += $value
    }

    return ,$items
}

function Build-SpeakJson(
    [int]$main,
    [string]$audioPath,
    [string]$assetBundle,
    [string]$assetName,
    [int]$face,
    [string]$facePresetId,
    [string]$facePresetName,
    [bool]$facePresetRandom,
    [string]$facesText,
    [double]$volume,
    [double]$pitch,
    [bool]$keepCurrent,
    [string]$modeText
) {
    $payload = [ordered]@{
        type = "speak"
        main = $main
    }

    if (-not [string]::IsNullOrWhiteSpace($audioPath)) {
        $payload.audioPath = $audioPath
    }
    else {
        $payload.assetBundle = $assetBundle
        $payload.assetName = $assetName
    }

    if (-not [string]::IsNullOrWhiteSpace($facePresetName)) {
        $payload.facePresetName = $facePresetName.Trim()
        if ($facePresetRandom) {
            $payload.facePresetRandom = 1
        }
        if (-not [string]::IsNullOrWhiteSpace($facePresetId)) {
            $payload.facePresetId = $facePresetId.Trim()
        }
    }
    elseif (-not [string]::IsNullOrWhiteSpace($facePresetId)) {
        $payload.facePresetId = $facePresetId.Trim()
    }
    else {
        if ($keepCurrent) {
            $payload.keepCurrentFace = 1
        }

        if (-not [string]::IsNullOrWhiteSpace($modeText)) {
            $payload.faceMode = $modeText
        }

        if ($face -ge 0) {
            $payload.face = $face
        }

        $parsedFaces = Parse-Faces $facesText
        if ($parsedFaces.Count -gt 0) {
            $payload.faces = $parsedFaces
        }
    }

    if ($volume -ge 0.0) {
        $payload.volume = [Math]::Min(1.0, [Math]::Max(0.0, $volume))
    }

    if ($pitch -ge 0.0) {
        $payload.pitch = [Math]::Min(3.0, [Math]::Max(0.1, $pitch))
    }

    return ($payload | ConvertTo-Json -Compress -Depth 10)
}

function Send-PipeLine([string]$pipeName, [string]$line, [int]$timeoutMs) {
    $client = New-Object System.IO.Pipes.NamedPipeClientStream(".", $pipeName, [System.IO.Pipes.PipeDirection]::Out)
    try {
        $client.Connect($timeoutMs)
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        $writer = New-Object System.IO.StreamWriter($client, $utf8, 1024, $true)
        try {
            $writer.NewLine = "`n"
            $writer.AutoFlush = $true
            $writer.WriteLine($line)
        }
        finally {
            try {
                $writer.Dispose()
            }
            catch [System.IO.IOException] {
                # Receiver may close immediately after reading the line.
            }
        }
    }
    finally {
        $client.Dispose()
    }
}

function Normalize-Endpoint([string]$endpoint) {
    if ([string]::IsNullOrWhiteSpace($endpoint)) {
        return "/voice-face-event"
    }

    $trimmed = $endpoint.Trim()
    if (-not $trimmed.StartsWith("/")) {
        return "/" + $trimmed
    }

    return $trimmed
}

function Send-HttpLine(
    [string]$targetHost,
    [int]$targetPort,
    [string]$targetEndpoint,
    [string]$targetToken,
    [string]$line,
    [int]$timeoutMs
) {
    if ([string]::IsNullOrWhiteSpace($targetHost)) {
        throw "targetHost is empty"
    }

    $endpoint = Normalize-Endpoint $targetEndpoint
    $uri = "http://$($targetHost.Trim()):$targetPort$endpoint"

    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $requestPayload = @{
        line = $line
    } | ConvertTo-Json -Compress -Depth 10

    $content = New-Object System.Net.Http.StringContent($requestPayload, $utf8, "application/json")
    $handler = New-Object System.Net.Http.HttpClientHandler
    $client = New-Object System.Net.Http.HttpClient($handler)

    try {
        $safeTimeout = [Math]::Max(1000, $timeoutMs)
        $client.Timeout = [TimeSpan]::FromMilliseconds($safeTimeout)
        if (-not [string]::IsNullOrWhiteSpace($targetToken)) {
            $client.DefaultRequestHeaders.Add("X-Auth-Token", $targetToken)
        }

        $response = $client.PostAsync($uri, $content).GetAwaiter().GetResult()
        $responseText = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "http error status=$([int]$response.StatusCode) body=$responseText"
        }

        return $responseText
    }
    finally {
        $content.Dispose()
        $client.Dispose()
        $handler.Dispose()
    }
}

function Send-CommandLine([string]$lineToSend) {
    if ([string]::IsNullOrWhiteSpace($lineToSend)) {
        throw "line is empty"
    }

    if ($NoSend) {
        Write-Host "[NoSend] $lineToSend"
        Write-Host "[SendResult] status=skip reason=nosend"
        return
    }

    $useRemote = $RemoteHttp.IsPresent -or -not [string]::IsNullOrWhiteSpace($TargetHost)
    $transport = if ($useRemote) { "http" } else { "pipe" }
    Write-Host "[SendStart] transport=$transport bytes=$([System.Text.Encoding]::UTF8.GetByteCount($lineToSend))"
    try {
        if ($useRemote) {
            $responseText = Send-HttpLine `
                -targetHost $TargetHost `
                -targetPort $TargetPort `
                -targetEndpoint $TargetEndpoint `
                -targetToken $TargetToken `
                -line $lineToSend `
                -timeoutMs $ConnectTimeoutMs
            Write-Host "[SentHttp] host=$TargetHost port=$TargetPort endpoint=$(Normalize-Endpoint $TargetEndpoint)"
            if (-not [string]::IsNullOrWhiteSpace($responseText)) {
                Write-Host "[HttpResponse] $responseText"
            }
            Write-Host "[SendResult] status=ok transport=http"
            return
        }

        Send-PipeLine -pipeName $PipeName -line $lineToSend -timeoutMs $ConnectTimeoutMs
        Write-Host "[SentPipe] $lineToSend"
        Write-Host "[SendResult] status=ok transport=pipe"
    }
    catch {
        $safeError = if ($null -ne $_ -and $null -ne $_.Exception) { $_.Exception.Message } else { "unknown_error" }
        Write-Host "[SendResult] status=ng transport=$transport error=$safeError"
        throw
    }
}

function Show-InteractiveHelp {
    Write-Host "transport:"
    Write-Host "  default             -> local named pipe"
    Write-Host "  -TargetHost/-RemoteHttp -> HTTP bridge mode"
    Write-Host "commands:"
    Write-Host "  stop                 -> stop current external audio playback"
    Write-Host "  wav <path>           -> play wav with keep-current-face mode"
    Write-Host "  keep                 -> keep current face mode"
    Write-Host "  face <id>            -> explicit face id"
    Write-Host "  preset <id>          -> facePresetId mode"
    Write-Host "  presetname <name>    -> facePresetName mode"
    Write-Host "  faces <a,b,c>        -> random from face list"
    Write-Host "  asset <name>         -> set default assetName"
    Write-Host "  bundle <path>        -> set default assetBundle"
    Write-Host "  raw <json>           -> send raw json"
    Write-Host "  { ... }              -> send raw json"
    Write-Host "  help                 -> show this message"
    Write-Host "  quit                 -> exit"
}

function Run-Interactive {
    $stateMain = $Main
    $stateAssetBundle = $AssetBundle
    $stateAssetName = $AssetName

    $mode = if ($RemoteHttp.IsPresent -or -not [string]::IsNullOrWhiteSpace($TargetHost)) { "http" } else { "pipe" }
    Write-Host "[Interactive] mode=$mode pipe=$PipeName targetHost=$TargetHost targetPort=$TargetPort endpoint=$(Normalize-Endpoint $TargetEndpoint) timeoutMs=$ConnectTimeoutMs"
    Show-InteractiveHelp

    while ($true) {
        $line = Read-Host "voice-face-sender"
        if ($null -eq $line) {
            continue
        }

        $trimmed = $line.Trim()
        if ($trimmed.Length -eq 0) {
            continue
        }

        if ($trimmed -eq "quit" -or $trimmed -eq "exit") {
            break
        }

        if ($trimmed -eq "help") {
            Show-InteractiveHelp
            continue
        }

        if ($trimmed -eq "stop") {
            Send-CommandLine -lineToSend '{"type":"stop"}'
            continue
        }

        if ($trimmed -match "^asset\s+(.+)$") {
            $stateAssetName = $Matches[1].Trim()
            Write-Host "[State] assetName=$stateAssetName"
            continue
        }

        if ($trimmed -match "^bundle\s+(.+)$") {
            $stateAssetBundle = $Matches[1].Trim()
            Write-Host "[State] assetBundle=$stateAssetBundle"
            continue
        }

        if ($trimmed -eq "keep") {
            $jsonLine = Build-SpeakJson -main $stateMain -audioPath "" -assetBundle $stateAssetBundle -assetName $stateAssetName -face -1 -facePresetId "" -facesText "" -volume $Volume -pitch $Pitch -keepCurrent $true -modeText "keep_current"
            Send-CommandLine -lineToSend $jsonLine
            continue
        }

        if ($trimmed -match "^wav\s+(.+)$") {
            $wavPath = $Matches[1].Trim()
            $jsonLine = Build-SpeakJson -main $stateMain -audioPath $wavPath -assetBundle "" -assetName "" -face -1 -facePresetId "" -facesText "" -volume $Volume -pitch $Pitch -keepCurrent $true -modeText "keep_current"
            Send-CommandLine -lineToSend $jsonLine
            continue
        }

        if ($trimmed -match "^face\s+(-?\d+)$") {
            $faceId = [int]$Matches[1]
            $jsonLine = Build-SpeakJson -main $stateMain -audioPath "" -assetBundle $stateAssetBundle -assetName $stateAssetName -face $faceId -facePresetId "" -facesText "" -volume $Volume -pitch $Pitch -keepCurrent $false -modeText "select"
            Send-CommandLine -lineToSend $jsonLine
            continue
        }

        if ($trimmed -match "^preset\s+(.+)$") {
            $presetId = $Matches[1].Trim()
            if ([string]::IsNullOrWhiteSpace($presetId)) {
                Write-Host "[Warn] preset id is empty"
                continue
            }

            $jsonLine = Build-SpeakJson -main $stateMain -audioPath "" -assetBundle $stateAssetBundle -assetName $stateAssetName -face -1 -facePresetId $presetId -facePresetName "" -facePresetRandom $false -facesText "" -volume $Volume -pitch $Pitch -keepCurrent $false -modeText ""
            Send-CommandLine -lineToSend $jsonLine
            continue
        }

        if ($trimmed -match "^presetname\s+(.+)$") {
            $presetName = $Matches[1].Trim()
            if ([string]::IsNullOrWhiteSpace($presetName)) {
                Write-Host "[Warn] preset name is empty"
                continue
            }

            $jsonLine = Build-SpeakJson -main $stateMain -audioPath "" -assetBundle $stateAssetBundle -assetName $stateAssetName -face -1 -facePresetId "" -facePresetName $presetName -facePresetRandom $false -facesText "" -volume $Volume -pitch $Pitch -keepCurrent $false -modeText ""
            Send-CommandLine -lineToSend $jsonLine
            continue
        }

        if ($trimmed -match "^faces\s+(.+)$") {
            $facesText = $Matches[1].Trim()
            $jsonLine = Build-SpeakJson -main $stateMain -audioPath "" -assetBundle $stateAssetBundle -assetName $stateAssetName -face -1 -facePresetId "" -facesText $facesText -volume $Volume -pitch $Pitch -keepCurrent $false -modeText "select"
            Send-CommandLine -lineToSend $jsonLine
            continue
        }

        if ($trimmed -match "^raw\s+(.+)$") {
            Send-CommandLine -lineToSend $Matches[1].Trim()
            continue
        }

        if ($trimmed.StartsWith("{")) {
            Send-CommandLine -lineToSend $trimmed
            continue
        }

        Write-Host "[Warn] unknown command: $trimmed"
    }
}

if ($Interactive) {
    Run-Interactive
    exit 0
}

$lineToSend = $Json
if (-not [string]::IsNullOrWhiteSpace($JsonFile)) {
    $resolvedJsonFile = [System.IO.Path]::GetFullPath($JsonFile)
    if (-not [System.IO.File]::Exists($resolvedJsonFile)) {
        throw "JsonFile not found: $resolvedJsonFile"
    }
    $lineToSend = [System.IO.File]::ReadAllText($resolvedJsonFile, [System.Text.Encoding]::UTF8).Trim()
}
if ([string]::IsNullOrWhiteSpace($lineToSend)) {
    if ($Stop) {
        $lineToSend = '{"type":"stop"}'
    }
    else {
        $lineToSend = Build-SpeakJson -main $Main -audioPath $AudioPath -assetBundle $AssetBundle -assetName $AssetName -face $Face -facePresetId $FacePresetId -facePresetName $FacePresetName -facePresetRandom $FacePresetRandom.IsPresent -facesText $Faces -volume $Volume -pitch $Pitch -keepCurrent $KeepCurrentFace.IsPresent -modeText $FaceMode
    }
}

Send-CommandLine -lineToSend $lineToSend
