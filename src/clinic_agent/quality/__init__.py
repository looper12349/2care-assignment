"""All scenario scoring is executed by LangSmith's evaluation SDK."""

from clinic_agent.quality.runner import run_eval_loop

__all__ = ["run_eval_loop"]
