param(
    [Parameter(Mandatory = $true)]
    [string]$Output
)

$powerPoint = New-Object -ComObject PowerPoint.Application
$presentation = $null
try {
    $presentation = $powerPoint.Presentations.Add($false)
    $presentation.PageSetup.SlideWidth = 1275
    $presentation.PageSetup.SlideHeight = 135
    $slide = $presentation.Slides.Add(1, 12)
    $shape = $slide.Shapes.AddTextbox(1, 0, 0, 1275, 135)
    $shape.Fill.Visible = 0
    $shape.Line.Visible = 0
    $shape.TextFrame2.MarginLeft = 0
    $shape.TextFrame2.MarginRight = 0
    $shape.TextFrame2.MarginTop = 0
    $shape.TextFrame2.MarginBottom = 0
    $shape.TextFrame2.VerticalAnchor = 3
    $sampleText = ([char]0x5B57) + ([char]0x4F53) + ([char]0x5339) +
        ([char]0x914D) + ([char]0x6D4B) + ([char]0x8BD5) + ([char]0xFF1A) +
        ([char]0x80FD) + ([char]0x80FD) + ([char]0x7ED9) + ([char]0x4F60) +
        ([char]0x6253) + "call"
    $shape.TextFrame2.TextRange.Text = $sampleText
    $shape.TextFrame2.TextRange.ParagraphFormat.Alignment = 2
    $font = $shape.TextFrame2.TextRange.Font
    $font.Name = "AR WeiBeiGBStd BD"
    $font.NameFarEast = "AR WeiBeiGBStd BD"
    $font.Size = 48
    $font.Bold = -1
    $font.Fill.Visible = -1
    $font.Fill.Solid()
    $font.Fill.ForeColor.RGB = 16772029
    $font.Line.Visible = -1
    $font.Line.ForeColor.RGB = 16777215
    $font.Line.Weight = 3.15
    $destination = [System.IO.Path]::GetFullPath($Output)
    $shape.Export($destination, 2)
    Write-Output $destination
} finally {
    if ($presentation -ne $null) {
        $presentation.Close()
    }
    $powerPoint.Quit()
}
