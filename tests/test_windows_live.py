"""End-to-end contract against an ephemeral native WPF window on Windows."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

from test_cli import call_cli


@unittest.skipUnless(sys.platform == "win32", "requires a Windows desktop session")
class WindowsDesktopTests(unittest.TestCase):
    def test_explore_compile_invoke_and_read_back(self):
        title = "Any2CLI UIA " + uuid4().hex
        shell = shutil.which("powershell.exe")
        self.assertIsNotNone(shell, "Windows PowerShell with WPF is required for this integration test")
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "window profile.json"
            # WinForms/Win32 proxies vary between builds. WPF exposes the UIA patterns directly.
            script = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework
[xml]$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        ShowActivated="False" ShowInTaskbar="False" Width="480" Height="320">
  <StackPanel>
    <TextBox x:Name="Query" AutomationProperties.AutomationId="Query" />
    <CheckBox x:Name="Flag" Content="Enabled flag" />
    <Button x:Name="Apply" Content="Apply" />
    <TextBlock x:Name="Output" AutomationProperties.AutomationId="Output" Text="Ready" />
    <TabControl AutomationProperties.AutomationId="Pages">
      <TabItem Header="General"><TextBlock Text="General page"/></TabItem>
      <TabItem Header="Details"><TextBlock Text="Extra page"/></TabItem>
    </TabControl>
  </StackPanel>
</Window>
'@
$window = [Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader $xaml))
$window.Title = 'WINDOW_TITLE'
$window.FindName('Apply').Add_Click({
  $window.FindName('Output').Text = $window.FindName('Query').Text
  $window.FindName('Flag').IsChecked = $false
})
$window.Add_ContentRendered({[IO.File]::WriteAllText((Join-Path (Get-Location) 'ready'), '1')})
[void]$window.ShowDialog()
""".replace("WINDOW_TITLE", title)
            process = subprocess.Popen([shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
                                       cwd=folder, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 30
                while not (Path(folder) / "ready").is_file():
                    if process.poll() is not None:
                        self.fail(process.stderr.read().decode(errors="replace"))
                    self.assertLess(time.monotonic(), deadline, "WPF did not open in this desktop session")
                    time.sleep(0.1)

                def check(*args, launcher=None):
                    result = call_cli(*args, cwd=folder, launcher=launcher)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    return json.loads(result.stdout)

                scan = check("explore", shell, "--window", title, "--profile", profile,
                             "--primary", 3, "--steps", 1, "--depth", 1, "--follow", "Pages=Details")
                self.assertTrue(scan["exploration"]["restored"])
                self.assertGreaterEqual(scan["states"], 2)
                self.assertEqual(scan["commands"], 3)
                self.assertGreaterEqual(scan["operations"], 6)
                launcher = Path(scan["cli"])
                self.assertEqual(launcher.suffix, ".cmd")
                args = ["--window", title, "--settle", "2"]
                text = "UIA input 42 é"
                self.assertEqual(check("Query", "--value", text, *args, launcher=launcher)["status"], "verified_ui")
                self.assertEqual(check("Read Query", *args, launcher=launcher)["value"], text)
                self.assertEqual(check("Enabled flag", "--value", "true", *args, launcher=launcher)["status"],
                                 "verified_ui")
                result = check("Apply", *args, launcher=launcher)
                self.assertIn(result["status"], ("observed_ui_change", "verified_ui"))
                self.assertEqual(check("Output", *args, launcher=launcher)["value"], text)
                self.assertNotIn(text, profile.read_text(encoding="utf-8"))
                self.assertGreater(len(check("commands", profile)["commands"]), 3)
            finally:
                process.terminate()
                process.wait(timeout=10)
                process.stderr.close()
