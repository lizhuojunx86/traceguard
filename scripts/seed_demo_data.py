"""Seed demo data into traces.db for dashboard demonstration."""
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from guardian.store.writer import TraceWriter

DB_URL = "sqlite:///traces.db"


def main():
    writer = TraceWriter(DB_URL)
    now = datetime.now(timezone.utc)
    random.seed(42)

    pipelines = [
        {
            "name": "market-intelligence",
            "steps": ["step_01_collect", "step_02_analyze"],
        },
        {
            "name": "quant-research",
            "steps": ["step_01_fetch", "step_02_process", "step_03_report"],
        },
    ]

    total = 0
    for pipe in pipelines:
        for step_idx, step in enumerate(pipe["steps"]):
            for day in range(30):
                # 2-5 runs per day
                runs = random.randint(2, 5)
                for _ in range(runs):
                    ts = now - timedelta(
                        days=day,
                        hours=random.randint(0, 23),
                        minutes=random.randint(0, 59),
                    )

                    # Simulate quality degradation in recent days for step_02
                    if step_idx == 1 and day < 3:
                        score = round(random.uniform(0.3, 0.6), 2)
                        passed = random.random() < 0.3
                    elif step_idx == 1 and day < 7:
                        score = round(random.uniform(0.6, 0.85), 2)
                        passed = random.random() < 0.7
                    else:
                        score = round(random.uniform(0.8, 1.0), 2)
                        passed = random.random() < 0.9

                    if passed:
                        action = "pass"
                        issues = []
                    else:
                        action = random.choice(["retry", "abort", "alert"])
                        issues = random.sample(
                            [
                                "Missing required field: data",
                                "Output too short",
                                "JSON Schema validation failed",
                                "Language mismatch",
                                "Semantic score below threshold",
                            ],
                            k=random.randint(1, 3),
                        )

                    writer.write(
                        pipeline_name=pipe["name"],
                        step_name=step,
                        action=action,
                        passed=passed,
                        score=score,
                        issues=issues,
                        attempt=1 if passed else random.randint(1, 3),
                        output_preview=f"Demo output for {step} at {ts.isoformat()}",
                    )
                    total += 1

    print(f"Seeded {total} traces into {DB_URL}")


if __name__ == "__main__":
    main()
