#!/bin/sh
# Builds Vouch.exe (one file) with the .NET Framework compiler that ships with Windows.
cd "$(dirname "$0")"
N=nuget/pkg
/c/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe -nologo -target:winexe -platform:x64 -optimize -out:../dist/Vouch.exe -win32icon:vouch.ico \
  -r:System.Windows.Forms.dll -r:System.Drawing.dll -r:Microsoft.CSharp.dll -r:System.Core.dll \
  -r:$N/lib/net462/Microsoft.Web.WebView2.Core.dll -r:$N/lib/net462/Microsoft.Web.WebView2.WinForms.dll \
  -resource:$N/lib/net462/Microsoft.Web.WebView2.Core.dll,Microsoft.Web.WebView2.Core.dll \
  -resource:$N/lib/net462/Microsoft.Web.WebView2.WinForms.dll,Microsoft.Web.WebView2.WinForms.dll \
  -resource:$N/runtimes/win-x64/native/WebView2Loader.dll,WebView2Loader.dll Vouch.cs
