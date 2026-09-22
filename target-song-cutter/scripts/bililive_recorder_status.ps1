$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Result($payload) {
    $payload | ConvertTo-Json -Depth 6 -Compress
}

try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class BililiveRecorderWindowFinder {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr extraData);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int maxCount);
    public static List<IntPtr> ForProcess(uint target) {
        var result = new List<IntPtr>();
        EnumWindows((h, l) => {
            uint pid;
            GetWindowThreadProcessId(h, out pid);
            if (pid == target) result.Add(h);
            return true;
        }, IntPtr.Zero);
        return result;
    }
    public static string Title(IntPtr h) {
        var buffer = new StringBuilder(512);
        GetWindowText(h, buffer, buffer.Capacity);
        return buffer.ToString();
    }
}
'@

    $process = Get-Process -Name 'BililiveRecorder.WPF' -ErrorAction SilentlyContinue |
        Sort-Object StartTime | Select-Object -First 1
    if (-not $process) {
        Write-Result ([pscustomobject]@{
            available = $false
            source = 'mikufans-wpf-ui'
            reason = 'process-not-running'
            rooms = @()
        })
        exit 0
    }

    $root = $null
    $windowTitle = ''
    foreach ($handle in [BililiveRecorderWindowFinder]::ForProcess([uint32]$process.Id)) {
        $title = [BililiveRecorderWindowFinder]::Title($handle)
        if ($title -notmatch 'BiliRec|BililiveRecorder|mikufans') { continue }
        try {
            $candidate = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
            if ($candidate) {
                $root = $candidate
                $windowTitle = $title
                break
            }
        } catch {}
    }
    if (-not $root) {
        Write-Result ([pscustomobject]@{
            available = $false
            source = 'mikufans-wpf-ui'
            reason = 'window-not-readable'
            process_id = $process.Id
            rooms = @()
        })
        exit 0
    }

    $customCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Custom
    )
    $cards = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        $customCondition
    )
    $rooms = @()
    for ($cardIndex = 0; $cardIndex -lt $cards.Count; $cardIndex++) {
        $card = $cards.Item($cardIndex)
        $children = $card.FindAll(
            [System.Windows.Automation.TreeScope]::Children,
            [System.Windows.Automation.Condition]::TrueCondition
        )
        if ($children.Count -lt 9) { continue }
        $roomIndex = -1
        for ($index = 0; $index -lt $children.Count; $index++) {
            if ($children.Item($index).Current.Name -match '^\d{5,}$') {
                $roomIndex = $index
                break
            }
        }
        if ($roomIndex -lt 2) { continue }

        $roomId = $children.Item($roomIndex).Current.Name
        $name = $children.Item(0).Current.Name
        $title = $children.Item(1).Current.Name
        $area = if ($children.Count -gt 4) {
            (($children.Item(2).Current.Name, $children.Item(4).Current.Name) |
                Where-Object { $_ } | Select-Object -Unique) -join ' / '
        } else { '' }
        $viewerCount = ''
        if ($roomIndex + 1 -lt $children.Count) {
            $viewerCount = $children.Item($roomIndex + 1).Current.Name
        }
        $status = ''
        $bitrate = ''
        for ($index = $roomIndex + 1; $index -lt $children.Count; $index++) {
            $value = $children.Item($index).Current.Name.Trim()
            if (-not $bitrate -and $value -match '(?i)^\d+(?:\.\d+)?\s*Mbps$') {
                $bitrate = $value
                continue
            }
            if (
                -not $status -and $value -and
                $value -notmatch '^\d+$' -and
                $value -notmatch '^(?i:REC)$' -and
                $value -notmatch '(?i)Mbps$'
            ) {
                $status = $value
            }
        }
        $rate = 0.0
        if ($bitrate -match '(?i)^(\d+(?:\.\d+)?)\s*Mbps$') {
            $rate = [double]$Matches[1]
        }
        $isLive = $rate -gt 0.01 -or $status -match '(?i)recording|streaming|(^|\s)live($|\s)'
        $rooms += [pscustomobject]@{
            room_id = $roomId
            name = $name
            title = $title
            area = $area
            viewer_count = $viewerCount
            status = $status
            bitrate = $bitrate
            is_live = [bool]$isLive
        }
    }

    Write-Result ([pscustomobject]@{
        available = $true
        source = 'mikufans-wpf-ui'
        reason = ''
        process_id = $process.Id
        window_title = $windowTitle
        rooms = @($rooms)
    })
} catch {
    Write-Result ([pscustomobject]@{
        available = $false
        source = 'mikufans-wpf-ui'
        reason = 'read-error'
        error = $_.Exception.Message
        rooms = @()
    })
    exit 0
}
