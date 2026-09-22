param(
    [Parameter(Mandatory = $true)]
    [string]$AssFile,
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [double]$MaxSeconds = 32.0,
    [string]$FontName = 'AR WeiBeiGBStd BD'
)

$ErrorActionPreference = 'Stop'

function Convert-AssTimeToSeconds([string]$value) {
    $parts = $value.Split(':')
    return ([double]$parts[0] * 3600.0) + ([double]$parts[1] * 60.0) + [double]$parts[2]
}

function Convert-SecondsToAssTime([double]$value) {
    if ($value -lt 0) { $value = 0 }
    $hours = [math]::Floor($value / 3600)
    $minutes = [math]::Floor(($value % 3600) / 60)
    $seconds = $value % 60
    return ('{0}:{1:00}:{2:00.00}' -f [int]$hours, [int]$minutes, [double]$seconds)
}

function Add-PopFrame(
    $slide,
    [string]$text,
    [int]$visibleThrough,
    [int]$currentPosition,
    [double]$currentSize,
    [string]$imagePath
) {
    $shape = $slide.Shapes.AddTextbox(1, 0, 0, 960, 101.25)
    $shape.Fill.Visible = 0
    $shape.Line.Visible = 0
    $shape.TextFrame2.MarginLeft = 0
    $shape.TextFrame2.MarginRight = 0
    $shape.TextFrame2.MarginTop = 0
    $shape.TextFrame2.MarginBottom = 0
    $shape.TextFrame2.WordWrap = -1
    $shape.TextFrame2.VerticalAnchor = 3
    $shape.TextFrame2.TextRange.Text = $text
    $shape.TextFrame2.TextRange.ParagraphFormat.Alignment = 2

    $range = $shape.TextFrame2.TextRange
    $font = $range.Font
    $font.Name = $FontName
    $font.NameFarEast = $FontName
    $font.Size = 36
    $font.Bold = -1
    $font.Fill.Visible = -1
    $font.Fill.Solid()
    $font.Fill.ForeColor.RGB = 16777215
    $font.Fill.Transparency = 0
    $font.Line.Visible = -1
    $font.Line.ForeColor.RGB = 16772029
    $font.Line.Weight = 1.8
    $font.Line.Transparency = 0

    if ($visibleThrough -lt $text.Length) {
        $hidden = $range.Characters($visibleThrough + 1, $text.Length - $visibleThrough)
        $hidden.Font.Fill.Transparency = 1
        $hidden.Font.Line.Transparency = 1
    }
    if ($currentPosition -gt 0) {
        $range.Characters($currentPosition, 1).Font.Size = $currentSize
    }

    $shape.Export($imagePath, 2)
    $shape.Delete()
}

$source = [System.IO.Path]::GetFullPath($AssFile)
$outputRoot = [System.IO.Path]::GetFullPath($Output)
[System.IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$manifest = New-Object System.Collections.Generic.List[object]

$events = New-Object System.Collections.Generic.List[object]
Get-Content -LiteralPath $source -Encoding UTF8 | ForEach-Object {
    if (-not $_.StartsWith('Dialogue: ')) { return }
    $fields = $_.Substring(10) -split ',', 10
    $start = Convert-AssTimeToSeconds $fields[1]
    $end = Convert-AssTimeToSeconds $fields[2]
    if ($start -ge $MaxSeconds) { return }
    $events.Add([pscustomobject]@{
        Start = $start
        End = [math]::Min($end, $MaxSeconds)
        Text = $fields[9].Replace('\N', [Environment]::NewLine)
    })
}

$powerPoint = New-Object -ComObject PowerPoint.Application
$presentation = $null
try {
    $presentation = $powerPoint.Presentations.Add($false)
    $presentation.PageSetup.SlideWidth = 960
    $presentation.PageSetup.SlideHeight = 101.25
    $slide = $presentation.Slides.Add(1, 12)
    $frameIndex = 0

    foreach ($event in $events) {
        $positions = New-Object System.Collections.Generic.List[int]
        for ($position = 1; $position -le $event.Text.Length; $position++) {
            if (-not [char]::IsWhiteSpace($event.Text[$position - 1])) {
                $positions.Add($position)
            }
        }
        if ($positions.Count -eq 0) { continue }

        $available = [math]::Max(0.3, $event.End - $event.Start)
        $step = [math]::Min(0.24, [math]::Max(0.12, ($available * 0.72) / $positions.Count))
        for ($index = 0; $index -lt $positions.Count; $index++) {
            $position = $positions[$index]
            $start = $event.Start + ($index * $step)
            $next = if ($index -eq $positions.Count - 1) { $event.End } else { [math]::Min($event.End, $start + $step) }
            if ($start -ge $event.End) { break }

            $stages = @(
                @{ Start = $start; End = [math]::Min($next, $start + ($step * 0.34)); Size = 48 },
                @{ Start = $start + ($step * 0.34); End = [math]::Min($next, $start + ($step * 0.67)); Size = 41 },
                @{ Start = $start + ($step * 0.67); End = $next; Size = 36 }
            )
            foreach ($stage in $stages) {
                if ($stage.End -le $stage.Start) { continue }
                $imageName = ('{0:D4}.png' -f $frameIndex)
                $imagePath = Join-Path $outputRoot $imageName
                Add-PopFrame $slide $event.Text $position $position $stage.Size $imagePath
                $manifest.Add([pscustomobject]@{
                    clip_id = '001'
                    event_index = $frameIndex
                    start = Convert-SecondsToAssTime $stage.Start
                    end = Convert-SecondsToAssTime $stage.End
                    style = 'SongPop'
                    speaker = 'Kioi'
                    image = $imagePath
                    text = $event.Text.Replace([Environment]::NewLine, '\N')
                })
                $frameIndex += 1
            }
        }
    }
} finally {
    if ($presentation -ne $null) { $presentation.Close() }
    $powerPoint.Quit()
}

$manifestPath = Join-Path $outputRoot 'manifest.csv'
$manifest | Export-Csv -LiteralPath $manifestPath -NoTypeInformation -Encoding UTF8
Write-Output $manifestPath
Write-Output ('Rendered {0} animated subtitle states' -f $manifest.Count)
