param([string[]]$Files, [switch]$Preview)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$doc = New-Object System.Drawing.Printing.PrintDocument
Write-Output ("Printer: " + $doc.PrinterSettings.PrinterName + " valid=" + $doc.PrinterSettings.IsValid)
$doc.DocumentName = "stock_check"
$doc.DefaultPageSettings.Margins = New-Object System.Drawing.Printing.Margins(0,0,0,0)
if ($Preview) { $doc.PrintController = New-Object System.Drawing.Printing.PreviewPrintController }
$list = @($Files | ForEach-Object { $_ -split "," })
$state = @{ i = 0; files = $list }
$handler = {
  param($s, $e)
  $img = [System.Drawing.Image]::FromFile($state.files[$state.i])
  $b = $e.PageBounds
  $e.Graphics.DrawImage($img, 0, 0, $b.Width, $b.Height)
  $img.Dispose()
  $state.i++
  $e.HasMorePages = ($state.i -lt $state.files.Count)
}.GetNewClosure()
$doc.add_PrintPage($handler)
try { $doc.Print() } catch { Write-Output ("ERROR: " + $_.Exception.Message) }
Write-Output ("Pages handled: " + $state.i)
