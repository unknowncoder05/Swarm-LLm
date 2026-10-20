#Requires -Version 5.1
<#
.SYNOPSIS
    Sets up Ollama + a coding LLM on a Windows machine and registers
    it with a swarm-llm coordinator.

.EXAMPLE
    # Interactive wizard (recommended  -  auto-detects hardware, shows model menu):
    .\agent.ps1

    # Non-interactive (CI / scripted):
    .\agent.ps1 -Coordinator https://1.2.3.4:8443 -ApiKey abc123 -Model devstral-small-2:24b
#>
param(
    [string]$Coordinator  = "",
    [string]$ApiKey       = "",
    [string]$Model        = "",
    [int]$OllamaPort      = 11434,
    [switch]$SkipModelPull
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Require TLS 1.2+ and validate certificates normally (no self-signed bypass).
[System.Net.ServicePointManager]::SecurityProtocol = (
    [System.Net.SecurityProtocolType]::Tls12 -bor
    [System.Net.SecurityProtocolType]::Tls13
)

# -- media inference script (embedded Python  -  written to disk when needed) ----
# Single-quoted here-string: no PowerShell variable interpolation.

$script:MEDIA_INFER_PY = @'
import argparse, os, sys, time

def _download_model(model_id, token=None):
    import huggingface_hub as _hf
    try:
        _hf.snapshot_download(model_id, local_files_only=True, token=token or None)
        return
    except Exception:
        pass
    try:
        import tqdm as _tqdm
        _state = {"total": 0, "done": 0, "last_pct": -1}
        class _Bar(_tqdm.tqdm):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                if self.total:
                    _state["total"] += self.total
            def update(self, n=1):
                result = super().update(n)
                _state["done"] += n or 0
                if _state["total"]:
                    pct = int(_state["done"] / _state["total"] * 100)
                    if pct != _state["last_pct"]:
                        _state["last_pct"] = pct
                        mb = _state["done"] / 1024**2
                        total_mb = _state["total"] / 1024**2
                        print(f"DOWNLOAD {mb:.0f}/{total_mb:.0f}", flush=True)
                return result
        _hf.snapshot_download(model_id, token=token or None, tqdm_class=_Bar)
    except Exception:
        _hf.snapshot_download(model_id, token=token or None)

def _progress_cb(total_steps):
    def cb(pipe, step, timestep, kwargs):
        print(f"PROGRESS {step+1}/{total_steps}", flush=True)
        return kwargs
    return cb

def run_image(model, prompt, neg, out_dir, n, size, quality):
    import torch
    w, h = map(int, size.split("x"))
    steps, guidance = 30, 7.5
    if model == "sdxl":
        _download_model("stabilityai/stable-diffusion-xl-base-1.0")
        from diffusers import StableDiffusionXLPipeline
        pipe = StableDiffusionXLPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
            torch_dtype=torch.float16, use_safetensors=True, variant="fp16",
            local_files_only=True
        ).to("cuda")
    else:
        sys.exit(f"Unknown image model: {model}")
    if hasattr(pipe, "enable_model_cpu_offload"):
        pipe.enable_model_cpu_offload()
    os.makedirs(out_dir, exist_ok=True)
    cb = _progress_cb(steps)
    for i in range(n):
        result = pipe(
            prompt=prompt,
            negative_prompt=neg or None,
            num_inference_steps=steps,
            guidance_scale=guidance,
            width=w, height=h,
            callback_on_step_end=cb,
        )
        fp = os.path.join(out_dir, f"img_{i}_{int(time.time())}.png")
        result.images[0].save(fp)
        print(fp, flush=True)

_MODEL_NATIVE_FPS = {"wan-2.1-t2v-1.3b": 16}

def run_video(model, prompt, neg, out_file, duration, width, height,
              guidance_scale=5.0, num_inference_steps=50, fps=8):
    import torch, numpy as np
    native_fps = _MODEL_NATIVE_FPS.get(model, fps)
    cb = _progress_cb(num_inference_steps)
    if model == "ltx-video":
        _download_model("Lightricks/LTX-Video")
        from diffusers import LTXPipeline
        pipe = LTXPipeline.from_pretrained(
            "Lightricks/LTX-Video", torch_dtype=torch.bfloat16, local_files_only=True
        )
        pipe.enable_model_cpu_offload()
        result = pipe(
            prompt=prompt, negative_prompt=neg or None,
            width=width, height=height,
            num_frames=duration * native_fps + 1,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            callback_on_step_end=cb,
        )
    elif model == "cogvideox-2b":
        _download_model("THUDM/CogVideoX-2b")
        from diffusers import CogVideoXPipeline
        pipe = CogVideoXPipeline.from_pretrained(
            "THUDM/CogVideoX-2b", torch_dtype=torch.bfloat16, local_files_only=True
        ).to("cuda")
        result = pipe(
            prompt=prompt,
            num_frames=49, width=720, height=480,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            callback_on_step_end=cb,
        )
    elif model == "wan-2.1-t2v-1.3b":
        _download_model("Wan-AI/Wan2.1-T2V-1.3B-Diffusers")
        from diffusers import WanPipeline
        pipe = WanPipeline.from_pretrained(
            "Wan-AI/Wan2.1-T2V-1.3B-Diffusers", torch_dtype=torch.bfloat16, local_files_only=True
        ).to("cuda")
        result = pipe(
            prompt=prompt, negative_prompt=neg or None,
            height=height, width=width,
            num_frames=duration * native_fps + 1,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            callback_on_step_end=cb,
        )
    else:
        sys.exit(f"Unknown video model: {model}")
    frames = result.frames[0]
    frames_np = [np.array(f) for f in frames]
    import imageio
    os.makedirs(os.path.dirname(os.path.abspath(out_file)), exist_ok=True)
    imageio.mimwrite(out_file, frames_np, fps=fps, quality=8)
    print(out_file, flush=True)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--type",           required=True, choices=["image","video"])
    p.add_argument("--model",          required=True)
    p.add_argument("--prompt",         required=True)
    p.add_argument("--neg",            default="")
    p.add_argument("--out-dir",        default=".")
    p.add_argument("--out-file",       default="out.mp4")
    p.add_argument("--n",              type=int,   default=1)
    p.add_argument("--size",           default="1024x1024")
    p.add_argument("--quality",        default="standard")
    p.add_argument("--duration",       type=int,   default=5)
    p.add_argument("--width",          type=int,   default=704)
    p.add_argument("--height",         type=int,   default=480)
    p.add_argument("--guidance-scale", type=float, default=5.0)
    p.add_argument("--steps",          type=int,   default=50)
    p.add_argument("--fps",            type=int,   default=8)
    a = p.parse_args()
    if a.type == "image":
        run_image(a.model, a.prompt, a.neg, a.out_dir, a.n, a.size, a.quality)
    else:
        run_video(a.model, a.prompt, a.neg, a.out_file, a.duration, a.width, a.height,
                  guidance_scale=a.guidance_scale, num_inference_steps=a.steps, fps=a.fps)
'@

# -- console helpers -----------------------------------------------------------

function Write-Step([string]$msg) { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Err([string]$msg)  { Write-Host "[!] $msg" -ForegroundColor Red }

function Show-Banner {
    Clear-Host
    Write-Host ""
    Write-Host "  #######+##+    ##+ #####+ ######+ ###+   ###+    ##+     ##+     ###+   ###+" -ForegroundColor Cyan
    Write-Host "  ##+====+##|    ##|##+==##+##+==##+####+ ####|    ##|     ##|     ####+ ####|" -ForegroundColor Cyan
    Write-Host "  #######+##| #+ ##|#######|######++##+####+##|    ##|     ##|     ##+####+##|" -ForegroundColor Cyan
    Write-Host "  +====##|##|###+##|##+==##|##+==##+##|+##++##|    ##|     ##|     ##|+##++##|" -ForegroundColor Cyan
    Write-Host "  #######|+###+###++##|  ##|##|  ##|##| +=+ ##|    #######+#######+##| +=+ ##|" -ForegroundColor Cyan
    Write-Host "  +======+ +==++==+ +=+  +=++=+  +=++=+     +=+    +======++======++=+     +=+" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  swarm-llm Agent  |  TLS ON  |  API Key Auth ON" -ForegroundColor DarkCyan
    Write-Host ""
}

# Arrow-key choice list. Returns the selected item.
function Show-ChoiceList([string]$prompt, [string[]]$items, [int]$defaultIndex = 0) {
    $selected = $defaultIndex
    $top = [Console]::CursorTop

    while ($true) {
        # Redraw
        [Console]::SetCursorPosition(0, $top)
        Write-Host "  $prompt" -ForegroundColor Yellow
        Write-Host ""
        for ($i = 0; $i -lt $items.Count; $i++) {
            if ($i -eq $selected) {
                Write-Host "   > $($items[$i])" -ForegroundColor White -BackgroundColor DarkBlue
            } else {
                Write-Host "     $($items[$i])" -ForegroundColor Gray
            }
        }
        Write-Host ""
        Write-Host "  [Up/Down] navigate   [Enter] select" -ForegroundColor DarkGray

        $key = [Console]::ReadKey($true)
        switch ($key.Key) {
            "UpArrow"   { if ($selected -gt 0)                { $selected-- } }
            "DownArrow" { if ($selected -lt $items.Count - 1) { $selected++ } }
            "Enter"     { Write-Host ""; return $items[$selected] }
            "Escape"    { throw "Cancelled" }
        }
    }
}

function Read-MaskedInput([string]$prompt) {
    Write-Host "  $prompt" -NoNewline -ForegroundColor Yellow
    $secure = Read-Host -AsSecureString
    $ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($secure)
    try { return [System.Runtime.InteropServices.Marshal]::PtrToStringUni($ptr) }
    finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeGlobalAllocUnicode($ptr) }
}

# -- hardware detection --------------------------------------------------------

function Measure-NetworkSpeed {
    # Downloads 5 MB from Cloudflare's speed test endpoint; returns MB/s or $null
    $testUrl = "https://speed.cloudflare.com/__down?bytes=5242880"
    $tmpFile = [System.IO.Path]::GetTempFileName()
    try {
        $wc = New-Object System.Net.WebClient
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $wc.DownloadFile($testUrl, $tmpFile)
        $sw.Stop()
        $bytes = (Get-Item $tmpFile).Length
        if ($sw.Elapsed.TotalSeconds -gt 0 -and $bytes -gt 0) {
            return [math]::Round($bytes / $sw.Elapsed.TotalSeconds / 1MB, 2)
        }
    } catch { }
    finally {
        Remove-Item $tmpFile -ErrorAction SilentlyContinue
    }
    return $null
}

function Get-HardwareQuick {
    $hw = @{ vram_gb = 0; ram_gb = 0; gpu_name = "none"; cpu_name = "unknown"; network_mbps = $null }
    try {
        $smi = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null
        if ($LASTEXITCODE -eq 0 -and $smi) {
            $parts = $smi -split ","
            $hw.gpu_name = $parts[0].Trim()
            $hw.vram_gb  = [math]::Round([double]$parts[1].Trim() / 1024, 1)
        }
    } catch { }
    try {
        $os = Get-WmiObject Win32_OperatingSystem -ErrorAction Stop
        $hw.ram_gb = [math]::Round($os.TotalVisibleMemorySize / 1MB, 0)
    } catch { }
    try {
        $cpu = Get-WmiObject Win32_Processor -ErrorAction Stop | Select-Object -First 1
        $hw.cpu_name = $cpu.Name.Trim()
    } catch { }
    return $hw
}

# Pick recommended model index based on VRAM
function Get-RecommendedModelIndex([double]$vramGb) {
    if     ($vramGb -ge 18) { return 4 } # qwen3.6:27b          -  best coding 2026, 18+ GB
    elseif ($vramGb -ge 16) { return 3 } # devstral-small-2:24b  -  best agentic coding
    elseif ($vramGb -ge 12) { return 2 } # qwen3:14b             -  excellent tool use, 12+ GB
    elseif ($vramGb -ge 8)  { return 1 } # gemma4:12b            -  strong general, 8+ GB
    else                    { return 0 } # qwen3:8b              -  minimum capable agentic model
}

# VRAM required for a given model. Falls back to a heuristic for unknown models.
function Get-ModelVram([string]$model) {
    $known = @{
        "devstral-small-2:24b" = 15.0
        "qwen3.6:27b"          = 17.0
        "qwen3:30b-a3b"        = 17.0
        "qwen3:14b"            =  9.3
        "gemma4:12b"           =  8.0
        "qwen3:8b"             =  5.2
    }
    if ($known.ContainsKey($model)) { return $known[$model] }
    if ($model -match ':(\d+\.?\d*)b') { return [math]::Round([double]$Matches[1] * 0.65, 1) }
    return 5.0  # conservative default for unknown models
}

# All model IDs this machine can serve given its VRAM.
function Get-ModelCandidates([double]$vramGb) {
    $catalog = @(
        @{ id="qwen3:8b";             vram=5.2  },
        @{ id="gemma4:12b";           vram=8.0  },
        @{ id="qwen3:14b";            vram=9.3  },
        @{ id="devstral-small-2:24b"; vram=15.0 },
        @{ id="qwen3.6:27b";          vram=17.0 },
        @{ id="qwen3:30b-a3b";        vram=17.0 }
    )
    $limit = if ($vramGb -le 0) { 6.0 } else { $vramGb - 1.0 }
    return @($catalog | Where-Object { $_.vram -le $limit } | ForEach-Object { $_.id })
}

# -- system info (full, for registration) --------------------------------------

function Get-SystemInfo([hashtable]$hw) {
    $info = @{
        hostname       = $env:COMPUTERNAME
        cpu_name       = $hw.cpu_name
        cpu_cores      = $null
        ram_total_gb   = $hw.ram_gb
        ram_free_gb    = $null
        gpu_name       = if ($hw.gpu_name -ne "none") { $hw.gpu_name } else { $null }
        gpu_vram_gb    = if ($hw.vram_gb -gt 0)       { $hw.vram_gb }  else { $null }
        network_mbps   = $hw.network_mbps
        ollama_version = "?"
        os_version     = [System.Environment]::OSVersion.VersionString
    }
    try {
        $cpu = Get-WmiObject Win32_Processor -ErrorAction Stop | Select-Object -First 1
        $info.cpu_cores = [int]$cpu.NumberOfLogicalProcessors
    } catch { }
    try {
        $os = Get-WmiObject Win32_OperatingSystem -ErrorAction Stop
        $info.ram_free_gb = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
    } catch { }
    try {
        $ver = & $script:ollamaExe --version 2>&1
        $info.ollama_version = ($ver -replace "ollama version (is )?", "").Trim()
    } catch { }
    return $info
}

# -- ollama helpers ------------------------------------------------------------

# How many concurrent requests Ollama should handle for a given model + VRAM.
# LLM inference is memory-bandwidth bound: a single request leaves ~50% GPU idle.
# Parallel slots let multiple agentic sessions share the same loaded weights.
# Uses 4 GB/slot as a conservative budget (actual KV cache with q8_0 is ~half that).
function Get-OllamaParallel([double]$vramGb, [string]$model) {
    if ($vramGb -le 0) { return 1 }  # CPU inference  -  no benefit from parallelism
    $modelVram = switch -Wildcard ($model) {
        "devstral-small-2:24b" { 15 }
        "qwen3.6:27b"          { 17 }
        "qwen3:30b*"           { 17 }
        "qwen3:14b"            {  9 }
        "gemma4:12b"           {  8 }
        "qwen3:8b"             {  5 }
        default                { [math]::Ceiling($vramGb * 0.65) }
    }
    # Headroom after weights and 2 GB driver/OS overhead
    $freeVram = $vramGb - $modelVram - 2
    return [math]::Max(1, [math]::Min(4, 1 + [int][math]::Floor($freeVram / 4)))
}

function Get-OllamaPath {
    $found = Get-Command ollama -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
        "$env:USERPROFILE\AppData\Local\Programs\Ollama\ollama.exe",
        "$env:TEMP\ollama\ollama.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

function Install-Ollama {
    # Fast path 1: winget  -  instant if available, no download needed
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Step "Trying winget (fastest path)..."
        Write-Status "INSTALLING" "installing via winget..."
        winget install --id Ollama.Ollama --silent `
            --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
        $path = Get-OllamaPath
        if ($path) { Write-Ok "Installed via winget: $path"; return $path }
        Write-Step "winget did not produce Ollama  -  falling back to portable zip"
    }

    # Primary download method: portable zip (no installer, no UAC, no admin required)
    $cacheDir   = Join-Path $env:LOCALAPPDATA "swarm-llm"
    $installDir = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
    if (-not (Test-Path $cacheDir))   { New-Item -ItemType Directory -Path $cacheDir   | Out-Null }
    if (-not (Test-Path $installDir)) { New-Item -ItemType Directory -Path $installDir | Out-Null }
    $zipFile = Join-Path $cacheDir "ollama-windows-amd64.zip"

    $useCached = (Test-Path $zipFile) -and
                 ((Get-Item $zipFile).Length -gt 100MB) -and
                 ((Get-Date) - (Get-Item $zipFile).LastWriteTime).TotalHours -lt 8

    if ($useCached) {
        $ageMin = [math]::Round(((Get-Date) - (Get-Item $zipFile).LastWriteTime).TotalMinutes)
        Write-Ok "Using cached zip (${ageMin}m old)  -  skipping download"
        Write-Status "INSTALLING" "using cached zip (${ageMin}m old)"
    } else {
        # Prefer coordinator mirror (EC2 bandwidth) over GitHub
        $mirrorUrl   = "$script:Coordinator/ollama/ollama-windows-amd64.zip"
        $officialUrl = "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip"
        $downloadUrl = $officialUrl
        try {
            $head = Invoke-WebRequest -Uri $mirrorUrl -Method Head -UseBasicParsing -TimeoutSec 4 -ErrorAction Stop
            if ($head.StatusCode -eq 200) { $downloadUrl = $mirrorUrl; Write-Step "Using coordinator mirror" }
        } catch { }

        $attempts = 0
        while ($attempts -lt 3) {
            try {
                Remove-Item $zipFile -ErrorAction SilentlyContinue
                Write-Step "Downloading Ollama from $downloadUrl ..."
                $dlJob = Start-Job -ScriptBlock {
                    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
                    Invoke-WebRequest -Uri $using:downloadUrl -OutFile $using:zipFile -UseBasicParsing
                }
                while ($dlJob.State -eq "Running") {
                    Start-Sleep 5
                    $mb = if (Test-Path $zipFile) { [math]::Round((Get-Item $zipFile).Length / 1MB, 0) } else { 0 }
                    Write-Status "INSTALLING" "downloading Ollama... ${mb} MB"
                }
                Receive-Job $dlJob -ErrorAction Stop | Out-Null
                Remove-Job $dlJob
                break
            } catch {
                Remove-Job $dlJob -Force -ErrorAction SilentlyContinue
                $attempts++
                if ($attempts -ge 3) { throw "Download failed after 3 attempts: $_" }
                Write-Err "Retrying ($attempts/3)..."
                Start-Sleep 2
            }
        }
    }

    Write-Status "INSTALLING" "extracting Ollama..."
    Write-Step "Extracting to $installDir ..."
    Expand-Archive -Path $zipFile -DestinationPath $installDir -Force

    $path = Get-OllamaPath
    if (-not $path) { throw "Ollama not found after extract  -  check $installDir" }
    Write-Ok "Ollama ready at: $path"
    return $path
}

function Wait-OllamaReady([int]$port, [int]$timeoutSec = 60) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:$port/api/tags" `
                -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep 1
    }
    return $false
}

# -- coordinator comms ---------------------------------------------------------

function Get-AgentId {
    # Stable UUID persisted across restarts  -  used as the coordinator key so
    # machines behind the same NAT don't overwrite each other.
    $idFile = "$env:TEMP\swarm-agent-id.txt"
    if (Test-Path $idFile) {
        $id = (Get-Content $idFile -Raw).Trim()
        if ($id -match '^[0-9a-f-]{36}$') { return $id }
    }
    $id = [guid]::NewGuid().ToString()
    $id | Set-Content $idFile -Encoding UTF8
    return $id
}

function Register-WithCoordinator([string]$coordinator, [string]$apiKey, [int]$port, [string]$model, [hashtable]$hw) {
    $myIp = (
        Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.*" } |
        Select-Object -First 1
    ).IPAddress
    if (-not $myIp) { throw "Could not determine local IP" }

    Write-Step "Collecting system info..."
    $sysInfo = Get-SystemInfo $hw

    $body = @{
        agent_id       = $script:AgentId
        port           = $port      # ip is derived server-side from the source address
        model          = $model
        hostname       = $sysInfo.hostname
        cpu_name       = $sysInfo.cpu_name
        cpu_cores      = $sysInfo.cpu_cores
        ram_total_gb   = $sysInfo.ram_total_gb
        ram_free_gb    = $sysInfo.ram_free_gb
        gpu_name       = $sysInfo.gpu_name
        gpu_vram_gb    = $sysInfo.gpu_vram_gb
        network_mbps   = $sysInfo.network_mbps
        ollama_version = $sysInfo.ollama_version
        os_version     = $sysInfo.os_version
    } | ConvertTo-Json

    $headers = @{ "X-API-Key" = $apiKey }
    Write-Step "Registering $myIp`:$port with $coordinator ..."
    $resp = Invoke-RestMethod -Uri "$coordinator/register" `
        -Method Post -Body $body -ContentType "application/json" `
        -Headers $headers -ErrorAction Stop
    Write-Ok "Registered. Total machines: $($resp.machines_total)"
    return $myIp
}

function Test-CoordinatorReachable([string]$coordinator, [string]$apiKey) {
    # Step 1: connectivity  -  /healthz has no auth, confirms the server is up
    Write-Step "Connecting to coordinator at $coordinator ..."
    try {
        $r = Invoke-WebRequest -Uri "$coordinator/healthz" `
            -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop
        if ($r.StatusCode -ne 200) { throw "unexpected status $($r.StatusCode)" }
    } catch {
        Write-Err "Cannot reach coordinator at $coordinator"
        Write-Err "Check the URL and make sure the server is running."
        return $false
    }

    # Step 2: auth check  -  validate key against /agent-status before spending
    #         time installing Ollama or pulling a model
    Write-Step "Validating API key ..."
    try {
        $body = @{ phase = "CONNECTING"; detail = ""; agent_id = $script:AgentId; hostname = $env:COMPUTERNAME; model = $script:Model } | ConvertTo-Json
        Invoke-RestMethod -Uri "$coordinator/agent-status" `
            -Method Post -Body $body -ContentType "application/json" `
            -Headers @{ "X-API-Key" = $apiKey } `
            -TimeoutSec 8 -ErrorAction Stop | Out-Null
    } catch {
        $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        if ($code -eq 401) {
            Write-Err "API key rejected (401)  -  check the key you entered and try again."
        } else {
            Write-Err "Key validation failed: $_"
        }
        return $false
    }

    Write-Ok "Connected  -  key valid."
    return $true
}

function Write-Status([string]$phase, [string]$detail = "") {
    $ts  = (Get-Date).ToString("HH:mm:ss")
    $msg = if ($detail) { "[$ts] $phase  -  $detail" } else { "[$ts] $phase" }
    Write-Host "  $msg" -ForegroundColor DarkCyan
    $msg | Out-File -FilePath "$env:TEMP\swarm-agent-status.txt" -Encoding UTF8

    # Report to coordinator if we already have credentials
    if ($script:Coordinator -and $script:ApiKey) {
        try {
            $body = @{
                phase    = $phase
                detail   = $detail
                agent_id = $script:AgentId
                hostname = $env:COMPUTERNAME
                model    = $script:Model
            } | ConvertTo-Json
            Invoke-RestMethod -Uri "$script:Coordinator/agent-status" `
                -Method Post -Body $body -ContentType "application/json" `
                -Headers @{ "X-API-Key" = $script:ApiKey } `
                -ErrorAction SilentlyContinue | Out-Null
        } catch { }  # never let status reporting crash the agent
    }
}

function Invoke-InferWithMascot([int]$port, [string]$bodyJson, [string]$jobId = "", [string]$coordinator = "", [hashtable]$cancelHeaders = @{}) {
    $eyeFrames   = @("o . o","@ . @","o . o","> . <","o . o","^ . ^","o . o","* . *")
    $mouthFrames = @(" ___ "," --- "," ~~~ "," ... "," ### ")
    $spinners    = @("-","\","|","/")
    $thoughts    = @(
        "  crunching tokens   ",
        "  hot take incoming  ",
        "  brb, doing math    ",
        "  yes this is fast   ",
        "  GPU go brrrr       ",
        "  almost there...    ",
        "  big brain moment   "
    )

    # Run the HTTP call in a background job so we can animate while it waits.
    # Use Invoke-WebRequest + return raw Content string  -  avoids PS hashtable
    # serialisation bugs across job runspace boundaries (ConvertTo-Json on
    # complex PSObjects can throw "index out of bounds" on PS 5.1).
    # Prefix "__ERR__:" on failure so the string itself carries the signal.
    $inferJob = Start-Job -ScriptBlock {
        param($url, $body)
        try {
            $r = Invoke-WebRequest -Uri $url -Method Post -Body $body `
                -ContentType "application/json" -TimeoutSec 1800 `
                -UseBasicParsing -ErrorAction Stop
            return $r.Content   # raw JSON string  -  no PSObject serialisation
        } catch {
            return "__ERR__:$_"
        }
    } -ArgumentList "http://127.0.0.1:$port/api/chat", $bodyJson

    # Reserve 5 lines for the mascot box
    1..5 | ForEach-Object { Write-Host "" }
    $mascotTop = [Console]::CursorTop - 5
    [Console]::CursorVisible = $false

    $frame           = 0
    $t0              = [DateTime]::UtcNow
    $lastCancelCheck = [DateTime]::MinValue

    while ($inferJob.State -eq 'Running') {
        $elapsed = [math]::Floor(([DateTime]::UtcNow - $t0).TotalSeconds)
        $eye     = $eyeFrames[$frame   % $eyeFrames.Count]
        $mouth   = $mouthFrames[$frame % $mouthFrames.Count]
        $spin    = $spinners[$frame    % $spinners.Count]
        $think   = $thoughts[([int]($frame / 4)) % $thoughts.Count]

        [Console]::SetCursorPosition(0, $mascotTop)
        Write-Host "   .-----------.   " -ForegroundColor DarkCyan
        Write-Host "   |  ($eye)  |   " -ForegroundColor Yellow
        Write-Host "   |   $mouth   |   $spin  ${elapsed}s" -ForegroundColor DarkCyan
        Write-Host "   '-----------'   " -ForegroundColor DarkCyan
        Write-Host "  [$think]  " -ForegroundColor DarkGray

        # Poll coordinator for cancellation every ~5 seconds
        if ($jobId -and $coordinator -and ([DateTime]::UtcNow - $lastCancelCheck).TotalSeconds -ge 5) {
            $lastCancelCheck = [DateTime]::UtcNow
            try {
                $cr = Invoke-RestMethod -Uri "$coordinator/agent/jobs/$jobId/cancelled" `
                    -Method Get -Headers $cancelHeaders -TimeoutSec 3 -ErrorAction Stop
                if ($cr.cancelled) {
                    Stop-Job  $inferJob -ErrorAction SilentlyContinue
                    Remove-Job $inferJob -Force -ErrorAction SilentlyContinue
                    [Console]::SetCursorPosition(0, $mascotTop)
                    1..5 | ForEach-Object { Write-Host ("".PadRight(60)) }
                    [Console]::SetCursorPosition(0, $mascotTop)
                    [Console]::CursorVisible = $true
                    throw "Job $jobId cancelled by coordinator (consumer disconnected or timed out)"
                }
            } catch [System.Management.Automation.RuntimeException] {
                throw  # re-throw our own "cancelled" exception
            } catch { }  # network hiccup  -  keep running, coordinator will clean up
        }

        Start-Sleep -Milliseconds 150
        $frame++
    }

    # Clear mascot area
    [Console]::SetCursorPosition(0, $mascotTop)
    1..5 | ForEach-Object { Write-Host ("".PadRight(60)) }
    [Console]::SetCursorPosition(0, $mascotTop)
    [Console]::CursorVisible = $true

    $raw = Receive-Job $inferJob -Wait
    Remove-Job $inferJob -Force

    if ($raw -like "__ERR__:*") { throw ($raw -replace "^__ERR__:","") }
    # Return the raw JSON string  -  avoids PS 5.1's strict ConvertFrom-Json which rejects
    # valid-ish escape sequences (e.g. backslash-space) that some models produce in code.
    return $raw
}

# Pull a model from Ollama, streaming progress to the console and coordinator.
# Throws on failure. Safe to call at startup or on-demand from the work loop.
function Invoke-ModelPull([string]$modelName, [int]$port) {
    Write-Step "Pulling '$modelName' (instant if already cached)..."
    $pullJob = Start-Job -ScriptBlock {
        param($exe, $model, $port)
        $env:OLLAMA_HOST = "127.0.0.1:$port"
        & $exe pull $model 2>&1 | ForEach-Object { Write-Output "$_" }
        Write-Output "__EXIT:$LASTEXITCODE"
    } -ArgumentList $script:ollamaExe, $modelName, $port

    $lastReport = [DateTime]::MinValue
    $readIdx    = 0
    while ($pullJob.State -eq "Running") {
        Start-Sleep 5
        $all      = @(Receive-Job $pullJob -Keep 2>$null)
        $newLines = if ($all.Count -gt $readIdx) { $all[$readIdx..($all.Count - 1)] } else { @() }
        $readIdx  = $all.Count
        foreach ($l in $newLines) { if ($l -and $l -notmatch '^__EXIT:') { Write-Host $l } }
        $latest = $newLines | Where-Object { $_ -match '(\d+)%' } | Select-Object -Last 1
        if ($latest -and ([DateTime]::UtcNow - $lastReport).TotalSeconds -ge 5) {
            $null  = $latest -match '(\d+)%'
            $pct   = $Matches[1]
            $speed = if ($latest -match '([\d.]+ [MG]B/s)') { "  $($Matches[1])" } else { "" }
            Write-Status "DOWNLOADING" "${pct}%${speed}  -  $modelName"
            $lastReport = [DateTime]::UtcNow
        }
    }

    $allLines  = @(Receive-Job $pullJob)
    Remove-Job $pullJob -Force
    $allLines | Where-Object { $_ -notmatch '^__EXIT:' } | ForEach-Object { Write-Host $_ }

    $exitLine  = $allLines | Where-Object { $_ -match '^__EXIT:' } | Select-Object -Last 1
    $exitCode  = if ($exitLine) { [int]($exitLine -replace '^__EXIT:','') } else { 0 }
    $outputStr = ($allLines | Where-Object { $_ -notmatch '^__EXIT:' }) -join " "

    if ($exitCode -ne 0 -and $outputStr -notmatch '\bsuccess\b') {
        $tail = ($allLines | Where-Object { $_ -notmatch '^__EXIT:' } | Select-Object -Last 10) -join "`n"
        throw "Failed to pull '$modelName':`n$tail"
    }
    Write-Ok "Model '$modelName' ready."
}

# -- media helpers -------------------------------------------------------------

function Install-MediaDeps {
    Write-Step "Checking media generation dependencies (Python + diffusers)..."
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $py) { throw "Python 3.10+ required for media generation  -  install it and add to PATH" }
    Write-Step "Python: $($py.Source)"

    $pkgs = @(
        "torch --index-url https://download.pytorch.org/whl/cu121",
        "diffusers --upgrade",
        "transformers --upgrade",
        "accelerate --upgrade",
        "safetensors --upgrade",
        "huggingface_hub --upgrade",
        "imageio[ffmpeg]",
        "sentencepiece",
        "protobuf"
    )
    foreach ($pkg in $pkgs) {
        Write-Step "pip install $($pkg.Split(' ')[0]) ..."
        $pipArgs = @("-m","pip","install","--quiet") + $pkg.Split(" ")
        & python @pipArgs 2>&1 | Where-Object { $_ -match 'error' } | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }
    }
    Write-Ok "Media deps ready."
}

function Start-MediaWorkLoop([string]$coordinator, [string]$apiKey, [string]$agentId, [double]$vramGb, [string]$inferScript) {
    # Write the inference script to disk before Start-Job to avoid PS runspace
    # serialization limits on large strings passed via ArgumentList.
    $tmpDir  = Join-Path $env:TEMP "swarm-media"
    if (-not (Test-Path $tmpDir)) { New-Item -ItemType Directory $tmpDir | Out-Null }
    $inferPath = Join-Path $tmpDir "infer.py"
    $inferScript | Set-Content $inferPath -Encoding UTF8

    return Start-Job -ScriptBlock {
        param($coordinator, $apiKey, $agentId, $vramGb, $inferPath)

        $headers       = @{ "X-API-Key" = $apiKey }
        $depsInstalled = $false
        $tmpDir        = Split-Path $inferPath
        $aidEnc = [Uri]::EscapeDataString($agentId)

        # Report that media loop is alive and idle
        try {
            Invoke-RestMethod -Uri "$coordinator/agent/media/status?agent_id=$aidEnc&phase=IDLE" `
                -Method Post -Headers $headers -ErrorAction SilentlyContinue | Out-Null
        } catch { }

        while ($true) {
            try {
                $resp = Invoke-WebRequest `
                    -Uri "$coordinator/agent/media/jobs/next?vram_gb=$vramGb&agent_id=$aidEnc" `
                    -Method Get -Headers $headers -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop
                if ($resp.StatusCode -ne 200) { Start-Sleep 3; continue }

                $job     = $resp.Content | ConvertFrom-Json
                $jobId   = $job.id
                $jobType = $job.type
                $model   = $job.model
                $body    = $job.body_json | ConvertFrom-Json
                Write-Host "  [media] $jobType $($jobId.Substring(0,8))  -  $model"

                if (-not $depsInstalled) {
                    Write-Host "  [media] installing diffusers + torch (first job)..." -ForegroundColor Cyan
                    try {
                        Invoke-RestMethod -Uri "$coordinator/agent/media/status?agent_id=$aidEnc&phase=DOWNLOADING_DEPS&model=$([Uri]::EscapeDataString($model))" `
                            -Method Post -Headers $headers -ErrorAction SilentlyContinue | Out-Null
                    } catch { }
                    $pkgs = @(
                        @("torch", "--index-url", "https://download.pytorch.org/whl/cu121"),
                        @("diffusers", "--upgrade"), @("transformers", "--upgrade"),
                        @("accelerate", "--upgrade"), @("safetensors", "--upgrade"),
                        @("huggingface_hub", "--upgrade"),
                        @("imageio[ffmpeg]"), @("sentencepiece"), @("protobuf")
                    )
                    foreach ($pkg in $pkgs) { python -m pip install --quiet @pkg }
                    $depsInstalled = $true
                    Write-Host "  [media] deps ready." -ForegroundColor Green
                }

                try {
                    Invoke-RestMethod -Uri "$coordinator/agent/media/status?agent_id=$aidEnc&phase=RUNNING&model=$([Uri]::EscapeDataString($model))" `
                        -Method Post -Headers $headers -ErrorAction SilentlyContinue | Out-Null
                } catch { }

                $t0    = [DateTime]::UtcNow
                $outDir = Join-Path $tmpDir $jobId
                try {
                    if ($jobType -eq "image") {
                        New-Item -ItemType Directory $outDir -Force | Out-Null
                        $pyArgs = @("--type","image","--model",$model,
                                    "--prompt",$body.prompt,
                                    "--n","$($body.n)",
                                    "--size",$body.size,
                                    "--quality",$body.quality,
                                    "--out-dir",$outDir)
                        if ($body.negative_prompt) { $pyArgs += @("--neg",$body.negative_prompt) }

                        # Stream stdout to capture DOWNLOAD/PROGRESS lines in real time
                        $psi = New-Object System.Diagnostics.ProcessStartInfo
                        $psi.FileName = "python"; $psi.Arguments = ((@((Join-Path $tmpDir "infer.py")) + $pyArgs) | ForEach-Object { "`"$_`"" }) -join " "
                        $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.UseShellExecute = $false
                        $proc = [System.Diagnostics.Process]::Start($psi)
                        $outLines = [System.Collections.Generic.List[string]]::new()
                        while (-not $proc.StandardOutput.EndOfStream) {
                            $line = $proc.StandardOutput.ReadLine()
                            if ($line -match '^DOWNLOAD (\d+\.?\d*)/(\d+\.?\d*)') {
                                $dlMb = [double]$Matches[1]; $totalMb = [double]$Matches[2]
                                $pct = if ($totalMb -gt 0) { [math]::Floor($dlMb * 100 / $totalMb) } else { 0 }
                                try { Invoke-RestMethod -Uri "$coordinator/agent/media/jobs/$jobId/progress?job_type=$jobType" -Method Post -Headers $headers -Body (@{phase="downloading";downloaded_mb=$dlMb;total_mb=$totalMb;pct=$pct}|ConvertTo-Json) -ContentType "application/json" -TimeoutSec 5 | Out-Null } catch {}
                                Write-Host "  [media] image $($jobId.Substring(0,8)) download $([math]::Round($dlMb,0))/$([math]::Round($totalMb,0)) MB ($pct%)"
                            } elseif ($line -match '^PROGRESS (\d+)/(\d+)') {
                                $step = [int]$Matches[1]; $total = [int]$Matches[2]
                                $pct = [math]::Floor($step * 100 / $total)
                                try { Invoke-RestMethod -Uri "$coordinator/agent/media/jobs/$jobId/progress?job_type=$jobType" -Method Post -Headers $headers -Body (@{phase="generating";step=$step;total=$total;pct=$pct}|ConvertTo-Json) -ContentType "application/json" -TimeoutSec 5 | Out-Null } catch {}
                                Write-Host "  [media] image $($jobId.Substring(0,8)) step $step/$total ($pct%)"
                            } elseif ($line) { $outLines.Add($line) }
                        }
                        $stderrOut = $proc.StandardError.ReadToEnd(); $proc.WaitForExit()
                        if ($proc.ExitCode -ne 0) { throw "inference error: $stderrOut" }
                        $outPaths = @($outLines | Where-Object { $_ -and (Test-Path "$_") })
                        if ($outPaths.Count -eq 0) { throw "no output files produced" }
                        $outFile  = $outPaths[0]
                        $ms       = [math]::Round(([DateTime]::UtcNow - $t0).TotalMilliseconds)
                        $ext      = [IO.Path]::GetExtension($outFile).TrimStart(".")
                        $data     = [IO.File]::ReadAllBytes($outFile)
                        Invoke-RestMethod `
                            -Uri "$coordinator/agent/media/jobs/$jobId/result?job_type=image&ext=$ext&elapsed_ms=$ms" `
                            -Method Post -Body $data -ContentType "application/octet-stream" `
                            -Headers $headers -TimeoutSec 120 -ErrorAction Stop | Out-Null
                        Write-Host "  [media] image done $([math]::Round($ms/1000,1))s  $([math]::Round($data.Length/1MB,1)) MB"
                    } else {
                        $outFile  = Join-Path $tmpDir "$jobId.mp4"
                        $pyArgs   = @("--type","video","--model",$model,
                                      "--prompt",$body.prompt,
                                      "--duration","$($body.duration)",
                                      "--width","$($body.width)",
                                      "--height","$($body.height)",
                                      "--guidance-scale","$($body.guidance_scale)",
                                      "--steps","$($body.num_inference_steps)",
                                      "--fps","$($body.fps)",
                                      "--out-file",$outFile)
                        if ($body.negative_prompt) { $pyArgs += @("--neg",$body.negative_prompt) }

                        $psi = New-Object System.Diagnostics.ProcessStartInfo
                        $psi.FileName = "python"; $psi.Arguments = ((@((Join-Path $tmpDir "infer.py")) + $pyArgs) | ForEach-Object { "`"$_`"" }) -join " "
                        $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.UseShellExecute = $false
                        $proc = [System.Diagnostics.Process]::Start($psi)
                        while (-not $proc.StandardOutput.EndOfStream) {
                            $line = $proc.StandardOutput.ReadLine()
                            if ($line -match '^DOWNLOAD (\d+\.?\d*)/(\d+\.?\d*)') {
                                $dlMb = [double]$Matches[1]; $totalMb = [double]$Matches[2]
                                $pct = if ($totalMb -gt 0) { [math]::Floor($dlMb * 100 / $totalMb) } else { 0 }
                                try { Invoke-RestMethod -Uri "$coordinator/agent/media/jobs/$jobId/progress?job_type=$jobType" -Method Post -Headers $headers -Body (@{phase="downloading";downloaded_mb=$dlMb;total_mb=$totalMb;pct=$pct}|ConvertTo-Json) -ContentType "application/json" -TimeoutSec 5 | Out-Null } catch {}
                                Write-Host "  [media] video $($jobId.Substring(0,8)) download $([math]::Round($dlMb,0))/$([math]::Round($totalMb,0)) MB ($pct%)"
                            } elseif ($line -match '^PROGRESS (\d+)/(\d+)') {
                                $step = [int]$Matches[1]; $total = [int]$Matches[2]
                                $pct = [math]::Floor($step * 100 / $total)
                                try { Invoke-RestMethod -Uri "$coordinator/agent/media/jobs/$jobId/progress?job_type=$jobType" -Method Post -Headers $headers -Body (@{phase="generating";step=$step;total=$total;pct=$pct}|ConvertTo-Json) -ContentType "application/json" -TimeoutSec 5 | Out-Null } catch {}
                                Write-Host "  [media] video $($jobId.Substring(0,8)) step $step/$total ($pct%)"
                            }
                        }
                        $stderrOut = $proc.StandardError.ReadToEnd(); $proc.WaitForExit()
                        if ($proc.ExitCode -ne 0) { throw "inference error: $stderrOut" }
                        $ms   = [math]::Round(([DateTime]::UtcNow - $t0).TotalMilliseconds)
                        $data = [IO.File]::ReadAllBytes($outFile)
                        Invoke-RestMethod `
                            -Uri "$coordinator/agent/media/jobs/$jobId/result?job_type=video&ext=mp4&elapsed_ms=$ms" `
                            -Method Post -Body $data -ContentType "application/octet-stream" `
                            -Headers $headers -TimeoutSec 600 -ErrorAction Stop | Out-Null
                        Write-Host "  [media] video done $([math]::Round($ms/1000,1))s  $([math]::Round($data.Length/1MB,1)) MB"
                    }
                } catch {
                    $err = "$_"
                    Write-Host "  [media] job $jobId failed: $err"
                    try {
                        Invoke-RestMethod `
                            -Uri "$coordinator/agent/media/jobs/$jobId/error?job_type=$jobType" `
                            -Method Post -Body (@{error=$err}|ConvertTo-Json) `
                            -ContentType "application/json" -Headers $headers -ErrorAction SilentlyContinue | Out-Null
                    } catch { }
                }
                # Back to idle after each job (success or failure)
                try {
                    Invoke-RestMethod -Uri "$coordinator/agent/media/status?agent_id=$aidEnc&phase=IDLE" `
                        -Method Post -Headers $headers -ErrorAction SilentlyContinue | Out-Null
                } catch { }
            } catch {
                $errMsg = "$_"
                Write-Host "  [media] loop error: $errMsg" -ForegroundColor Red
                Start-Sleep 3
            }
        }
    } -ArgumentList $coordinator, $apiKey, $agentId, $vramGb, $inferPath
}

function Start-WorkLoop([string]$coordinator, [string]$apiKey, [string]$ip, [int]$port, [double]$vramGb) {
    $headers    = @{ "X-API-Key" = $apiKey }
    $lastHB     = [DateTime]::MinValue
    $vramLimit  = if ($vramGb -le 0) { 8.0 } else { $vramGb - 1.0 }  # 1 GB headroom for OS/driver

    # Pre-populate from ollama list so models already on disk skip the download step
    $pulledModels = @{}
    try {
        $env:OLLAMA_HOST = "127.0.0.1:$port"
        $listOut = & $script:ollamaExe list 2>&1 | Select-Object -Skip 1
        foreach ($line in $listOut) {
            $name = ($line -split '\s+')[0]
            if ($name) { $pulledModels[$name] = $true }
        }
        if ($pulledModels.Count -gt 0) {
            Write-Step "Already on disk: $($pulledModels.Keys -join ', ')"
        }
    } catch { }

    Write-Status "RUNNING" "accepting any model that fits in $vramGb GB VRAM  -  Ctrl+C to quit"
    while ($true) {
        # Restart media job if it exited unexpectedly
        if ($script:mediaJob) {
            $ms = $script:mediaJob.State
            if ($ms -ne "Running" -and $ms -ne "NotStarted") {
                Receive-Job $script:mediaJob 2>$null | ForEach-Object { Write-Host "  [media] $_" }
                Remove-Job $script:mediaJob -Force -ErrorAction SilentlyContinue
                Write-Host "  [media] loop exited ($ms), restarting..." -ForegroundColor Yellow
                $script:mediaJob = Start-MediaWorkLoop $coordinator $apiKey $script:AgentId $script:VramGb $script:MEDIA_INFER_PY
            }
        }

        # heartbeat every 15s  -  re-register automatically if coordinator restarted
        if (([DateTime]::UtcNow - $lastHB).TotalSeconds -ge 15) {
            try {
                Invoke-WebRequest -Uri "$coordinator/heartbeat" -Method Post `
                    -Body (@{ port = $port; agent_id = $script:AgentId } | ConvertTo-Json) `
                    -ContentType "application/json" `
                    -Headers $headers -UseBasicParsing -ErrorAction Stop | Out-Null
            } catch {
                $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
                if ($status -eq 404) {
                    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Coordinator restarted, re-registering..." -ForegroundColor Yellow
                    try { Register-WithCoordinator $coordinator $apiKey $port $script:Model $script:hw } catch { }
                }
            }
            $lastHB = [DateTime]::UtcNow
        }

        # Ask coordinator which models have jobs waiting right now, then filter to what fits our VRAM.
        # This replaces a static catalog  -  any model the coordinator knows about is fair game.
        $candidates = @()
        try {
            $qResp = Invoke-RestMethod -Uri "$coordinator/agent/jobs/queued-models" `
                -Method Get -Headers $headers -ErrorAction Stop
            $candidates = @($qResp.models | Where-Object { (Get-ModelVram $_) -le $vramLimit })
        } catch { }

        $servedJob = $false
        foreach ($model in $candidates) {
            $modelEnc = [Uri]::EscapeDataString($model)

            # Download model if not local yet
            if (-not $pulledModels.ContainsKey($model)) {
                Write-Status "DOWNLOADING" "demand detected  -  pulling $model"
                try {
                    Invoke-ModelPull $model $port
                    $pulledModels[$model] = $true
                    $script:Model = $model
                    try { Register-WithCoordinator $coordinator $apiKey $port $model $script:hw } catch { }
                } catch {
                    $errMsg = "Pull failed for ${model}: $_"
                    Write-Err $errMsg
                    # Surface download failures in coordinator agent-status so they're visible remotely
                    try {
                        $statusBody = @{
                            phase    = "RUNNING"
                            detail   = $errMsg
                            agent_id = $script:AgentId
                            hostname = $env:COMPUTERNAME
                            model    = $model
                        } | ConvertTo-Json
                        Invoke-RestMethod -Uri "$coordinator/agent-status" -Method Post `
                            -Body $statusBody -ContentType "application/json" `
                            -Headers $headers -ErrorAction SilentlyContinue | Out-Null
                    } catch { }
                    continue
                }
            }

            # Consume and serve the job
            try {
                $agentIdEnc = [System.Uri]::EscapeDataString($script:AgentId)
                $resp = Invoke-WebRequest -Uri "$coordinator/agent/jobs/next?model=$modelEnc&agent_id=$agentIdEnc" `
                    -Method Get -Headers $headers -UseBasicParsing -ErrorAction Stop
                if ($resp.StatusCode -ne 200) { continue }

                $job         = $resp.Content | ConvertFrom-Json
                $jobId       = $job.id
                $jobBodyJson = $job.body_json
                Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Job $jobId  model=$model" -ForegroundColor DarkCyan

                $t0 = [DateTime]::UtcNow
                try {
                    $rawResult = Invoke-InferWithMascot -port $port -bodyJson $jobBodyJson -jobId $jobId -coordinator $coordinator -cancelHeaders $headers
                    $ms        = [math]::Round(([DateTime]::UtcNow - $t0).TotalMilliseconds, 1)
                    $postBody  = "{`"result`":$rawResult,`"elapsed_ms`":$ms}"
                    Invoke-RestMethod -Uri "$coordinator/agent/jobs/$jobId/result" `
                        -Method Post -ContentType "application/json" `
                        -Body $postBody `
                        -Headers $headers -ErrorAction SilentlyContinue | Out-Null
                    Write-Ok "Job $jobId done in $([math]::Round($ms / 1000, 1))s"
                } catch {
                    $err = "$_"
                    Write-Err "Job $jobId failed: $err"
                    try {
                        Invoke-RestMethod -Uri "$coordinator/agent/jobs/$jobId/result" `
                            -Method Post -ContentType "application/json" `
                            -Body (@{ error = $err } | ConvertTo-Json) `
                            -Headers $headers -ErrorAction SilentlyContinue | Out-Null
                    } catch { }
                }

                $servedJob = $true
                break
            } catch {
                # poll error  -  silently skip
            }
        }

        if (-not $servedJob) { Start-Sleep 2 }
    }
}

# -- interactive wizard --------------------------------------------------------

function Start-Wizard {
    Show-Banner

    Write-Host "  Detecting hardware..." -ForegroundColor DarkCyan
    $hw = Get-HardwareQuick

    # Show detected specs
    Write-Host ""
    Write-Host "  Detected hardware:" -ForegroundColor Yellow
    Write-Host "    CPU  : $($hw.cpu_name)"
    Write-Host "    RAM  : $($hw.ram_gb) GB"
    if ($hw.vram_gb -gt 0) {
        Write-Host "    GPU  : $($hw.gpu_name) ($($hw.vram_gb) GB VRAM)" -ForegroundColor Green
    } else {
        Write-Host "    GPU  : none detected  (CPU inference)" -ForegroundColor DarkYellow
    }
    Write-Host ""

    # Model selection
    # Auto-select default model based on VRAM (downloaded at startup; others pulled on demand)
    $modelIds = @(
        "qwen3:8b",
        "gemma4:12b",
        "qwen3:14b",
        "devstral-small-2:24b",
        "qwen3.6:27b",
        "qwen3:30b-a3b"
    )
    $recIdx   = Get-RecommendedModelIndex $hw.vram_gb
    $chosenId = $modelIds[$recIdx]
    Write-Host ""
    Write-Host "  Default model  : $chosenId  (auto-selected for $($hw.vram_gb) GB VRAM)" -ForegroundColor Green
    Write-Host "  Other models   : pulled on demand when a job requests them" -ForegroundColor DarkGray
    Write-Host ""

    # Coordinator URL
    Write-Host ""
    Write-Host "  Coordinator URL (e.g. https://1.2.3.4:8443): " -NoNewline -ForegroundColor Yellow
    $coordUrl = (Read-Host).Trim().TrimEnd("/")

    # API key (masked)
    $apiKey = Read-MaskedInput "API Key: "

    # Local Ollama port
    Write-Host ""
    $portChoices = @(
        "11434  (default)",
        "11435",
        "11436",
        "11437"
    )
    $portValues = @(11434, 11435, 11436, 11437)
    $portChosen = Show-ChoiceList "Local Ollama port (use a different one if 11434 is in use):" $portChoices 0
    $ollamaPort = $portValues[$portChoices.IndexOf($portChosen)]

    Write-Host ""
    # Expose credentials to Write-Status before returning so the first report fires
    $script:Coordinator = $coordUrl
    $script:ApiKey      = $apiKey
    $script:Model       = $chosenId
    return @{
        Model       = $chosenId
        Coordinator = $coordUrl
        ApiKey      = $apiKey
        OllamaPort  = $ollamaPort
        Hw          = $hw
    }
}

# -- main ----------------------------------------------------------------------

# Stable UUID  -  generated once, persisted across restarts, unique per machine
# even if multiple machines share the same hostname or NAT IP.
$script:AgentId = Get-AgentId

# If any required param is missing, run the wizard
$interactive = ($Coordinator -eq "" -or $ApiKey -eq "" -or $Model -eq "")

if ($interactive) {
    $cfg = Start-Wizard
    $Coordinator = $cfg.Coordinator
    $ApiKey      = $cfg.ApiKey
    $Model       = $cfg.Model
    $OllamaPort  = $cfg.OllamaPort
    $hw          = $cfg.Hw
} else {
    $hw = Get-HardwareQuick
    Show-Banner
    Write-Host "  Model: $Model  |  Coordinator: $Coordinator" -ForegroundColor Yellow
    Write-Host ""
}

# 1. Verify coordinator is reachable AND API key is valid before doing anything else
if (-not (Test-CoordinatorReachable $Coordinator $ApiKey)) {
    exit 1
}

# 2. Ensure Ollama is present
$ollamaExe = Get-OllamaPath
if (-not $ollamaExe) {
    $ollamaExe = Install-Ollama
} else {
    Write-Ok "Ollama found at: $ollamaExe"
}

# 3. Kill any existing Ollama on this port
$existing = Get-NetTCPConnection -LocalPort $OllamaPort -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Step "Stopping existing Ollama on port $OllamaPort ..."
    $ownPid = (Get-NetTCPConnection -LocalPort $OllamaPort -State Listen).OwningProcess
    if ($ownPid) { Stop-Process -Id $ownPid -Force -ErrorAction SilentlyContinue }
    Start-Sleep 2
}

# 4. Start ollama serve (all interfaces)
Write-Status "STARTING" "Ollama server on port $OllamaPort"
$env:OLLAMA_HOST            = "0.0.0.0:$OllamaPort"
$env:OLLAMA_FLASH_ATTENTION = "1"              # ~20-40% faster on supported GPUs
$env:OLLAMA_KV_CACHE_TYPE   = "q8_0"          # quantize KV cache: ~50% less VRAM, no quality loss
$ollamaParallel = Get-OllamaParallel $hw.vram_gb $Model
$env:OLLAMA_NUM_PARALLEL    = "$ollamaParallel"  # concurrent requests sharing loaded weights
Write-Step "Parallel slots: $ollamaParallel (VRAM: $($hw.vram_gb) GB, model: $Model)"
$serverProc = Start-Process -FilePath $ollamaExe `
    -ArgumentList "serve" -PassThru -WindowStyle Hidden

Write-Step "Waiting for Ollama to be ready..."
if (-not (Wait-OllamaReady $OllamaPort 60)) {
    throw "Ollama did not start within 60 seconds"
}
Write-Ok "Ollama up (PID $($serverProc.Id))"

# 5. Network speed test  -  run before pull so coordinator stores the result
Write-Step "Measuring network speed (5 MB test)..."
$hw.network_mbps = Measure-NetworkSpeed
if ($hw.network_mbps) {
    Write-Ok "Network speed: $($hw.network_mbps) MB/s"
} else {
    Write-Step "Speed test failed  -  skipping"
}

# 6. Pull default model (others are pulled on demand when a job arrives)
$script:ollamaExe = $ollamaExe   # expose to Invoke-ModelPull
if (-not $SkipModelPull) {
    $speedTag = if ($hw.network_mbps) { "$($hw.network_mbps) MB/s  |  " } else { "" }
    Write-Status "DOWNLOADING" "${speedTag}starting pull  -  $Model"
    Invoke-ModelPull $Model $OllamaPort
}

# 7. Register
$script:hw = $hw   # make available to work loop for re-registration
Write-Status "REGISTERING" ""
$myIp = Register-WithCoordinator $Coordinator $ApiKey $OllamaPort $Model $hw

# 8. Media loop (always on if GPU present; deps install lazily on first job)
$script:mediaJob = $null
$script:VramGb   = $hw.vram_gb
if ($hw.vram_gb -gt 0) {
    $script:mediaJob = Start-MediaWorkLoop $Coordinator $ApiKey $script:AgentId $hw.vram_gb $script:MEDIA_INFER_PY
    Write-Ok "Media loop ready ($($hw.vram_gb) GB VRAM)  -  deps install on first job"
}

# 9. Work loop  -  dynamically discovers queued models from coordinator and pulls on demand
Start-WorkLoop $Coordinator $ApiKey $myIp $OllamaPort $hw.vram_gb
