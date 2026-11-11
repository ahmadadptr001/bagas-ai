"""Regresi installer berulang tanpa pip/unduhan nyata atau perubahan sistem."""
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_support", REPO / "src/agent/install_support.py")
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="uji_installer_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source with spaces"
        self.package = self.source / "src/agent"
        self.package.mkdir(parents=True)
        (self.source / "pyproject.toml").write_text(
            '[project]\nname="bagasai"\nversion="1.0.0"\ndependencies=["rich>=13"]\n', encoding="utf-8")
        (self.package / "__init__.py").write_text("# fixture\n")
        self.installed = self.root / "installed/agent"
        shutil.copytree(self.package, self.installed)
        self.dist = SimpleNamespace(version="1.0.0", locate_file=lambda _: self.installed,
            read_text=lambda _: None, entry_points=[SimpleNamespace(name=n, group="console_scripts")
                for n in ("bagas-ai", "bagasai", "bagas")])
        self.reporter = MagicMock()

    def test_current_package_and_dependencies_are_not_reinstalled(self):
        with (patch.object(support.metadata, "distribution", return_value=self.dist),
              patch.object(support.metadata, "version", return_value="14.0")):
            support.install_python(self.source, self.reporter)
            support.install_python(self.source, self.reporter)
        self.reporter.run.assert_not_called()

    def test_changed_source_is_detected_even_at_same_version(self):
        with patch.object(support.metadata, "distribution", return_value=self.dist):
            (self.package / "__init__.py").write_bytes(b"# fixture\r\n")
            self.assertTrue(support.package_current(self.source))
            (self.package / "__init__.py").write_text("# changed\n")
            self.assertFalse(support.package_current(self.source))

    def test_only_missing_requirement_is_installed_once(self):
        pyproject = self.source / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace('["rich>=13"]',
            '["rich>=13", "fixture-package>=2", "ignored; python_version < \'1\'"]'))
        present = {"rich": "14.0"}
        def version(name):
            if name not in present:
                raise support.metadata.PackageNotFoundError(name)
            return present[name]
        def run(args, *a, **kw):
            self.assertIn("fixture-package>=2", args)
            self.assertNotIn("rich>=13", args)
            self.assertNotIn("--upgrade", args)
            present["fixture-package"] = "2.1"
        self.reporter.run.side_effect = run
        with (patch.object(support.metadata, "distribution", return_value=self.dist),
              patch.object(support.metadata, "version", side_effect=version)):
            support.install_python(self.source, self.reporter)
            support.install_python(self.source, self.reporter)
        self.assertEqual(self.reporter.run.call_count, 1)

    def test_app_build_is_clean_and_does_not_reinstall_dependencies(self):
        stale = self.source / "build/lib/agent"
        stale.mkdir(parents=True)
        (stale / "old.py").write_text("# stale")
        def run(args, label, **kwargs):
            stage = Path(args[-1])
            self.assertNotEqual(stage, self.source)
            self.assertTrue((stage / "src/agent/__init__.py").is_file())
            self.assertFalse((stage / "build").exists())
            self.assertIn("--no-deps", args)
            self.assertIn("--no-build-isolation", args)
            self.assertNotIn("--no-cache-dir", args)
        self.reporter.run.side_effect = run
        with (patch.object(support, "package_current", side_effect=[False, True, True]),
              patch.object(support.metadata, "version", return_value="999"),
              patch.object(support, "_source_metadata") as origin):
            support.install_python(self.source, self.reporter)
            support.install_python(self.source, self.reporter)
            origin.assert_called_once_with(self.source)
        self.assertEqual(self.reporter.run.call_count, 1)
        self.assertTrue((stale / "old.py").is_file())

    def test_browser_reuses_valid_runtime_and_installs_missing_once(self):
        with patch.object(support, "chromium_ready", return_value=True):
            support.ensure_browser(self.reporter)
            support.ensure_browser(self.reporter)
        self.reporter.run.assert_not_called()
        with patch.object(support, "chromium_ready", side_effect=[False, True, True]):
            support.ensure_browser(self.reporter)
            support.ensure_browser(self.reporter)
        self.assertEqual(self.reporter.run.call_count, 1)

    def test_cached_vision_is_verified_without_pull(self):
        vision = SimpleNamespace(_mulai_ollama=lambda: (True, "siap"), _model_ada=lambda: True,
                                 _nama_model=lambda: "gemma3:4b")
        agent, tools = ModuleType("agent"), ModuleType("agent.tools")
        agent.tools = tools
        tools.vision_local = vision
        with (patch.dict(sys.modules, {"agent": agent, "agent.tools": tools}),
              patch.object(support, "ensure_blip"),
              patch.object(support, "find_ollama", return_value="ollama"),
              patch.dict(os.environ, {}, clear=False)):
            support.ensure_vision(self.reporter)
            support.ensure_vision(self.reporter)
        self.assertEqual(self.reporter.run.call_count, 2)
        self.assertTrue(all("pull" not in call.args[0] for call in self.reporter.run.call_args_list))

    def test_cached_blip_is_not_downloaded_again(self):
        ready = MagicMock(return_value=(True, "siap"))
        module = SimpleNamespace(ensure_blip_available=ready)
        with patch.dict(sys.modules, {"agent.blip_local": module}):
            support.ensure_blip(self.reporter)
            support.ensure_blip(self.reporter)
        self.reporter.run.assert_not_called()
        self.assertTrue(all(call.kwargs == {"download": False} for call in ready.call_args_list))

    def test_missing_blip_is_prepared_once_then_reused(self):
        ready = MagicMock(side_effect=[(False, "belum ada"), (True, "siap"), (True, "siap")])
        module = SimpleNamespace(ensure_blip_available=ready)
        with patch.dict(sys.modules, {"agent.blip_local": module}):
            support.ensure_blip(self.reporter)
            support.ensure_blip(self.reporter)
        self.reporter.run.assert_called_once()
        self.assertIn("--prepare-model", self.reporter.run.call_args.args[0])

    def test_ollama_outside_path_is_found(self):
        executable = self.root / "Programs/Ollama/ollama.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        with (patch.object(support.shutil, "which", return_value=None),
              patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)})):
            self.assertEqual(Path(support.find_ollama()), executable)

    def test_whisper_cache_is_used_offline_and_partial_cache_is_retained(self):
        spec = importlib.util.spec_from_file_location("dengar_cache_uji", REPO / "src/agent/dengar.py")
        dengar = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = dengar
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(dengar)
        cache = self.root / "model"
        cache.mkdir()
        for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json"):
            (cache / name).write_bytes(b"existing cached data")
        download = MagicMock(return_value=str(cache))
        fake_utils = SimpleNamespace(download_model=download)
        fake_hub = SimpleNamespace(LocalEntryNotFoundError=FileNotFoundError)
        with patch.dict(sys.modules, {"faster_whisper.utils": fake_utils, "huggingface_hub.utils": fake_hub}):
            self.assertEqual(dengar._lokasi_model_whisper("small"), str(cache))
            download.assert_called_once_with("small", local_files_only=True)
            download.reset_mock()
            (cache / "tokenizer.json").unlink()
            self.assertEqual(dengar._lokasi_model_whisper("small"), str(cache))
            self.assertEqual(download.call_count, 2)
            self.assertEqual(download.call_args.args, ("small",))
            self.assertEqual((cache / "model.bin").read_bytes(), b"existing cached data")

    def test_failed_command_is_logged_and_not_reported_as_success(self):
        with patch.dict(os.environ, {"BAGASAI_INSTALL_LOG": str(self.root / "install.log")}):
            reporter = support.Reporter()
            with (self.assertRaises(support.InstallerError), patch("sys.stdout", io.StringIO())):
                reporter.run([sys.executable, "-c", "print('diagnostic detail'); raise SystemExit(3)"], "Uji")
        self.assertIn("diagnostic detail", reporter.log.read_text())

    def _cached_source(self):
        home = self.root / "home with spaces"
        cached = home / "src"
        (cached / "src/agent").mkdir(parents=True)
        (cached / "pyproject.toml").write_text('[project]\nname="bagasai"\n')
        (cached / "src/agent/install_support.py").write_text("# cached support\n")
        (cached / ".bagasai-source-id").write_text("https://github.com/ahmadadptr001/bagas-ai#master\n")
        (cached / "keep.bin").write_bytes(b"downloaded data")
        return home, cached

    @unittest.skipUnless(os.name == "nt", "PowerShell wrapper khusus Windows")
    def test_powershell_wrapper_reuses_source_twice(self):
        home, cached = self._cached_source()
        apps = self.root / "apps"
        for file in (apps / "BraveSoftware/Brave-Browser/Application/brave.exe",
                     apps / "Tesseract-OCR/tesseract.exe"):
            file.parent.mkdir(parents=True, exist_ok=True)
            file.touch()
        script = (REPO / "install.ps1").read_text(encoding="utf-8")
        script = script[:script.index("# --- 4. Pastikan")]
        mocks = r'''
function python {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$NativeArgs)
    $global:LASTEXITCODE = 0
    if ($NativeArgs -contains '--version') { 'Python 3.13' }
    if ($NativeArgs -contains 'find-ollama') { 'C:\cached\ollama.exe' }
}
function git { throw 'Source cache tidak boleh diunduh ulang' }
function winget { throw 'Komponen yang tersedia tidak boleh dipasang ulang' }
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$NoNewWindow, [switch]$Wait, [switch]$PassThru)
    [pscustomobject]@{ExitCode=0}
}
'''
        wrapper = self.root / "wrapper.ps1"
        wrapper.write_text(mocks + script, encoding="ascii")
        env = {**os.environ, "BAGASAI_HOME": str(home), "LOCALAPPDATA": str(apps),
               "ProgramFiles": str(apps), "ProgramFiles(x86)": str(apps)}
        env.pop("BAGASAI_REPO", None)
        env.pop("BAGASAI_BRANCH", None)
        for _ in range(2):
            run = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
                cwd=self.root, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("Kode sumber sudah tersedia", run.stdout)
        self.assertEqual((cached / "keep.bin").read_bytes(), b"downloaded data")

    def test_bash_wrapper_reuses_source_twice(self):
        bash = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else Path(shutil.which("bash") or "")
        if not bash.is_file():
            self.skipTest("Bash tidak tersedia")
        home, cached = self._cached_source()
        script = (REPO / "install.sh").read_text(encoding="utf-8")
        script = script[:script.index("# --- 4. Pastikan")]
        mocks = '''
python3() { case "$*" in *--version*) printf '%s\\n' 'Python 3.13' ;; esac; return 0; }
git() { printf '%s\\n' 'unexpected git download' >&2; return 99; }
curl() { printf '%s\\n' 'unexpected curl download' >&2; return 99; }
pipx() { [ "$1" = environment ] && { printf '%s\\n' "$HOME/no-pipx"; return 0; }; return 99; }
brave-browser() { return 0; }
tesseract() { return 0; }
ollama() { return 0; }
'''
        wrapper = self.root / "wrapper.sh"
        wrapper.write_text(mocks + script, encoding="utf-8", newline="\n")
        env = {**os.environ, "BAGASAI_HOME": home.as_posix(), "HOME": home.as_posix()}
        env.pop("BAGASAI_REPO", None)
        env.pop("BAGASAI_BRANCH", None)
        for _ in range(2):
            run = subprocess.run([str(bash), wrapper.as_posix()], cwd=self.root, env=env,
                capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("Kode sumber sudah tersedia", run.stdout)
        self.assertEqual((cached / "keep.bin").read_bytes(), b"downloaded data")


if __name__ == "__main__":
    unittest.main(verbosity=2)
