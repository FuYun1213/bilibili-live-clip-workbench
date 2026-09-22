param(
    [Parameter(Mandatory = $true)]
    [string]$AssDir,
    [Parameter(Mandatory = $true)]
    [string]$Output
)

function Convert-AssColorToOfficeRgb([string]$value) {
    $hex = $value.Replace('&H', '').PadLeft(8, '0')
    return [Convert]::ToInt32($hex.Substring($hex.Length - 6), 16)
}

$assRoot = [System.IO.Path]::GetFullPath($AssDir)
$outputRoot = [System.IO.Path]::GetFullPath($Output)
[System.IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$manifest = New-Object System.Collections.Generic.List[object]

$powerPoint = New-Object -ComObject PowerPoint.Application
$presentation = $null
try {
    $presentation = $powerPoint.Presentations.Add($false)
    $presentation.PageSetup.SlideWidth = 1275
    $presentation.PageSetup.SlideHeight = 135
    $slide = $presentation.Slides.Add(1, 12)

    Get-ChildItem -LiteralPath $assRoot -Filter '*.ass' | Sort-Object Name | ForEach-Object {
        $source = $_
        $clipId = $source.Name.Substring(0, 3)
        $clipOutput = Join-Path $outputRoot $clipId
        [System.IO.Directory]::CreateDirectory($clipOutput) | Out-Null
        $styles = @{}
        $eventIndex = 0

        Get-Content -LiteralPath $source.FullName -Encoding UTF8 | ForEach-Object {
            $line = $_
            if ($line.StartsWith('Style: ')) {
                $fields = $line.Substring(7).Split(',')
                $styles[$fields[0]] = @{
                    Font = $fields[1]
                    Size = [double]$fields[2]
                    Color = Convert-AssColorToOfficeRgb $fields[3]
                }
                return
            }
            if (-not $line.StartsWith('Dialogue: ')) {
                return
            }

            $fields = $line.Substring(10) -split ',', 10
            $start = $fields[1]
            $end = $fields[2]
            $styleName = $fields[3]
            $speaker = $fields[4]
            $text = $fields[9].Replace('\N', [Environment]::NewLine)
            $style = $styles[$styleName]
            if ($null -eq $style) {
                throw "Unknown ASS style $styleName in $($source.Name)"
            }

            $shape = $slide.Shapes.AddTextbox(1, 0, 0, 1275, 135)
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
            $font = $shape.TextFrame2.TextRange.Font
            $font.Name = $style.Font
            $font.NameFarEast = $style.Font
            $font.Size = $style.Size * 0.75
            $font.Bold = -1
            $font.Fill.Visible = -1
            $font.Fill.Solid()
            $font.Fill.ForeColor.RGB = $style.Color
            $font.Line.Visible = -1
            $font.Line.ForeColor.RGB = 16777215
            $font.Line.Weight = 3.15

            $imageName = ('{0:D3}.png' -f $eventIndex)
            $imagePath = Join-Path $clipOutput $imageName
            $shape.Export($imagePath, 2)
            $shape.Delete()
            $manifest.Add([pscustomobject]@{
                clip_id = $clipId
                event_index = $eventIndex
                start = $start
                end = $end
                style = $styleName
                speaker = $speaker
                image = $imagePath
                text = $text.Replace([Environment]::NewLine, '\N')
            })
            $eventIndex += 1
        }
    }
} finally {
    if ($presentation -ne $null) {
        $presentation.Close()
    }
    $powerPoint.Quit()
}

$manifestPath = Join-Path $outputRoot 'manifest.csv'
$manifest | Export-Csv -LiteralPath $manifestPath -NoTypeInformation -Encoding UTF8
Write-Output $manifestPath
Write-Output ("Rendered {0} subtitle images" -f $manifest.Count)
