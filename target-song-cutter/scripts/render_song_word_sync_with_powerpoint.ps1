param(
    [Parameter(Mandatory = $true)]
    [string]$TimingCsv,
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [string]$FontName = 'AR WeiBeiGBStd BD'
)

$ErrorActionPreference = 'Stop'

function Convert-SecondsToAssTime([double]$value) {
    if ($value -lt 0) { $value = 0 }
    $hours = [math]::Floor($value / 3600)
    $minutes = [math]::Floor(($value % 3600) / 60)
    $seconds = $value % 60
    return ('{0}:{1:00}:{2:00.00}' -f [int]$hours, [int]$minutes, [double]$seconds)
}

function Set-RangePalette($range, [double]$fillTransparency, [int]$fillRgb, [double]$lineTransparency, [int]$lineRgb) {
    $range.Font.Fill.Visible = -1
    $range.Font.Fill.Solid()
    $range.Font.Fill.ForeColor.RGB = $fillRgb
    $range.Font.Fill.Transparency = $fillTransparency
    $range.Font.Line.Visible = -1
    $range.Font.Line.ForeColor.RGB = $lineRgb
    $range.Font.Line.Transparency = $lineTransparency
    $range.Font.Line.Weight = 1.8
}

function Add-WordFrame(
    $slide,
    [string]$text,
    [int]$wordPosition,
    [int]$wordLength,
    [int]$futurePosition,
    [double]$activeSize,
    [double]$activeTransparency,
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
    Set-RangePalette $range 0 16776439 0 7032353

    try {
        $font.Shadow.Visible = -1
        $font.Shadow.ForeColor.RGB = 2168071
        $font.Shadow.Transparency = 0.35
        $font.Shadow.Blur = 1.3
        $font.Shadow.OffsetX = 1.2
        $font.Shadow.OffsetY = 1.6
    } catch {
        # Older Office builds may not expose all Font2 shadow properties.
    }

    if ($futurePosition -le $text.Length) {
        $future = $range.Characters($futurePosition, $text.Length - $futurePosition + 1)
        Set-RangePalette $future 1 16776439 1 7032353
        try { $future.Font.Shadow.Transparency = 1 } catch {}
    }

    $active = $range.Characters($wordPosition, $wordLength)
    Set-RangePalette $active $activeTransparency 16776439 $activeTransparency 7032353
    $active.Font.Size = $activeSize
    try { $active.Font.Shadow.Transparency = [math]::Min(1, 0.35 + $activeTransparency) } catch {}

    $shape.Export($imagePath, 2)
    $shape.Delete()
}

$timingPath = [System.IO.Path]::GetFullPath($TimingCsv)
$outputRoot = [System.IO.Path]::GetFullPath($Output)
[System.IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$rows = Import-Csv -LiteralPath $timingPath -Encoding UTF8
$manifest = New-Object System.Collections.Generic.List[object]

$powerPoint = New-Object -ComObject PowerPoint.Application
$presentation = $null
try {
    $presentation = $powerPoint.Presentations.Add($false)
    $presentation.PageSetup.SlideWidth = 960
    $presentation.PageSetup.SlideHeight = 101.25
    $slide = $presentation.Slides.Add(1, 12)
    $frameIndex = 0

    $rows | Group-Object line_id | Sort-Object { [int]$_.Name } | ForEach-Object {
        $words = @($_.Group | Sort-Object { [double]$_.word_start })
        $text = $words[0].text
        $searchFrom = 0
        for ($index = 0; $index -lt $words.Count; $index++) {
            $entry = $words[$index]
            $wordStart = [double]$entry.word_start
            $wordEnd = if ($index -eq $words.Count - 1) {
                [double]$entry.line_end
            } else {
                [double]$words[$index + 1].word_start
            }
            $zeroBased = $text.IndexOf($entry.word, $searchFrom, [System.StringComparison]::Ordinal)
            if ($zeroBased -lt 0) { throw "Word '$($entry.word)' not found in '$text'" }
            $wordPosition = $zeroBased + 1
            $wordLength = $entry.word.Length
            $searchFrom = $zeroBased + $wordLength
            $futurePosition = $searchFrom + 1

            $popDuration = [math]::Min(0.24, [math]::Max(0.16, ($wordEnd - $wordStart) * 0.28))
            $stages = @(
                @{ Start = $wordStart; End = [math]::Min($wordEnd, $wordStart + ($popDuration * 0.22)); Size = 52; Alpha = 0.82 },
                @{ Start = $wordStart + ($popDuration * 0.22); End = [math]::Min($wordEnd, $wordStart + ($popDuration * 0.50)); Size = 44; Alpha = 0.45 },
                @{ Start = $wordStart + ($popDuration * 0.50); End = [math]::Min($wordEnd, $wordStart + $popDuration); Size = 38; Alpha = 0.12 },
                @{ Start = $wordStart + $popDuration; End = $wordEnd; Size = 36; Alpha = 0 }
            )

            foreach ($stage in $stages) {
                if ($stage.End -le $stage.Start) { continue }
                $imageName = ('{0:D4}.png' -f $frameIndex)
                $imagePath = Join-Path $outputRoot $imageName
                Add-WordFrame $slide $text $wordPosition $wordLength $futurePosition $stage.Size $stage.Alpha $imagePath
                $manifest.Add([pscustomobject]@{
                    clip_id = '001'
                    event_index = $frameIndex
                    start = Convert-SecondsToAssTime $stage.Start
                    end = Convert-SecondsToAssTime $stage.End
                    style = 'WordSync'
                    speaker = 'Kioi'
                    image = $imagePath
                    text = $text
                    active_word = $entry.word
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
Write-Output ('Rendered {0} word-synced subtitle states' -f $manifest.Count)
