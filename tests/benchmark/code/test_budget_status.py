from types import SimpleNamespace

from coding4game_gui.benchmark.code.budget_status import BudgetReporter


def test_notice_deduplicates_and_stages_only_public_message(capsys):
    state = {'tokens_reported':8, 'unknown_usage_requests':1, 'stop_reason':None,
             'request_in_flight':False, 'budget_message':'at least 8; one unknown request'}
    rows, files = [], []
    reporter = BudgetReporter(SimpleNamespace(status=lambda:dict(state)),
        SimpleNamespace(append=lambda *a,**kw:rows.append(kw)),
        supervisor=SimpleNamespace(stage=lambda f,**kw:files.append((f,kw))))
    reporter.update(); reporter.update()
    assert len(rows) == len(files) == 1
    assert files[0] == ({'budget_status.txt':state['budget_message'].encode()}, {'name':'operator'})
    state.update(unknown_usage_requests=2, stop_reason='usage_unknown_limit', budget_message='stopped: two unknown')
    reporter.update()
    assert len(rows) == len(files) == 2
    assert 'stopped=usage_unknown_limit' in capsys.readouterr().out


def test_inflight_and_usage_changes_do_not_flood_terminal(monkeypatch, capsys):
    now = [0]
    monkeypatch.setattr('coding4game_gui.benchmark.code.budget_status.time.monotonic', lambda: now[0])
    state = {'tokens_reported': 8, 'budget_message': 'full accounting detail',
             'request_in_flight': False}
    rows = []
    reporter = BudgetReporter(SimpleNamespace(status=lambda: dict(state)),
                              SimpleNamespace(append=lambda *a, **kw: rows.append(kw)))
    reporter.update()
    for n in range(10):
        state.update(tokens_reported=n + 9, request_in_flight=bool(n % 2))
        reporter.update()
    assert len(rows) == 11
    assert len(capsys.readouterr().out.splitlines()) == 1
    now[0] = 60
    state['tokens_reported'] = 30
    reporter.update()
    assert 'total=30/unlimited' in capsys.readouterr().out
