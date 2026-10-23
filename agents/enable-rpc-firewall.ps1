#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
Allow the private RPC worker port from exactly one primary machine.
Run on the worker in administrator PowerShell. Use -Remove to undo the rule.
#>
param(
    [Parameter(Mandatory=$true)]
    [string]$PrimaryAddress,
    [Parameter(Mandatory=$true)]
    [string]$WorkerAddress,
    [ValidateRange(50052, 50055)]
    [int]$RpcPort = 50052,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

function Assert-PrivateIPv4([string]$Address) {
    $parsed = [System.Net.IPAddress]::Parse($Address)
    if ($parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw "RPC requires a private IPv4 address: $Address"
    }
    $bytes = $parsed.GetAddressBytes()
    $private = $bytes[0] -eq 10 -or
        ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
    if (-not $private) { throw "RPC address is outside the private ranges: $Address" }
}

Assert-PrivateIPv4 $PrimaryAddress
Assert-PrivateIPv4 $WorkerAddress
if ($PrimaryAddress -eq $WorkerAddress) { throw "Primary and worker addresses must differ" }

$ruleName = "PeerFoundry-RPC-$RpcPort-from-$PrimaryAddress"
$existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existing) { $existing | Remove-NetFirewallRule }
if ($Remove) {
    Write-Host "Removed $ruleName"
    exit 0
}

New-NetFirewallRule -Name $ruleName `
    -DisplayName "PeerFoundry RPC $RpcPort from $PrimaryAddress" `
    -Direction Inbound -Action Allow -Protocol TCP -Enabled True `
    -LocalAddress $WorkerAddress -LocalPort $RpcPort `
    -RemoteAddress $PrimaryAddress -Profile Any | Out-Null

Write-Host "Allowed ${PrimaryAddress} -> ${WorkerAddress}:${RpcPort}"
Write-Host "The RPC worker must be running before a TCP connectivity test can pass."
