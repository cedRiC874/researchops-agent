from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd


SEED = 20260815
ROW_COUNT = 240
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "data" / "synthetic_trial.csv"


def main() -> None:
    rng = random.Random(SEED)
    rows: list[dict[str, object]] = []
    enrollment_start = date(2025, 1, 6)

    for index in range(ROW_COUNT):
        group = "treatment" if index % 2 == 0 else "control"
        age = round(min(78, max(22, rng.gauss(52, 12))))
        sex = rng.choice(["female", "male"])
        site = rng.choice(["north", "central", "south"])
        baseline_bp = round(rng.gauss(132 + (age - 52) * 0.18, 12), 1)
        treatment_effect = -6.0 if group == "treatment" else -1.0
        followup_bp = round(baseline_bp + treatment_effect + rng.gauss(0, 8), 1)
        biomarker_pre = round(rng.lognormvariate(2.3, 0.35), 2)
        biomarker_change = rng.gauss(-1.8 if group == "treatment" else -0.3, 1.4)
        biomarker_post = round(max(0.1, biomarker_pre + biomarker_change), 2)
        enrolled_on = enrollment_start + timedelta(days=rng.randrange(0, 150))

        # 制造两种缺失机制：随访脱落，以及少量独立生物标志物缺失。
        dropout = rng.random() < (0.11 if group == "control" else 0.07)
        if dropout:
            followup_bp = None
            biomarker_post = None
        elif rng.random() < 0.05:
            biomarker_post = None

        rows.append(
            {
                "participant_id": f"P{index + 1:04d}",
                "group": group,
                "age": age,
                "sex": sex,
                "site": site,
                "enrolled_on": enrolled_on.isoformat(),
                "baseline_sbp": baseline_bp,
                "followup_sbp": followup_bp,
                "biomarker_pre": biomarker_pre,
                "biomarker_post": biomarker_post,
            }
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUTPUT_PATH, index=False, encoding="utf-8")
    print(f"Generated {ROW_COUNT} rows at {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
