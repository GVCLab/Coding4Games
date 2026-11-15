"""Validate automatic episode termination before launching a game or player."""


def validate_terminal_detection(spec):
    lifecycle = spec.get('lifecycle') or {}
    sampling = lifecycle.get('stepped_terminal_sampling', 'action_end_v1')
    if sampling not in {'action_end_v1', 'during_action_v1', 'during_action_v2'}:
        raise ValueError('unknown lifecycle.stepped_terminal_sampling')
    if sampling in {'during_action_v1', 'during_action_v2'}:
        interval = lifecycle.get('terminal_scan_tics', 2)
        if type(interval) is not int or interval <= 0:
            raise ValueError('terminal_scan_tics must be a positive integer')
    if sampling == 'during_action_v2':
        scorer = spec.get('scorer') or {}
        if (scorer.get('implementation') != 'overcooked_2'
                or scorer.get('recognition_version') != 'overcooked_results_v2'
                or lifecycle.get('terminal_scan_tics') != 3):
            raise ValueError('during_action_v2 requires overcooked_results_v2 and terminal_scan_tics=3')
    if (spec.get('scorer') or {}).get('implementation') == 'overcooked_2':
        from coding4game_gui.benchmark.windows.scorers.overcooked_contract import validate_overcooked_spec
        validate_overcooked_spec(spec['scorer'])
        cap = (spec.get('runtime') or {}).get('max_tics')
        if type(cap) is not int or cap <= 0:
            raise ValueError('overcooked_2 requires a measured positive runtime.max_tics')
    enabled = (spec.get('lifecycle') or {}).get('terminal_detection', True)
    if type(enabled) is not bool:
        raise ValueError('lifecycle.terminal_detection must be a boolean')
    terminal_only = (spec.get('runtime') or {}).get('terminal_only_final_tests', False)
    if type(terminal_only) is not bool:
        raise ValueError('runtime.terminal_only_final_tests must be a boolean')
    if terminal_only and not enabled:
        raise ValueError('terminal-only final tests require registered terminal detection')
    if enabled and (spec.get('scorer') or {}).get('implementation') == 'riders_republic':
        raise ValueError('Riders Republic terminal detection is not calibrated. '
                         'No game or player was launched.')
    return enabled
