"""Real local post-run review timing, kept separate from original observations."""
import time
import re
from copy import deepcopy


class HumanReview:
    def __init__(self, task_id, pseudonymous_role):
        if not isinstance(pseudonymous_role, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", pseudonymous_role):
            raise ValueError("pseudonymous_role_required")
        self.task_id = task_id
        self.role = pseudonymous_role
        self.start = time.perf_counter()
        self.finished = False

    def finish(self, *, needs_correction, edits, followup_design_required):
        if self.finished:
            raise ValueError("review_already_finished")
        self.finished = True
        if type(needs_correction) is not bool or type(followup_design_required) is not bool or not isinstance(edits, list):
            raise ValueError("invalid_review_record")
        from .paths import safe
        safe(edits)
        return {"task_id": self.task_id, "reviewer_role": self.role,
                "elapsed_seconds": time.perf_counter() - self.start, "clock_source": "local_monotonic",
                "needs_correction": needs_correction, "edits": deepcopy(edits),
                "followup_design_required": followup_design_required,
                "stage": "post_run_review", "changes_original_score": False}


def unobserved_review(task_id):
    return {"task_id": task_id, "elapsed_seconds": None, "stage": "post_run_review",
            "observation_state": "not_recorded", "needs_correction": None, "edits": None}
