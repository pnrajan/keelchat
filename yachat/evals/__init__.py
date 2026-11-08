"""Evaluation harness: replay sets (scripted users and personas the model plays), expectations checked in code, and
metrics from DeepEval and Ragas scored by a judge model. Runs on the simulator's engine (`yachat.sim.harness`).

The platform owns the harness; each team owns its eval set in `evals/<agent>/` (`replay.yaml`, `thresholds.yaml`).
Everything specific to an agent (scripts, personas, rubrics, the policy under test) lives in those files.
Install with `pip install 'yachat[evals]'`; run with `yachat evals run`.
"""
