from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from ops import setup_manager as sm


def test_dry_run_installs_nothing(monkeypatch, capsys):
    monkeypatch.setattr(sm.subprocess, 'run', Mock(side_effect=AssertionError('process in dry-run')))
    monkeypatch.setattr(sm, 'runtime_dir', Mock(side_effect=AssertionError('state in dry-run')))
    assert sm.main(['install', '--dry-run']) == 0
    assert 'Dry run:' in capsys.readouterr().out


def test_start_dry_run_no_state_or_process(monkeypatch):
    monkeypatch.setattr(sm, 'start', Mock(side_effect=AssertionError('dry-run start')))
    assert sm.main(['start', '--dry-run']) == 0


def test_invalid_port_rejected():
    with pytest.raises(SystemExit):
        sm.main(['start', '--port', '0'])


def test_port_fallback_never_takes_over(monkeypatch):
    fake = Mock(); fake.__enter__ = Mock(return_value=fake); fake.__exit__ = Mock()
    fake.bind.side_effect = [OSError('occupied'), None]
    fake.getsockname.return_value = ('127.0.0.1', 54321)
    monkeypatch.setattr(sm.socket, 'socket', Mock(return_value=fake))
    assert sm.choose_port(5055) == 54321
    assert fake.bind.call_args_list[1].args[0] == ('127.0.0.1', 0)


def test_stop_never_uses_stored_pid(monkeypatch, capsys):
    from contextlib import nullcontext
    monkeypatch.setattr(sm, 'lifecycle_lock', nullcontext)
    monkeypatch.setattr(sm, 'load_record', lambda: {'pid': 1, 'repo': str(sm.ROOT)})
    monkeypatch.setattr(sm, 'control', lambda *_: None)
    monkeypatch.setattr(sm.os, 'kill', Mock(side_effect=AssertionError('stored PID kill')))
    assert sm.main(['stop']) == 1
    assert 'no PID-based kill' in capsys.readouterr().out


def test_child_defaults_loopback_and_origins(monkeypatch):
    monkeypatch.setenv('D3M0N_HOST', '0.0.0.0')
    monkeypatch.setenv('D3M0N_ALLOWED_ORIGINS', 'https://dashboard.example.test')
    monkeypatch.setenv('D3M0N_PUBLIC_ORIGIN', 'https://dashboard.example.test')
    env = sm.clean_child_environment(55123)
    assert env['D3M0N_HOST'] == '127.0.0.1'
    assert env['D3M0N_PORT'] == '55123'
    assert env['D3M0N_ALLOWED_ORIGINS'] == 'http://127.0.0.1:55123,http://localhost:55123'
    assert 'D3M0N_PUBLIC_ORIGIN' not in env


def test_record_rejects_other_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, 'runtime_dir', lambda: tmp_path)
    (tmp_path/'instance.json').write_text(json.dumps({'repo':'/another-checkout','control_port':1234,'control_key':'fixture'}))
    assert sm.load_record() is None


def test_record_rejects_out_of_range_port(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, 'runtime_dir', lambda: tmp_path)
    (tmp_path/'instance.json').write_text(json.dumps({'repo':str(sm.ROOT),'control_port':99999,'control_key':'fixture'}))
    assert sm.load_record() is None


def test_control_requires_instance_match(monkeypatch):
    reply=Mock();reply.status=200;reply.read.return_value=b'{"instance":"another","running":true}'
    conn=Mock();conn.getresponse.return_value=reply
    monkeypatch.setattr(sm.http.client,'HTTPConnection',Mock(return_value=conn))
    record={'control_port':54321,'control_key':'fixture','instance':'expected'}
    assert sm.control(record) is None
    assert conn.request.call_args.kwargs['headers']['X-D3m0n-Launcher']=='fixture'


def test_no_ready_url_when_unhealthy(monkeypatch, capsys, tmp_path):
    from contextlib import nullcontext
    py=tmp_path/'python';py.write_text('')
    root=tmp_path/'repo';(root/'frontend/dist').mkdir(parents=True);(root/'frontend/dist/index.html').write_text('fixture')
    monkeypatch.setattr(sm,'ROOT',root);monkeypatch.setattr(sm,'venv_python',lambda:py)
    monkeypatch.setattr(sm,'lifecycle_lock',nullcontext)
    monkeypatch.setattr(sm,'load_record',lambda:{'instance':'fixture'})
    monkeypatch.setattr(sm,'control',lambda *_:{'running':True,'port':55123})
    monkeypatch.setattr(sm,'health',lambda *_:False)
    with pytest.raises(RuntimeError,match='unhealthy'):
        sm.start(5055,True)
    assert 'DASHBOARD READY' not in capsys.readouterr().out


def test_shell_secrets_do_not_cross_manager_boundary(monkeypatch):
    monkeypatch.setenv('UNRELATED_ADMIN_TOKEN', 'fixture-never-copy')
    monkeypatch.setenv('D3M0N_PROVIDER_API_KEY', 'fixture-never-copy')
    env=sm.clean_child_environment(55123)
    assert 'UNRELATED_ADMIN_TOKEN' not in env
    assert 'D3M0N_PROVIDER_API_KEY' not in env
