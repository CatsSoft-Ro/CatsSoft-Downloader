"""Teste de regresie pentru integrarea Windows Defender.

Bug prins in auditul manual: `scan_with_defender()` folosea
`MpCmdRun.exe -Scan -ScanType 3 -File <path>`. ScanType 3 este *ResourceScan*:
pentru un fisier individual Defender raspunde "Scanning ... was skipped." si
totusi intoarce cod 0. Aplicatia interprets codul 0 drept "curat", deci
anuntau "fisierul nu are amenintari" fara sa fi scanat vreun octet.

Testele de mai jos ruleaza fara Defender: se monkey-patcheaza subprocess.run.
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class DefenderScanTests(unittest.TestCase):

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()
        self.calls = []
        self._original_run = self.app.subprocess.run
        self._original_find = self.app._find_mpcmdrun
        self.app._find_mpcmdrun = lambda: r"C:\fake\MpCmdRun.exe"

    def tearDown(self):
        self.app.subprocess.run = self._original_run
        self.app._find_mpcmdrun = self._original_find
        self.env.__exit__(None, None, None)

    def _fake_run(self, returncode=0, stdout="", stderr=""):
        def runner(args, **kwargs):
            self.calls.append(list(args))
            return _FakeProc(returncode, stdout, stderr)
        self.app.subprocess.run = runner

    # ── argumentele corecte ────────────────────────────────────────────────
    def test_uses_the_invocation_that_actually_scans(self):
        """Doar -ScanType 3 -DisableRemediation scaneaza efectiv un fisier.

        -ScanType 3 -File (fara DisableRemediation) SARE fisierul si totusi
        intoarce cod 0; -ScanType 1 nu da niciun verdict."""
        self._fake_run(stdout="Scan finished. found no threats.")
        self.app.scan_with_defender(r"C:\temp\file.bin")
        self.assertTrue(self.calls, "MpCmdRun nu a fost apelat")
        args = self.calls[0]
        self.assertIn("3", args, "trebuie folosit -ScanType 3: %s" % args)
        self.assertIn("-DisableRemediation", args,
                      "fara -DisableRemediation fisierul este sarit: %s" % args)
        self.assertIn("-File", args)

    # ── interpretarea rezultatului ─────────────────────────────────────────
    def test_clean_file_is_reported_clean(self):
        self._fake_run(returncode=0, stdout="Scan finished. Scanning x found no threats.")
        result = self.app.scan_with_defender(r"C:\temp\file.bin")
        self.assertTrue(result["ran"])
        self.assertTrue(result["clean"])
        self.assertFalse(result["skipped"])

    def test_detected_threat_is_not_clean(self):
        self._fake_run(returncode=2, stdout="Threats found. File: C:\\temp\\file.bin")
        result = self.app.scan_with_defender(r"C:\temp\file.bin")
        self.assertTrue(result["ran"])
        self.assertFalse(result["clean"], "o amenintare detectata nu poate fi 'curata'")

    def test_detected_threat_name_is_extracted(self):
        output = ("Scan finished. Scanning x found 1 threats.\n"
                  "Threat                  : Virus:DOS/EICAR_Test_File\n"
                  "Resources               : 1 total\n")
        self._fake_run(returncode=2, stdout=output)
        result = self.app.scan_with_defender(r"C:\temp\file.bin")
        self.assertFalse(result["clean"])
        self.assertIn("Virus:DOS/EICAR_Test_File", result["threats"])

    def test_clean_verdict_comes_from_text_not_only_returncode(self):
        """Defender poate raporta 'no threats' cu un cod de iesire nenul."""
        self._fake_run(returncode=1, stdout="Scan finished. Scanning x found no threats.")
        result = self.app.scan_with_defender(r"C:\temp\file.bin")
        self.assertTrue(result["clean"], "verdictul din text are prioritate")

    def test_skipped_scan_is_not_reported_as_clean(self):
        """Defender a sarit fisierul: starea ramane NECUNOSCUTA, nu 'curat'."""
        self._fake_run(returncode=0,
                       stdout="Scan finished. Scanning C:\\temp\\file.bin was skipped.")
        result = self.app.scan_with_defender(r"C:\temp\file.bin")
        self.assertTrue(result["ran"])
        self.assertTrue(result["skipped"])
        self.assertIsNone(result["clean"],
                          "o scanare sarita nu poate fi raportata ca 'curata'")

    def test_timeout_leaves_clean_unknown(self):
        def runner(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)
        self.app.subprocess.run = runner
        result = self.app.scan_with_defender(r"C:\temp\file.bin")
        self.assertFalse(result["ran"])
        self.assertIsNone(result["clean"])

    def test_missing_mpcmdrun_is_handled(self):
        self.app._find_mpcmdrun = lambda: None
        result = self.app.scan_with_defender(r"C:\temp\file.bin")
        self.assertFalse(result["ran"])
        self.assertIsNone(result["clean"])
        self.assertIn("MpCmdRun", result["output"])

    def test_non_windows_platform_is_skipped(self):
        original = self.app.sys.platform
        self.app.sys.platform = "linux"
        try:
            result = self.app.scan_with_defender(r"C:\temp\file.bin")
            self.assertFalse(result["ran"])
            self.assertIsNone(result["clean"])
        finally:
            self.app.sys.platform = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
