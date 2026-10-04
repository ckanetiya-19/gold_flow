# =====================================================================
# start_all.ps1 - launches the whole Goldflow real-spot ecosystem.
# Registered in Task Scheduler to run AT LOGON, so everything comes back
# after a PC shutdown/restart. Safe to re-run by hand (kills + relaunches).
#
# SAFETY: MT5 execution is forced OFF here (the MT5 terminal is on a REAL
# account). These run as PAPER only. Real-money trading is never auto-started.
# =====================================================================
$base = 'C:\Users\ckane\Desktop\claud order flow'

# --- persisted iTick keys (User scope) into this session ---
foreach ($n in ([Environment]::GetEnvironmentVariables('User').Keys | Where-Object { $_ -like 'GOLDFLOW_*' })) {
    Set-Item -Path "Env:$n" -Value ([Environment]::GetEnvironmentVariable($n,'User'))
}
# --- MT5 DEMO execution for the VWAP-Crossover ports (9060/9080/9900) + 9900 Zones ---
# The per-port demo-guard BLOCKS every order/modify/close if the MT5 terminal is
# ever on a REAL account, so this can only ever trade the demo. Distinct magics:
# 9060=90601, 9080=90801, 9900 vwapx=90901, 9900 zones=90902.
# MT5 execution OFF (user stopped demo trading). Flip both to '1' to re-arm
# demo auto-execution (demo-guard blocks real accounts). Magics: 9060=90601,
# 9080=90801, 9900 vwapx=90901, 9900 zones=90902.
$env:GOLDFLOW_MT5_TRADING_ENABLED   = '0'
$env:GOLDFLOW_VWAPCROSS_MT5_ENABLED = '0'
$env:GOLDFLOW_MT5_SYMBOL            = 'XAUUSD'
# 9100 stays PAPER (user's choice); legacy extra strategies stay off.
$env:GOLDFLOW_9100_MT5_ENABLED      = '0'
$env:GOLDFLOW_PORTFOLIO_ENABLED     = '0'
$env:GOLDFLOW_VWAP_FC_ENABLED       = '0'
$env:GOLDFLOW_STRATEGIES_ENABLED    = '0'
$env:GOLDFLOW_QUESTDB_ENABLED       = '0'

function Start-Port($name, $dir, $script) {
    # kill any existing instance of this script (idempotent re-run)
    $c = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*$script*" }
    if ($c) { $c | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }
    Start-Sleep -Milliseconds 400
    $logbase = [System.IO.Path]::GetFileNameWithoutExtension($script)
    $out = Join-Path $dir "$logbase.out.log"
    $err = Join-Path $dir "$logbase.err.log"
    Start-Process python -ArgumentList $script -WorkingDirectory $dir -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
    Write-Output "started $name  ($script)"
}

# 1) Shared iTick feed hub FIRST (ports subscribe to it)
Start-Port 'feed-hub (9200)' $base 'feed_hub.py'
Start-Sleep -Seconds 6   # let the hub establish its iTick connection

# 2) The four terminals
Start-Port '9060' "$base\quant_9060" 'PORT_9060_V2_INSTITUTIONAL_DASHBOARD_V2.py'
Start-Port '9080' "$base\quant_9080" 'PORT_9080_AI_QUANT_TERMINAL_V2.py'
Start-Port '9100' "$base\zones_9100" 'PORT_9100_ORDERFLOW_ZONES_CHART.py'
Start-Port '9900' "$base\spot_9900" 'PORT_9900_ITICK_ORDERFLOW_TERMINAL.py'

# 3) Chart mirrors (read from their parent port)
Start-Port '9061 (mirror)' "$base\quant_9060" 'PORT_9061_INSTITUTIONAL_FULLSCREEN_CHART.py'
Start-Port '9081 (mirror)' "$base\quant_9080" 'PORT_9081_QUANT_FULLSCREEN_CHART.py'

Write-Output "Goldflow ecosystem launched (real-spot iTick; MT5 DEMO on for 9060/9080/9900, demo-guarded; 9100 paper)."
