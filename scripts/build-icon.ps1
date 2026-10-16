param([string]$Source)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
if(-not $Source){$Source=Join-Path $root 'web\assets\usic-source.png'}
# Format/size conversion only. Artwork is the original image-gen output.
Add-Type -AssemblyName System.Drawing
$original=[Drawing.Image]::FromFile((Resolve-Path -LiteralPath $Source).Path)
$images=New-Object 'System.Collections.Generic.List[byte[]]'
$sizes=@(16,24,32,48,64,128,256)
$iconDir=Join-Path $root 'desktop\assets'
[IO.Directory]::CreateDirectory($iconDir) | Out-Null
try{
  foreach($size in $sizes){
    $bitmap=New-Object Drawing.Bitmap($size,$size,[Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics=[Drawing.Graphics]::FromImage($bitmap)
    $stream=New-Object IO.MemoryStream
    try{
      $graphics.CompositingMode=[Drawing.Drawing2D.CompositingMode]::SourceCopy
      $graphics.InterpolationMode=[Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
      $graphics.PixelOffsetMode=[Drawing.Drawing2D.PixelOffsetMode]::HighQuality
      $graphics.Clear([Drawing.Color]::Transparent)
      $graphics.DrawImage($original,0,0,$size,$size)
      $bitmap.Save($stream,[Drawing.Imaging.ImageFormat]::Png)
      $images.Add($stream.ToArray())
      if($size -eq 256){[IO.File]::WriteAllBytes((Join-Path $root 'web\assets\usic.png'),$stream.ToArray())}
    }finally{$stream.Dispose();$graphics.Dispose();$bitmap.Dispose()}
  }
  $file=[IO.File]::Create((Join-Path $iconDir 'usic.ico'))
  $writer=New-Object IO.BinaryWriter($file)
  try{
    $writer.Write([uint16]0);$writer.Write([uint16]1);$writer.Write([uint16]$sizes.Count)
    $offset=6+16*$sizes.Count
    for($i=0;$i -lt $sizes.Count;$i++){
      $dimension=$sizes[$i];if($dimension -eq 256){$dimension=0}
      $writer.Write([byte]$dimension);$writer.Write([byte]$dimension)
      $writer.Write([byte]0);$writer.Write([byte]0)
      $writer.Write([uint16]1);$writer.Write([uint16]32)
      $writer.Write([uint32]$images[$i].Length);$writer.Write([uint32]$offset)
      $offset+=$images[$i].Length
    }
    foreach($bytes in $images){$writer.Write([byte[]]$bytes)}
  }finally{$writer.Dispose();$file.Dispose()}
}finally{$original.Dispose()}
Write-Output 'USIC_ICONS_BUILT: transparent PNG + 7-size ICO'
