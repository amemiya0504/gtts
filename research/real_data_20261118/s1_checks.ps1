Set-Location $env:GTTS_REPO
$py = Join-Path $PSScriptRoot "run_s1_real.py"
$runs = @(
  @{name="base k20"; H="20"; F="25"; S="1"; HO="0"; end="2026-09-04"},
  @{name="k10"; H="10"; F="25"; S="1"; HO="0"; end="2026-09-04"},
  @{name="k40"; H="40"; F="25"; S="1"; HO="0"; end="2026-09-04"},
  @{name="k20 fee50 slip2"; H="20"; F="50"; S="2"; HO="0"; end="2026-09-04"},
  @{name="k20 fee100 slip2"; H="20"; F="100"; S="2"; HO="0"; end="2026-09-04"},
  @{name="HOLDOUT k20 frozen"; H="20"; F="25"; S="1"; HO="1"; end="2026-11-17"}
)
foreach ($r in $runs) {
  "######## $($r.name)"
  $env:S1_HORIZON=$r.H; $env:S1_FEE=$r.F; $env:S1_SLIP=$r.S; $env:S1_HOLDOUT=$r.HO
  uv run --locked python $py $r.end 2>&1 | Select-String -Pattern '^(variant|institutional_net|   |compare|Traceback|\w+Error)' | ForEach-Object { $_.Line }
}
