from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))


def test_ci_has_supported_native_matrix_and_frontend_node24():
    jobs = workflow()['jobs']
    matrix = jobs['native']['strategy']['matrix']
    assert set(matrix['os']) == {'ubuntu-latest', 'windows-latest'}
    assert set(matrix['python']) == {'3.11', '3.12'}
    text = (ROOT / '.github/workflows/ci.yml').read_text()
    for marker in ["node-version: '24.x'", 'npm run type-check', 'npm test', 'npm audit --audit-level=high']:
        assert marker in text


def test_release_is_gated_after_native_and_docker_integration():
    jobs = workflow()['jobs']
    assert set(jobs['release']['needs']) == {'native', 'frontend', 'native-install', 'docker-clean-install'}
    assert set(jobs['package']['needs']) == {'native', 'frontend'}
    assert jobs['docker-clean-install']['needs'] == 'package'
    assert '--release --ci-proof' in str(jobs['release']['steps'])


def test_ci_uses_hashed_locks_and_no_second_resolution():
    text = (ROOT / '.github/workflows/ci.yml').read_text()
    assert text.count('python scripts/ci_create_environment.py') == 2
    assert 'ci_create_environment.py --development' in text
    helper = (ROOT / 'scripts/ci_create_environment.py').read_text()
    assert 'venv.EnvBuilder(with_pip=True)' in helper
    assert "'--require-hashes'" in helper
    assert "'build.lock'" in helper and "'dev.lock'" in helper
    assert "'--no-deps', '--no-build-isolation', '-e'" in helper
    assert "'pip', 'check'" in helper
    assert 'GITHUB_PATH' in helper
    assert 'pip install pytest' not in text


def test_acceptance_scripts_exercise_fresh_auth_proxy_and_restart():
    script = (ROOT / 'scripts/ci_docker_install.py').read_text()
    for marker in ["'up','-d','--wait'", '/api/health', '/api/auth/status', 'install-hash', "['restart']", "'npm','test'", '503']:
        assert marker in script
    assert "startswith('d3m0n-ci-')" in script
    assert "['down','--volumes','--remove-orphans']" in script
    native = (ROOT / 'scripts/ci_native_install.py').read_text()
    assert "['install', '--no-browser']" in native
    assert "['stop']" in native
    assert "['completed_at']" in native


def test_acceptance_scripts_never_discover_or_control_devices():
    for filename in ['ci_native_install.py', 'ci_docker_install.py']:
        text = (ROOT / 'scripts' / filename).read_text().lower()
        for forbidden in ['adb devices', 'adb pair', 'adb shell', 'nmap', 'start-stream', 'create_job']:
            assert forbidden not in text
