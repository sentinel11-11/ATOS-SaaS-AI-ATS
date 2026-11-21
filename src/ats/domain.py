class Problem(Exception):
    def __init__(self, status: int, code: str, detail: str | None = None):
        self.status = status
        self.code = code
        self.detail = detail


ACTIVE = {"prepared", "dispatching", "accepted", "answered", "unknown"}
TERMINAL = {"completed", "failed"}
MISSING_AMI_TERMINAL = "ami_terminal_event_missing"
EVIDENCE_REQUIRED = {
    "callback_evidence_missing",
    MISSING_AMI_TERMINAL, "callback_bridge_unconfirmed", "callback_result_conflict",
    "callback_leg_identity_conflict",
    "callback_leg_tracking_missing",
}
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
