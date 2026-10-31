# Word -> PDF for the template-digitise bench (owner-run; needs Word installed).
# Spec: docs/superpowers/specs/2026-10-31-model-scope-and-config-update-design.md Part B1.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools/template-digitise-bench/convert.ps1 `
#       -Src "C:\Users\camil\Dropbox\ADE\Penina Site Inspection Checklist.docx" `
#       -Out C:\Users\camil\wt\digitise-bench\inputs -Name penina
#
# Writes <Out>\<Name>.pdf (Name defaults to the source's base name). Refuses an -Out
# under Dropbox (bench files must never sync) and an -Out that is the source's own
# folder (never writes next to the source). The source is opened read-only.
param(
    [Parameter(Mandatory = $true)][string]$Src,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Name = ""
)
$ErrorActionPreference = "Stop"

function Norm([string]$p) {
    return [System.IO.Path]::GetFullPath($p).TrimEnd('\', '/').ToLowerInvariant()
}

$srcFull = [System.IO.Path]::GetFullPath($Src)
$outFull = Norm $Out
$dropbox = Norm "C:\Users\camil\Dropbox"
if ($outFull -eq $dropbox -or $outFull.StartsWith($dropbox + "\")) {
    [Console]::Error.WriteLine("refused: -Out is under Dropbox ($Out)")
    exit 2
}
if ($outFull -eq (Norm ([System.IO.Path]::GetDirectoryName($srcFull)))) {
    [Console]::Error.WriteLine("refused: -Out is the source's folder; never write next to the source")
    exit 2
}
if (-not (Test-Path -LiteralPath $srcFull)) {
    [Console]::Error.WriteLine("refused: no such file $Src")
    exit 2
}
if ($Name -eq "") { $Name = [System.IO.Path]::GetFileNameWithoutExtension($srcFull) }
New-Item -ItemType Directory -Force -Path $Out | Out-Null
# A plain string: Join-Path returns a PSObject-wrapped string that Word COM rejects.
[string]$pdf = [System.IO.Path]::Combine([System.IO.Path]::GetFullPath($Out), $Name + ".pdf")

$wdFormatPDF = 17
$word = New-Object -ComObject Word.Application
$word.Visible = $false
try {
    # Open(FileName, ConfirmConversions, ReadOnly)
    $doc = $word.Documents.Open($srcFull, $false, $true)
    try {
        $doc.SaveAs2($pdf, $wdFormatPDF)
    } finally {
        $doc.Close([ref]$false)
    }
} finally {
    $word.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
}
Write-Output "wrote $pdf"
