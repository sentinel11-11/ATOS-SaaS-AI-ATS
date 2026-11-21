class Problem(Exception):
    def __init__(self, status: int, code: str, detail: str | None = None):
        self.status = status
        self.code = code
        self.detail = detail


ACTIVE = {"prepared", "dispatching", "accepted", "answered", "unknown"}
TERMINAL = {"completed", "failed"}
DIAL_STATUSES = {
    "ANSWER", "BUSY", "NOANSWER", "CHANUNAVAIL", "CONGESTION",
    "CANCEL", "DONTCALL", "TORTURE", "INVALIDARGS",
}


def next_state(current: str, event: str) -> str:
    """Terminal events win over late progress. Unknown requires evidence, never redial."""
    if current in TERMINAL:
        return current
    if event in TERMINAL:
        return event
    if current == "answered":
        return current
    if event == "answered":
        return event
    if event == "accepted" and current in {"prepared", "dispatching", "unknown"}:
        return event
    return current
