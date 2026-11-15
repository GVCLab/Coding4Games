"""Operator notices from host accounting; boxed copies are display-only."""


import time


class BudgetReporter:
    def __init__(self, gateway, timeline, *, supervisor=None):
        self.gateway, self.timeline, self.supervisor = gateway, timeline, supervisor
        self.last = None
        self.last_print = float("-inf")
        self.last_stop = None

    def update(self):
        state = self.gateway.status()
        # Do not log every wall-clock tick or repeatedly print an unchanged notice.
        key = tuple(state.get(k) for k in (
            'tokens_reported', 'unknown_usage_requests', 'stop_reason', 'request_in_flight'))
        if key != self.last:
            self.last = key
            message = state.get('budget_message', '')
            self.timeline.append('model_budget', source='harness', details=state)
            if message:
                now = time.monotonic()
                stop = state.get('stop_reason')
                if now - self.last_print >= 60 or stop != self.last_stop:
                    self.last_print, self.last_stop = now, stop
                    parts = state.get('token_breakdown') or {}
                    limit = state.get('token_limit')
                    print(
                        f"[C4G usage] total={state.get('tokens_reported', '?')}/"
                        f"{limit if limit is not None else 'unlimited'}; "
                        f"cached={parts.get('cached_input_tokens', '?')}; "
                        f"uncached={parts.get('uncached_input_tokens', '?')}; "
                        f"output={parts.get('output_tokens', '?')}; "
                        f"unknown_requests={state.get('unknown_usage_requests', 0)}"
                        + (f"; stopped={stop}" if stop else ""), flush=True)
                if self.supervisor is not None:
                    # Informational copy only; never read back as budget authority.
                    self.supervisor.stage({'budget_status.txt': message.encode('utf-8')}, name='operator')
        return state
