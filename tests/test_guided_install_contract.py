from pathlib import Path
import re
ROOT=Path(__file__).resolve().parents[1]

def test_root_entrypoints_and_docs_exist():
    for rel in ('install.cmd','install.ps1','install.sh','start.cmd','start.sh','docs/INSTALLATION.md'):
        assert (ROOT/rel).is_file()

def test_windows_npm_and_default_no_consent():
    text=(ROOT/'scripts/windows-prerequisites.ps1').read_text()
    assert "Find-CommandVersion 'npm.cmd'" in text
    assert 'npm.exe' not in text
    assert 'if (-not $Yes)' in text and '[y/N]' in text
    assert "'package-default'" in text and "'machine'" in text and "'user'" in text
    assert "--force" not in text.replace('no --silent, --force, or agreement suppression','')

def test_linux_plan_and_verified_user_node():
    text=(ROOT/'scripts/linux-prerequisites.sh').read_text()
    assert "import venv, ensurepip" in text
    assert 'command -v "$candidate"' in text
    assert 'sha256sum -c' in text
    assert 'SHASUMS256.txt' in text
    assert 'ASSUME_YES == 0' in text and '[y/N]' in text
    assert not re.search(r'\bcurl\b[^\n]*\|[ \t]*(?:ba)?sh\b', text)
    assert 'sudo apt-get' in text and 'sudo dnf' in text

def test_manager_does_not_run_phone_tools():
    text=(ROOT/'ops/setup_manager.py').read_text()
    assert "['adb'" not in text and "['sudo'" not in text
    assert "X-D3m0n-Launcher" in text
    assert "X-D3m0n-Instance" in text
    assert 'os.kill(' not in text
    assert 'checkout_lock' in text
