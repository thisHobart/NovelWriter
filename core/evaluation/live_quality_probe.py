"""Cost-gated live-model probe for the fixed mystery quality run."""
from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

from agents.review.domain_review_agent import DomainReviewAgent
from core.generation.ai_helper import get_backend, send_prompt, set_backend
from core.generation.domain_profiles import get_domain_profile
from core.generation.llm_trace import trace_session, trace_stage


RUN_ID = "live_plausibility_probe_v1"
PROSE = "方屿砸碎唯一的便携硬盘。技术员把碎片接上导线，几秒后恢复了完整录像，并把录像当成锁定方屿的核心证据。"
CASE_BIBLE = {
    "domain_rules": {
        "model": "当代中国现实证据与普通电子设备",
        "baseline_rules": ["损坏原件不得虚构恢复结果"],
        "limits": ["没有超自然能力或未来数据恢复技术"],
    }
}


def _target() -> tuple[str, int]:
    raw_url = (os.environ.get("HOSTED_LLM_URL") or "localhost").strip()
    parsed = urlparse(raw_url if "://" in raw_url else f"http://{raw_url}")
    host = parsed.hostname or "localhost"
    port = int(os.environ.get("HOSTED_LLM_PORT") or parsed.port or 23333)
    return host, port


def _reachable(host: str, port: int, timeout: float = 1.5) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def run_probe(output_dir: str | Path) -> tuple[int, dict]:
    root = Path(output_dir)
    run_dir = root / "system" / "quality_runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    host, port = _target()
    reachable, error = _reachable(host, port)
    recovery = (
        f".\\.venv\\Scripts\\python.exe -m core.evaluation.live_quality_probe "
        f"--output-dir \"{root}\""
    )
    if not reachable:
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": "blocked",
            "configured_backend": "api",
            "configured_model": "hosted-llm",
            "upstream_model": os.environ.get("HOSTED_LLM_MODEL", "unset"),
            "endpoint_scope": "local" if host in {"localhost", "127.0.0.1", "::1"} else "remote",
            "port": port,
            "attempted_model_calls": 0,
            "reason": f"configured endpoint is not accepting TCP connections ({error})",
            "recovery": [
                f"启动或恢复监听 {host}:{port} 的 OpenAI 兼容服务",
                recovery,
                "只有该单阶段探针通过后，才运行单章和固定三章真实生成",
            ],
        }
        (run_dir / "blocker.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 2, report

    set_backend("api", "hosted-llm")
    reviewer = DomainReviewAgent(
        model="hosted-llm",
        profile=get_domain_profile("legal_suspense"),
        send_prompt_fn=send_prompt,
    )
    try:
        with trace_session(root, run_id=RUN_ID, mode="real") as recorder:
            with trace_stage("plausibility_review"):
                review = reviewer.review_plausibility(PROSE, CASE_BIBLE)
        actual_calls = len(recorder.calls)
        schema_retries = sum(
            "不符合 schema" in call["prompt"] for call in recorder.calls
        )
        detected = any(
            failure.get("code") in {"EVIDENCE_SELF_DESTRUCTION", "IMPOSSIBLE_MECHANISM"}
            for failure in review.hard_failures
        )
        status = "passed" if (not review.passed and detected) else "quality_failed"
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": status,
            "configured_backend": get_backend(),
            "configured_model": "hosted-llm",
            "attempted_model_calls": actual_calls,
            "schema_retry_calls": schema_retries,
            "expected": "reject destroyed-evidence inference with a quoted, minimal repair",
            "review": review.to_dict(),
            "continue_to_single_chapter": status == "passed",
        }
        code = 0 if status == "passed" else 1
    except Exception as exc:
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": "blocked",
            "configured_backend": get_backend(),
            "configured_model": "hosted-llm",
            "attempted_model_calls": len(recorder.calls) if "recorder" in locals() else 0,
            "reason": f"{type(exc).__name__}: {exc}",
            "recovery": [recovery],
        }
        code = 2
    (run_dir / "probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return code, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行真实模型的低成本现实合理性单阶段探针")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    code, report = run_probe(args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
