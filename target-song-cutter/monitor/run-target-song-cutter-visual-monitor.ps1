param(
  [string]$OutputRoot = 'D:\切歌skill\target-song-cutter-outputs-20260818',
  [int]$RefreshSec = 5
)

$ErrorActionPreference = 'SilentlyContinue'
$htmlPath = Join-Path $PSScriptRoot 'target-song-cutter-live.html'
$logPath  = Join-Path $PSScriptRoot 'monitor-debug.log'

$RefreshSec = [math]::Max(1, $RefreshSec)

function Get-Snapshot {
  param([string]$Root)
  if(-not (Test-Path $Root)){
    return [pscustomobject]@{
      at = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
      tasks_total = 0; tasks_done = 0; tasks_running = 0; tasks_pending = 0; python_processes = 0;
      tasks = @();
      processes = @()
    }
  }

  $items = Get-ChildItem -Path $Root -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -notin @('test') }

  $procs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -like '*target_song_cutter.py*'
  }

  $rows = foreach($dir in $items){
    $seg = Test-Path (Join-Path $dir.FullName 'segments.csv')
    $clipDir = Join-Path $dir.FullName 'clips'
    $clipCount = if(Test-Path $clipDir){ (Get-ChildItem $clipDir -File -ErrorAction SilentlyContinue | Measure-Object).Count } else { 0 }
    $log = Get-ChildItem $dir.FullName -File -Filter 'run-*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $workDirs = Get-ChildItem $dir.FullName -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'target-song-cutter-*' }
    $active = ($procs | Where-Object { $_.CommandLine -like ('*' + $dir.Name + '*') }).Count
    $state = if($seg){'done'} elseif($active -gt 0){'running'} elseif($workDirs){'processing'} else {'pending'}
    [PSCustomObject]@{
      name = $dir.Name
      state = $state
      clip_count = $clipCount
      log_last = if($log){ $log.LastWriteTime.ToString('HH:mm:ss') } else { '--' }
      active = $active
    }
  }

  $queue = @()
  foreach($p in $procs){
    $queue += [PSCustomObject]@{
      pid = $p.ProcessId
      cmd = $p.CommandLine
    }
  }

  [pscustomobject]@{
    at = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    tasks_total = $rows.Count
    tasks_done = ($rows | Where-Object { $_.state -eq 'done' }).Count
    tasks_running = ($rows | Where-Object { $_.state -eq 'running' }).Count
    tasks_pending = ($rows | Where-Object { $_.state -eq 'pending' }).Count
    python_processes = $procs.Count
    tasks = $rows
    processes = $queue
  }
}

function Render-HTML {
  param([psobject]$Data)
  if($null -eq $Data){
    return "<!doctype html><html><body><h1>target-song-cutter monitor</h1><p>No snapshot yet</p></body></html>"
  }

  $tasksTotal = [int]($Data.tasks_total)
  $tasksDone = [int]($Data.tasks_done)
  $tasksRunning = [int]($Data.tasks_running)
  $tasksPending = [int]($Data.tasks_pending)
  $procCount = [int]($Data.python_processes)

  $rowsHtml = @()
  foreach($t in @($Data.tasks)){
    $cls = if($t.state -eq 'done'){'ok'} elseif($t.state -eq 'running'){'run'} else {'wait'}
    $statusCn = if($t.state -eq 'done'){'DONE'} elseif($t.state -eq 'running'){'RUNNING'} else {'WAIT'}
    $rowsHtml += "<tr class='$cls'><td>$($t.name)</td><td>$statusCn</td><td>$($t.clip_count)</td><td>$($t.active)</td><td>$($t.log_last)</td></tr>"
  }

  $procHtml = @()
  foreach($p in @($Data.processes)){
    $name = if($p.cmd -match '1727071052'){ 'Viridis/210030' } elseif($p.cmd -match '165257-757'){ 'YuJiu/165257' } else { 'Other' }
    $procHtml += "<li><b>PID $($p.pid)</b> - $name<br><code>$($p.cmd)</code></li>"
  }
  if($procHtml.Count -eq 0){ $procHtml = @('<li>No active target_song_cutter process</li>') }

  $barPct = [int](($tasksDone / [math]::Max($tasksTotal,1))*100)
  $barPct = [math]::Min(100, [math]::Max(0, $barPct))

  @"
<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="$RefreshSec"><title>target-song-cutter monitor</title>
<style>
body{font-family:Segoe UI,Arial,sans-serif;background:#0b1020;color:#e9ecff;margin:24px}
h1{margin-bottom:8px} .card{background:#151c33;border:1px solid #2a3560;border-radius:12px;padding:16px;margin-bottom:14px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #2a3560;padding:8px;text-align:left;font-size:14px}
th{background:#243057}
.ok{background:#16351f}.run{background:#2d2a14}.wait{background:#20293d}
.badge{display:inline-block;background:#2d3f7a;padding:4px 10px;border-radius:999px;margin-right:6px}
</style></head>
<body>
  <h1>target-song-cutter Process Monitor</h1>
  <div class="card">
    <div><span class="badge">Updated: $($Data.at)</span><span class="badge">Total: $tasksTotal</span><span class="badge">Done: $tasksDone</span><span class="badge">Running: $tasksRunning</span><span class="badge">Waiting: $tasksPending</span><span class="badge">python: $procCount</span></div>
    <div style="margin-top:10px; background:#12192c;height:22px;border:1px solid #2a3560;border-radius:12px;overflow:hidden">
      <div style="height:100%; width:$barPct%; background:#52a86f"></div>
    </div>
  </div>
  <div class="card">
    <table>
      <tr><th>Record Folder</th><th>Status</th><th>clips</th><th>Active Proc</th><th>Last Log Time</th></tr>
      $($rowsHtml -join "`n")
    </table>
  </div>
  <div class="card">
    <h3>Running Process</h3>
    <ul>$($procHtml -join "`n")</ul>
  </div>
</body></html>
"@
}

while($true){
  try {
    $snapshot = Get-Snapshot -Root $OutputRoot
    $html = Render-HTML -Data $snapshot
  } catch {
    $err = $_.Exception.Message
    Add-Content -Path $logPath -Value ("{0} ERROR {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $err)
    $snapshot = [pscustomobject]@{tasks_done = -1; tasks_total = -1; python_processes = 0}
    $html = "<!doctype html><html><body><h1>target-song-cutter monitor</h1><pre>Error: $err</pre></body></html>"
  }
  Set-Content -Path $htmlPath -Value $html -Encoding UTF8

  if($snapshot.tasks_done -ge $snapshot.tasks_total -and $snapshot.python_processes -eq 0){
    break
  }
  Start-Sleep -Seconds $RefreshSec
}
