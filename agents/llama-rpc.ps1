#Requires -Version 5.1
<#
.SYNOPSIS
    llama.cpp RPC agent: splits a GGUF model across multiple machines and
    registers the combined endpoint with the peerfoundry coordinator.

.DESCRIPTION
    Two roles:
      primary  — runs llama-server (with optional --rpc workers), polls the
                 coordinator for jobs, and proxies inference results back.
      worker   — runs llama-rpc-server, donates its GPU memory to a primary.

    The coordinator never calls the machines directly; agents pull jobs.
    Works through the same NAT/firewall as the Ollama-based agent.ps1.

.PARAMETER Role
    "primary" or "worker". Omit to enter the interactive wizard.

.PARAMETER Coordinator
    HTTPS URL of the peerfoundry coordinator.  Required for primary role.

.PARAMETER ApiKey
    Coordinator agent API key.  Required for primary role.

.PARAMETER ModelAlias
    Name to register with the coordinator, e.g. "qwen3.6:27b".
    Defaults to the GGUF filename stem if not provided.

.PARAMETER ModelPath
    One of:
      - Path to a local .gguf file
      - Ollama model name (e.g. "qwen3.6:27b") — reuses the Ollama blob cache
      - HuggingFace repo ID (e.g. "Qwen/Qwen3.6-27B-GGUF") — prints download hint

.PARAMETER Workers
    Comma-separated RPC worker endpoints for the primary role.
    Example: "192.0.2.10:50052,192.0.2.11:50052"
    Leave empty to run without remote workers (single-machine mode).

.PARAMETER LlamaPort
    HTTP port for llama-server (primary only). Default: 8080.

.PARAMETER RpcPort
    TCP port for llama-rpc-server (worker role). Default: 50052.

.PARAMETER GpuLayers
    Transformer layers to offload to GPU. Default: 99 (all).

.PARAMETER CtxSize
    Context window in tokens. Default: 16384.

.PARAMETER LlamaBinDir
    Path to a directory that already contains llama-server.exe and
    llama-rpc-server.exe. Auto-downloaded from GitHub if not provided.

.EXAMPLE
    # Worker machine (GPU-NODE-02):
    .\llama-rpc.ps1 -Role worker

    # Primary machine (GPU-NODE-01):
    .\llama-rpc.ps1 -Role primary `
        -Coordinator https://coordinator.example.com -ApiKey abc123 `
        -ModelAlias "qwen3.6:27b" -ModelPath "qwen3.6:27b" `
        -Workers "192.0.2.10:50052"
#>
param(
    [string]$Role        = "",
    [string]$Coordinator = "",
    [string]$ApiKey      = "",
    [string]$ModelAlias  = "",
    [string]$ModelPath   = "",
    [string]$Workers     = "",
    [int]$LlamaPort      = 8080,
    [int]$RpcPort        = 50052,
    [int]$GpuLayers      = 99,
    [int]$CtxSize        = 16384,
    [string]$LlamaBinDir = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[System.Net.ServicePointManager]::SecurityProtocol = (
    [System.Net.SecurityProtocolType]::Tls12 -bor
    [System.Net.SecurityProtocolType]::Tls13
)

# ── console helpers ─────────────────────────────────────────────────────────

function Write-Step([string]$m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok([string]$m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Write-Err([string]$m)  { Write-Host "[!] $m" -ForegroundColor Red }

function Write-Status([string]$phase, [string]$detail = "") {
    $ts  = (Get-Date).ToString("HH:mm:ss")
    $msg = if ($detail) { "[$ts] $phase  -  $detail" } else { "[$ts] $phase" }
    Write-Host "  $msg" -ForegroundColor DarkCyan
    $msg | Out-File -FilePath "$env:TEMP\swarm-rpc-status.txt" -Encoding UTF8
    if ($script:Coordinator -and $script:ApiKey) {
        try {
            $body = @{
                phase    = $phase
                detail   = $detail
                agent_id = $script:AgentId
                hostname = $env:COMPUTERNAME
                model    = $script:ModelAlias
            } | ConvertTo-Json
            Invoke-RestMethod -Uri "$script:Coordinator/agent-status" -Method Post `
                -Body $body -ContentType "application/json" `
                -Headers @{ "X-API-Key" = $script:ApiKey } `
                -ErrorAction SilentlyContinue | Out-Null
        } catch { }
    }
}

# ── hardware ────────────────────────────────────────────────────────────────

function Get-HardwareQuick {
    $hw = @{ vram_gb = 0; ram_gb = 0; gpu_name = "none"; cpu_name = "unknown" }
    try {
        $smi = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null
        if ($LASTEXITCODE -eq 0 -and $smi) {
            $parts       = $smi -split ","
            $hw.gpu_name = $parts[0].Trim()
            $hw.vram_gb  = [math]::Round([double]$parts[1].Trim() / 1024, 1)
        }
    } catch { }
    try {
        $os         = Get-WmiObject Win32_OperatingSystem -ErrorAction Stop
        $hw.ram_gb  = [math]::Round($os.TotalVisibleMemorySize / 1MB, 0)
    } catch { }
    try {
        $cpu          = Get-WmiObject Win32_Processor -ErrorAction Stop | Select-Object -First 1
        $hw.cpu_name  = $cpu.Name.Trim()
    } catch { }
    return $hw
}

# ── llama.cpp binary management ─────────────────────────────────────────────

function Get-LlamaDir([string]$hint) {
    foreach ($dir in @($hint, "$env:LOCALAPPDATA\llama-cpp",
                                "$env:USERPROFILE\llama-cpp",
                                "C:\llama-cpp")) {
        if ($dir -and (Test-Path "$dir\llama-server.exe")) { return $dir }
    }
    return $null
}

function Install-LlamaCpp {
    $installDir = "$env:LOCALAPPDATA\llama-cpp"
    Write-Status "INSTALLING" "fetching latest llama.cpp release from GitHub..."

    $release = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest" `
        -UseBasicParsing -TimeoutSec 20

    # Prefer CUDA build; fall back to generic Windows x64
    $asset = $release.assets |
        Where-Object { $_.name -match 'win' -and $_.name -match 'cuda' -and
                       $_.name -match 'x64' -and $_.name -match '\.zip' } |
        Select-Object -First 1
    if (-not $asset) {
        $asset = $release.assets |
            Where-Object { $_.name -match 'win' -and $_.name -match 'x64' -and
                           $_.name -match '\.zip' -and $_.name -notmatch 'openblas' } |
            Select-Object -First 1
    }
    if (-not $asset) { throw "No suitable Windows x64 llama.cpp asset found in latest release" }

    Write-Step "Downloading $($asset.name)  ($([math]::Round($asset.size/1MB,0)) MB)..."
    $zipPath = "$env:TEMP\llama-cpp.zip"
    $wc = New-Object System.Net.WebClient
    $wc.DownloadFile($asset.browser_download_url, $zipPath)

    Write-Step "Extracting to $installDir ..."
    if (-not (Test-Path $installDir)) { New-Item -ItemType Directory $installDir | Out-Null }
    Expand-Archive -Path $zipPath -DestinationPath $installDir -Force
    Remove-Item $zipPath -ErrorAction SilentlyContinue

    # Some releases zip into a subdirectory — flatten it
    $sub = Get-ChildItem $installDir -Directory | Select-Object -First 1
    if ($sub -and (Test-Path "$($sub.FullName)\llama-server.exe")) {
        Get-ChildItem $sub.FullName | Move-Item -Destination $installDir -Force
        Remove-Item $sub.FullName -Recurse -ErrorAction SilentlyContinue
    }

    if (-not (Test-Path "$installDir\llama-server.exe")) {
        throw "llama-server.exe not found after extraction in $installDir"
    }
    Write-Ok "llama.cpp installed at $installDir"
    return $installDir
}

# ── GGUF model resolution ────────────────────────────────────────────────────

function Resolve-GgufPath([string]$spec) {
    # 1. Already a .gguf file path
    if ($spec -match '\.gguf$' -and (Test-Path $spec)) { return (Resolve-Path $spec).Path }

    # 2. Ollama model name — reuse the blob cache (no re-download)
    $ollamaBase = "$env:USERPROFILE\.ollama\models"
    if (Test-Path $ollamaBase) {
        $name = $spec -replace ':.*', ''
        $tag  = if ($spec -match ':(.+)') { $Matches[1] } else { "latest" }
        foreach ($mp in @(
            "$ollamaBase\manifests\registry.ollama.ai\library\$name\$tag",
            "$ollamaBase\manifests\registry.ollama.ai\$name\$tag"
        )) {
            if (-not (Test-Path $mp)) { continue }
            try {
                $manifest = Get-Content $mp -Raw | ConvertFrom-Json
                $layer    = @($manifest.layers) |
                    Where-Object { $_.mediaType -eq "application/vnd.ollama.image.model" } |
                    Select-Object -First 1
                if ($layer) {
                    $hash = $layer.digest -replace '^sha256:', ''
                    $blob = "$ollamaBase\blobs\sha256-$hash"
                    if (Test-Path $blob) { Write-Ok "Reusing Ollama cache: $blob"; return $blob }
                }
            } catch { }
        }
    }

    # 3. HuggingFace repo ID — guide the user
    if ($spec -match '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
        Write-Err "HuggingFace repo '$spec' not found locally."
        Write-Host ""
        Write-Host "  Download a GGUF file first:" -ForegroundColor Yellow
        Write-Host "    huggingface-cli download $spec --include '*Q4_K_M*' --local-dir .\models" -ForegroundColor White
        Write-Host "  Then re-run with -ModelPath pointing to the downloaded .gguf file." -ForegroundColor DarkGray
        throw "Model not downloaded yet."
    }

    throw "Cannot resolve model '$spec'. Use a .gguf path, an Ollama model name (name:tag), or a HuggingFace repo ID."
}

# ── coordinator comms ────────────────────────────────────────────────────────

function Get-AgentId {
    $f = "$env:TEMP\swarm-rpc-id.txt"
    if (Test-Path $f) {
        $id = (Get-Content $f -Raw).Trim()
        if ($id -match '^[0-9a-f-]{36}$') { return $id }
    }
    $id = [guid]::NewGuid().ToString()
    $id | Set-Content $f -Encoding UTF8
    return $id
}

function Register-WithCoordinator([string]$url, [string]$key, [int]$port, [hashtable]$hw) {
    # Report combined VRAM: this machine + workers (assumes same GPU model)
    $workerCount  = @($script:Workers -split ',' | Where-Object { $_ -ne "" }).Count
    $combinedVram = [math]::Round($hw.vram_gb * ($workerCount + 1), 1)

    $body = @{
        agent_id       = $script:AgentId
        port           = $port
        model          = $script:ModelAlias
        hostname       = $env:COMPUTERNAME
        gpu_name       = if ($hw.gpu_name -ne "none") { "$($hw.gpu_name) x$($workerCount+1) (RPC)" } else { $null }
        gpu_vram_gb    = $combinedVram
        ram_total_gb   = $hw.ram_gb
        cpu_name       = $hw.cpu_name
        os_version     = [System.Environment]::OSVersion.VersionString
        ollama_version = "llama.cpp-rpc"
    } | ConvertTo-Json

    Write-Step "Registering '$($script:ModelAlias)' (${combinedVram} GB combined VRAM)..."
    $resp = Invoke-RestMethod -Uri "$url/register" -Method Post `
        -Body $body -ContentType "application/json" `
        -Headers @{ "X-API-Key" = $key } -ErrorAction Stop
    Write-Ok "Registered. Fleet total: $($resp.machines_total) machine(s)"
}

# ── Ollama → OpenAI format bridge ────────────────────────────────────────────
#
# The coordinator queues jobs in Ollama-native format (options.num_ctx, etc.).
# llama-server speaks OpenAI format. This function translates before the call.
# The response from llama-server is already OpenAI format (has "choices"),
# so the coordinator's submit_job_result passes it through unchanged.

function ConvertTo-OpenAIBody([string]$ollamaJson) {
    $src  = $ollamaJson | ConvertFrom-Json
    $opts = $src.options

    $body = [ordered]@{
        model    = $src.model
        messages = $src.messages
        stream   = $false
    }

    if ($opts) {
        if ($null -ne $opts.temperature) { $body.temperature = $opts.temperature }
        # num_predict: -1 = unlimited; omit it so llama-server uses --ctx-size headroom
        if ($null -ne $opts.num_predict -and $opts.num_predict -gt 0) {
            $body.max_tokens = [int]$opts.num_predict
        }
    }
    if ($src.tools)            { $body.tools           = $src.tools }
    if ($src.format -eq "json") { $body.response_format = @{ type = "json_object" } }

    return $body | ConvertTo-Json -Depth 20
}

# ── llama-server readiness ───────────────────────────────────────────────────

function Wait-LlamaReady([int]$port, [int]$timeoutSec = 300) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    $t0       = Get-Date
    while ((Get-Date) -lt $deadline) {
        try {
            $r    = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" `
                        -UseBasicParsing -TimeoutSec 4 -ErrorAction Stop
            $data = $r.Content | ConvertFrom-Json
            if ($data.status -eq "ok") { Write-Host ""; return $true }
            # status="loading model" — keep waiting
        } catch { }
        $s = [math]::Round(((Get-Date) - $t0).TotalSeconds)
        Write-Host "`r  Loading model into GPU...  ${s}s " -NoNewline -ForegroundColor DarkGray
        Start-Sleep 3
    }
    Write-Host ""
    return $false
}

function Stop-PortProcess([int]$port) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $pid = $conn.OwningProcess
        if ($pid) { Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue; Start-Sleep 1 }
    }
}

# ── work loop (primary only) ─────────────────────────────────────────────────

function Start-WorkLoop([string]$url, [string]$key, [int]$port, [System.Diagnostics.Process]$srv) {
    $headers = @{ "X-API-Key" = $key }
    $lastHB  = [DateTime]::MinValue

    Write-Status "RUNNING" "polling for '$($script:ModelAlias)' jobs  -  Ctrl+C to quit"

    while ($true) {
        # Exit if the llama-server process died
        if ($srv.HasExited) {
            Write-Err "llama-server exited (code $($srv.ExitCode))  -  shutting down"
            exit 1
        }

        # Heartbeat every 15 s; re-register on 404 (coordinator restarted)
        if (([DateTime]::UtcNow - $lastHB).TotalSeconds -ge 15) {
            try {
                Invoke-WebRequest -Uri "$url/heartbeat" -Method Post `
                    -Body (@{ port = $port; agent_id = $script:AgentId } | ConvertTo-Json) `
                    -ContentType "application/json" `
                    -Headers $headers -UseBasicParsing -ErrorAction Stop | Out-Null
            } catch {
                $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
                if ($code -eq 404) {
                    Write-Host "[$(Get-Date -F 'HH:mm:ss')] Re-registering after coordinator restart..." -ForegroundColor Yellow
                    try { Register-WithCoordinator $url $key $port $script:hw } catch { }
                }
            }
            $lastHB = [DateTime]::UtcNow
        }

        # Poll for a job
        try {
            $mEnc = [Uri]::EscapeDataString($script:ModelAlias)
            $aEnc = [Uri]::EscapeDataString($script:AgentId)
            $resp = Invoke-WebRequest `
                -Uri "$url/agent/jobs/next?model=$mEnc&agent_id=$aEnc" `
                -Method Get -Headers $headers -UseBasicParsing -ErrorAction Stop
            if ($resp.StatusCode -ne 200) { Start-Sleep 2; continue }

            $job     = $resp.Content | ConvertFrom-Json
            $jobId   = $job.id
            $jobBody = $job.body_json
            Write-Host "[$(Get-Date -F 'HH:mm:ss')] Job $($jobId.Substring(0,8))  model=$($script:ModelAlias)" `
                       -ForegroundColor DarkCyan

            $t0 = [DateTime]::UtcNow
            try {
                $openAiJson = ConvertTo-OpenAIBody $jobBody

                # Inference in a background job so we can poll for cancellation
                $inferJob = Start-Job -ScriptBlock {
                    param($url, $body)
                    try {
                        $r = Invoke-WebRequest -Uri $url -Method Post -Body $body `
                            -ContentType "application/json" -TimeoutSec 1800 `
                            -UseBasicParsing -ErrorAction Stop
                        return $r.Content
                    } catch { return "__ERR__:$_" }
                } -ArgumentList "http://127.0.0.1:$port/v1/chat/completions", $openAiJson

                while ($inferJob.State -eq "Running") {
                    try {
                        $cr = Invoke-RestMethod `
                            -Uri "$url/agent/jobs/$jobId/cancelled" `
                            -Method Get -Headers $headers -TimeoutSec 3 -ErrorAction Stop
                        if ($cr.cancelled) {
                            Stop-Job   $inferJob -ErrorAction SilentlyContinue
                            Remove-Job $inferJob -Force  -ErrorAction SilentlyContinue
                            throw "Job $jobId cancelled by coordinator"
                        }
                    } catch [System.Management.Automation.RuntimeException] { throw }
                    catch { }
                    Start-Sleep 2
                }

                $raw = Receive-Job $inferJob -Wait
                Remove-Job $inferJob -Force
                if ($raw -like "__ERR__:*") { throw ($raw -replace "^__ERR__:", "") }

                $ms = [math]::Round(([DateTime]::UtcNow - $t0).TotalMilliseconds, 1)
                # llama-server returns OpenAI format; coordinator passes it through unchanged
                Invoke-RestMethod -Uri "$url/agent/jobs/$jobId/result" `
                    -Method Post -ContentType "application/json" `
                    -Body "{`"result`":$raw,`"elapsed_ms`":$ms}" `
                    -Headers $headers -ErrorAction SilentlyContinue | Out-Null
                Write-Ok "Job $($jobId.Substring(0,8)) done  $([math]::Round($ms/1000,1))s"

            } catch {
                $err = "$_"
                Write-Err "Job $($jobId.Substring(0,8)) failed: $err"
                try {
                    Invoke-RestMethod -Uri "$url/agent/jobs/$jobId/result" `
                        -Method Post -ContentType "application/json" `
                        -Body (@{ error = $err } | ConvertTo-Json) `
                        -Headers $headers -ErrorAction SilentlyContinue | Out-Null
                } catch { }
            }

        } catch {
            $msg = "$_"
            if ($msg -notmatch "204|No.*content") { Write-Host "  poll: $msg" -ForegroundColor DarkGray }
            Start-Sleep 2
        }
    }
}

# ── interactive wizard ────────────────────────────────────────────────────────

function Start-Wizard {
    $hw = Get-HardwareQuick
    Write-Host ""
    Write-Host "  PeerFoundry  |  llama.cpp RPC agent" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Detected: $($hw.gpu_name) ($($hw.vram_gb) GB VRAM)  |  $($hw.ram_gb) GB RAM" -ForegroundColor DarkCyan
    Write-Host ""
    Write-Host "  Role:" -ForegroundColor Yellow
    Write-Host "    [1] primary  — runs llama-server, polls coordinator for jobs"
    Write-Host "    [2] worker   — runs llama-rpc-server, donates GPU to a primary"
    $roleIn = (Read-Host "  Choice [1/2]").Trim()
    $role   = if ($roleIn -eq "2") { "worker" } else { "primary" }

    $cfg = @{
        Role = $role; Hw = $hw
        Coordinator = ""; ApiKey = ""; ModelAlias = ""; ModelPath = ""; Workers = ""
    }

    if ($role -eq "primary") {
        Write-Host ""
        Write-Host "  Coordinator URL (e.g. https://coordinator.example.com): " -NoNewline -ForegroundColor Yellow
        $cfg.Coordinator = (Read-Host).Trim().TrimEnd("/")

        Write-Host "  API Key: " -NoNewline -ForegroundColor Yellow
        $sec = Read-Host -AsSecureString
        $ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($sec)
        try   { $cfg.ApiKey = [System.Runtime.InteropServices.Marshal]::PtrToStringUni($ptr) }
        finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeGlobalAllocUnicode($ptr) }

        Write-Host "  Model alias for coordinator (e.g. qwen3.6:27b): " -NoNewline -ForegroundColor Yellow
        $cfg.ModelAlias = (Read-Host).Trim()

        Write-Host "  Model path / Ollama name / HF repo: " -NoNewline -ForegroundColor Yellow
        $cfg.ModelPath = (Read-Host).Trim()

        Write-Host "  RPC worker endpoints (e.g. 192.0.2.10:50052), blank = none: " -NoNewline -ForegroundColor Yellow
        $cfg.Workers = (Read-Host).Trim()
    }

    return $cfg
}

# ── main ─────────────────────────────────────────────────────────────────────

$script:AgentId     = Get-AgentId
$script:Coordinator = $Coordinator
$script:ApiKey      = $ApiKey
$script:ModelAlias  = $ModelAlias
$script:Workers     = $Workers

if ($Role -eq "") {
    $cfg = Start-Wizard
    $Role        = $cfg.Role
    $Coordinator = $cfg.Coordinator;  $script:Coordinator = $Coordinator
    $ApiKey      = $cfg.ApiKey;       $script:ApiKey      = $ApiKey
    $ModelAlias  = $cfg.ModelAlias;   $script:ModelAlias  = $ModelAlias
    $ModelPath   = $cfg.ModelPath
    $Workers     = $cfg.Workers;      $script:Workers     = $Workers
    $script:hw   = $cfg.Hw
} else {
    $script:hw = Get-HardwareQuick
}

# -- resolve llama.cpp binaries -----------------------------------------------

$llamaDir = Get-LlamaDir $LlamaBinDir
if (-not $llamaDir) { $llamaDir = Install-LlamaCpp }
Write-Ok "llama.cpp: $llamaDir"

$llamaServerExe    = "$llamaDir\llama-server.exe"
$llamaRpcServerExe = "$llamaDir\llama-rpc-server.exe"

if (-not (Test-Path $llamaServerExe))    { throw "llama-server.exe not found in $llamaDir" }
if (-not (Test-Path $llamaRpcServerExe)) { throw "llama-rpc-server.exe not found in $llamaDir" }

# ════════════════════════════════════════════════════════════════════════════
# WORKER MODE
# ════════════════════════════════════════════════════════════════════════════

if ($Role -eq "worker") {
    Write-Step "Starting llama-rpc-server on port $RpcPort (all interfaces)..."
    Stop-PortProcess $RpcPort

    $proc = Start-Process -FilePath $llamaRpcServerExe `
        -ArgumentList "--port", $RpcPort, "--host", "0.0.0.0" `
        -PassThru -NoNewWindow

    $myIp = (
        Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.*" } |
        Select-Object -First 1
    ).IPAddress

    Write-Ok "llama-rpc-server ready (PID $($proc.Id))  port=$RpcPort"
    Write-Host ""
    Write-Host "  Tell the primary machine to include this worker:" -ForegroundColor Yellow
    Write-Host "    -Workers `"${myIp}:${RpcPort}`"" -ForegroundColor White
    Write-Host ""
    Write-Host "  Waiting for connections. Ctrl+C to stop." -ForegroundColor DarkGray

    $proc.WaitForExit()
    exit $proc.ExitCode
}

# ════════════════════════════════════════════════════════════════════════════
# PRIMARY MODE
# ════════════════════════════════════════════════════════════════════════════

if ($Role -ne "primary") { throw "Role must be 'primary' or 'worker'" }
if (-not $Coordinator)   { throw "-Coordinator is required for Role=primary" }
if (-not $ApiKey)        { throw "-ApiKey is required for Role=primary" }

# -- resolve GGUF model -------------------------------------------------------

Write-Status "STARTING" "resolving model..."
$ggufPath = Resolve-GgufPath $ModelPath
Write-Ok "Model file: $ggufPath"

if (-not $script:ModelAlias) {
    $script:ModelAlias = [System.IO.Path]::GetFileNameWithoutExtension($ggufPath)
    Write-Step "ModelAlias defaulted to: $script:ModelAlias"
}

# -- verify coordinator -------------------------------------------------------

Write-Status "CONNECTING" "checking coordinator..."
try {
    $r = Invoke-WebRequest -Uri "$Coordinator/healthz" -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop
    if ($r.StatusCode -ne 200) { throw "healthz returned $($r.StatusCode)" }
} catch { throw "Cannot reach coordinator at $Coordinator`: $_" }
Write-Ok "Coordinator reachable"

# -- build llama-server args --------------------------------------------------

$srvArgs = @(
    "--model",         $ggufPath,
    "--port",          $LlamaPort,
    "--host",          "127.0.0.1",   # local only — coordinator never calls us directly
    "--n-gpu-layers",  $GpuLayers,
    "--ctx-size",      $CtxSize,
    "--flash-attn",
    "--parallel",      "1"            # one slot: model occupies all VRAM
)

$workerList = @($Workers -split ',' | Where-Object { $_ -ne "" })
if ($workerList.Count -gt 0) {
    $rpcEndpoints = $workerList -join ","
    $srvArgs += @("--rpc", $rpcEndpoints)
    Write-Step "RPC workers: $rpcEndpoints"
} else {
    Write-Step "No RPC workers — single-machine mode"
}

# -- start llama-server -------------------------------------------------------

Write-Status "STARTING" "launching llama-server..."
Stop-PortProcess $LlamaPort

$srvProc = Start-Process -FilePath $llamaServerExe `
    -ArgumentList $srvArgs -PassThru -NoNewWindow

Write-Step "Waiting for model to load (large models take 30–120 s)..."
if (-not (Wait-LlamaReady $LlamaPort 300)) {
    Stop-Process -Id $srvProc.Id -Force -ErrorAction SilentlyContinue
    throw "llama-server did not become ready within 300 seconds"
}
Write-Ok "llama-server ready (PID $($srvProc.Id))  port=$LlamaPort  ctx=$CtxSize  gpu-layers=$GpuLayers"

# -- register + work loop -----------------------------------------------------

Register-WithCoordinator $Coordinator $ApiKey $LlamaPort $script:hw
Start-WorkLoop           $Coordinator $ApiKey $LlamaPort $srvProc
