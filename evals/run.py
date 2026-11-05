"""Executable eval runner for AIGate.

Reads YAML case files (schema: ``evals/README.md``) and drives a *running*
gateway over HTTP, so what gets tested is the deployed pipeline
(auth -> quota -> cache -> routing -> fallback -> metering), not an in-process
ASGI shortcut.

    uv run python evals/run.py                       # smoke.yaml
    uv run python evals/run.py regression            # evals/datasets/regression.yaml
    uv run python evals/run.py smoke regression --md report.md
    AIGATE_EVAL_BASE_URL=http://127.0.0.1:8092 uv run python evals/run.py

Exit codes: 0 = every case passed, 1 = at least one case failed, 2 = harness
error (bad dataset, gateway unreachable, missing credentials).

httpx is created with ``trust_env=False`` on purpose: a system HTTP proxy
silently swallows requests to 127.0.0.1 on some machines, and the symptom looks
exactly like "the server never came up".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATASET_DIR = HERE / "datasets"
DEFAULT_DATASET = "smoke.yaml"

CHAT_PATH = "/v1/chat/completions"
TRACE_PATH = "/admin/v1/traces/{request_id}"
PROJECTS_PATH = "/admin/v1/projects"
KEYS_PATH = "/admin/v1/keys"
MODELS_PATH = "/admin/v1/models"

REFUSAL_MARKERS = (
    "无法",
    "不能",
    "抱歉",
    "不知道",
    "i can't",
    "i cannot",
    "unable to",
    "not able to",
)

KNOWN_ASSERTS = (
    "http_status",
    "error_code",
    "contains",
    "not_contains",
    "not_empty",
    "degraded",
    "fallback_attempts",
    "cache_hit",
    "model_used",
    "finish_reason",
    "stream_error_code",
    "max_latency_ms",
    "max_cost_cny",
    "min_cost_cny",
    "must_refuse",
    "json_schema",
)


class HarnessError(RuntimeError):
    """The harness itself cannot run — not a case failure."""


class DatasetError(HarnessError):
    """Dataset is malformed or uses an unsupported assertion."""


@dataclass
class Probe:
    """What one HTTP call actually returned, normalized for assertions."""

    status: int
    elapsed_ms: int
    content: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    finish_reason: str | None = None
    stream_error: str | None = None
    request_id: str | None = None

    @property
    def error_code(self) -> str | None:
        if isinstance(self.body, dict):
            err = self.body.get("error")
            if isinstance(err, dict):
                code = err.get("code")
                return code if isinstance(code, str) else None
        return self.stream_error


@dataclass
class Check:
    name: str
    expected: Any
    actual: Any
    ok: bool


@dataclass
class CaseResult:
    case: dict[str, Any]
    probes: list[Probe] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def last(self) -> Probe | None:
        return self.probes[-1] if self.probes else None


@dataclass
class DatasetRun:
    path: Path
    cases: list[CaseResult]

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def failed(self) -> int:
        return len(self.cases) - self.passed


# ---------------------------------------------------------------------------
# dataset loading / validation
# ---------------------------------------------------------------------------


def resolve_dataset(name: str) -> Path:
    candidate = Path(name)
    if candidate.is_file():
        return candidate
    for suffix in ("", ".yaml", ".yml"):
        candidate = DATASET_DIR / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    raise DatasetError(f"dataset not found: {name} (looked in {DATASET_DIR})")


def _assert_block(case_id: str, label: str, block: Any) -> dict[str, Any]:
    if not isinstance(block, dict) or not block:
        raise DatasetError(f"{case_id}: '{label}' must be a non-empty mapping")
    for key, value in block.items():
        if key not in KNOWN_ASSERTS:
            raise DatasetError(
                f"{case_id}: unknown assertion '{key}' (allowed: {', '.join(sorted(KNOWN_ASSERTS))})"
            )
        if key in ("contains", "not_contains"):
            valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
        elif key in ("http_status", "fallback_attempts", "max_latency_ms"):
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif key in ("max_cost_cny", "min_cost_cny"):
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif key in ("not_empty", "degraded", "must_refuse"):
            valid = isinstance(value, bool)
        elif key == "cache_hit":
            valid = value is None or isinstance(value, str)
        elif key == "json_schema":
            valid = isinstance(value, dict)
        else:
            valid = isinstance(value, str)
        if not valid:
            raise DatasetError(f"{case_id}: assertion '{key}' has wrong type: {type(value).__name__}")
    return block


def load_dataset(path: Path) -> list[dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise DatasetError(f"{path}: top level must be a non-empty list of cases")

    seen: set[str] = set()
    cases: list[dict[str, Any]] = []
    for index, case in enumerate(raw, start=1):
        if not isinstance(case, dict):
            raise DatasetError(f"{path}: case #{index} is not a mapping")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise DatasetError(f"{path}: case #{index} needs a string 'id'")
        if case_id in seen:
            raise DatasetError(f"{path}: duplicate case id '{case_id}'")
        seen.add(case_id)

        for required in ("intent", "model"):
            if not isinstance(case.get(required), str) or not case[required]:
                raise DatasetError(f"{case_id}: missing string '{required}'")
        if not isinstance(case.get("messages"), list):
            raise DatasetError(f"{case_id}: 'messages' must be a list")
        if case.get("judge"):
            raise DatasetError(
                f"{case_id}: 'judge' rubrics are not wired up; assert deterministically instead"
            )
        if case.get("auth") not in (None, "none", "invalid"):
            raise DatasetError(f"{case_id}: 'auth' must be none/invalid")
        if "stream" in case and not isinstance(case["stream"], bool):
            raise DatasetError(f"{case_id}: 'stream' must be a boolean")
        repeat = case.get("repeat", 1)
        if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1:
            raise DatasetError(f"{case_id}: 'repeat' must be a positive integer")
        if case.get("assert_first") is not None and repeat < 2:
            raise DatasetError(f"{case_id}: 'assert_first' needs repeat >= 2")

        case["assert"] = _assert_block(case_id, "assert", case.get("assert"))
        if case.get("assert_first") is not None:
            case["assert_first"] = _assert_block(case_id, "assert_first", case["assert_first"])
        cases.append(case)
    return cases


# ---------------------------------------------------------------------------
# request / response handling
# ---------------------------------------------------------------------------


def build_payload(case: dict[str, Any], nonce: str, run_id: str) -> dict[str, Any]:
    def render(node: Any) -> Any:
        if isinstance(node, str):
            return node.replace("{nonce}", nonce).replace("{run_id}", run_id)
        if isinstance(node, list):
            return [render(item) for item in node]
        if isinstance(node, dict):
            return {key: render(value) for key, value in node.items()}
        return node

    payload: dict[str, Any] = {"model": case["model"], "messages": render(case["messages"])}
    if "temperature" in case:
        payload["temperature"] = case["temperature"]
    if case.get("stream"):
        payload["stream"] = True
    payload.update(render(case.get("params") or {}))
    return payload


def parse_probe(response: httpx.Response, stream: bool, elapsed_ms: int) -> Probe:
    probe = Probe(status=response.status_code, elapsed_ms=elapsed_ms)
    request_id = response.headers.get("x-request-id")

    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        probe.body = body
        meta = body.get("x_aigate")
        if isinstance(meta, dict):
            probe.meta = meta
            request_id = meta.get("request_id") or request_id
        error = body.get("error")
        if isinstance(error, dict):
            probe.stream_error = error.get("code") if isinstance(error.get("code"), str) else None
            request_id = error.get("request_id") or request_id

    if stream and response.status_code == 200:
        parts: list[str] = []
        for raw_line in response.text.splitlines():
            line = raw_line.strip()
            if line.startswith(": aigate "):
                try:
                    meta = json.loads(line[len(": aigate ") :])
                except ValueError:
                    continue
                if isinstance(meta, dict):
                    probe.meta = meta
                    request_id = meta.get("request_id") or request_id
                continue
            if not line.startswith("data: "):
                continue
            data = line[len("data: ") :]
            if data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            error = event.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                probe.stream_error = code if isinstance(code, str) else "upstream_error"
                request_id = error.get("request_id") or request_id
                continue
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                chunk = delta.get("content")
                if isinstance(chunk, str):
                    parts.append(chunk)
                if choice.get("finish_reason"):
                    probe.finish_reason = choice["finish_reason"]
        probe.content = "".join(parts)
    else:
        choices = (probe.body or {}).get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            probe.content = content if isinstance(content, str) else ""
            probe.finish_reason = choices[0].get("finish_reason")

    probe.request_id = request_id
    return probe


def trace_attempts(
    client: httpx.Client, request_id: str | None, admin_token: str | None
) -> tuple[int | None, Any]:
    """Count real upstream attempts from the control-plane trace.

    ``route_path`` is where fallback decisions are recorded; the data-plane body
    only exposes the final outcome, so this is the only honest way to assert
    "the fallback candidate was never called".
    """
    if not request_id:
        return None, "response carries no request_id"

    headers = {"X-Admin-Token": admin_token} if admin_token else {}
    last_error: Any = "trace not available"
    for _ in range(10):
        response = client.get(TRACE_PATH.format(request_id=request_id), headers=headers)
        if response.status_code == 200:
            path = response.json().get("route_path") or []
            attempts = [
                entry
                for entry in path
                if isinstance(entry, dict) and "provider" in entry and not entry.get("skipped")
            ]
            return len(attempts), [entry.get("provider") for entry in attempts]
        last_error = f"trace HTTP {response.status_code}"
        time.sleep(0.05)
    return None, last_error


# ---------------------------------------------------------------------------
# assertions
# ---------------------------------------------------------------------------


def _json_type_ok(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Tiny JSON-Schema subset: type / required / properties / items / enum.

    Deliberately not a dependency: eval assertions should stay deterministic and
    offline. If a dataset needs more, add ``jsonschema`` and replace this.
    """
    problems: list[str] = []
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _json_type_ok(value, expected_type):
        return [f"{path}: expected {expected_type}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} not in {schema['enum']!r}")
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                problems.append(f"{path}: missing required '{key}'")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value and isinstance(sub, dict):
                problems.extend(validate_schema(value[key], sub, f"{path}.{key}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            problems.extend(validate_schema(item, schema["items"], f"{path}[{index}]"))
    return problems


def check_case(
    client: httpx.Client,
    probe: Probe,
    block: dict[str, Any],
    label: str,
    admin_token: str | None,
) -> list[Check]:
    checks: list[Check] = []
    for name, expected in block.items():
        actual: Any
        ok: bool
        if name == "http_status":
            actual, ok = probe.status, probe.status == expected
        elif name == "error_code":
            actual, ok = probe.error_code, probe.error_code == expected
        elif name == "contains":
            missing = [needle for needle in expected if needle not in probe.content]
            actual = "all present" if not missing else f"missing {missing}"
            ok = not missing
        elif name == "not_contains":
            found = [needle for needle in expected if needle in probe.content]
            actual = "none present" if not found else f"found {found}"
            ok = not found
        elif name == "not_empty":
            has_text = bool(probe.content.strip())
            actual, ok = has_text, has_text is expected
        elif name == "degraded":
            actual = probe.meta.get("degraded", "<missing x_aigate.degraded>")
            ok = actual is expected
        elif name == "fallback_attempts":
            count, detail = trace_attempts(client, probe.request_id, admin_token)
            actual = count if count is not None else detail
            ok = count == expected
        elif name == "cache_hit":
            actual = probe.meta.get("cache_hit", "<missing x_aigate.cache_hit>")
            ok = actual == expected
        elif name == "model_used":
            actual, ok = probe.meta.get("model_used"), probe.meta.get("model_used") == expected
        elif name == "finish_reason":
            actual, ok = probe.finish_reason, probe.finish_reason == expected
        elif name == "stream_error_code":
            actual, ok = probe.stream_error, probe.stream_error == expected
        elif name == "max_latency_ms":
            latency = (probe.meta.get("latency_ms") or {}).get("gateway_total", probe.elapsed_ms)
            actual, ok = latency, isinstance(latency, int) and latency <= expected
        elif name in ("max_cost_cny", "min_cost_cny"):
            cost = probe.meta.get("cost_cny")
            if name == "max_cost_cny":
                actual, ok = cost, isinstance(cost, (int, float)) and cost <= expected
            else:
                actual, ok = cost, isinstance(cost, (int, float)) and cost >= expected
        elif name == "must_refuse":
            lowered = probe.content.lower()
            refused = any(marker in lowered for marker in REFUSAL_MARKERS)
            actual, ok = ("refused" if refused else probe.content[:120]), refused is expected
        elif name == "json_schema":
            try:
                parsed = json.loads(probe.content)
            except ValueError as exc:
                actual, ok = f"content is not JSON: {exc}", False
            else:
                problems = validate_schema(parsed, expected)
                actual, ok = ("valid" if not problems else "; ".join(problems[:5])), not problems
        else:  # pragma: no cover - load_dataset rejects unknown keys first
            actual, ok = f"unhandled assertion '{name}'", False
        checks.append(Check(name=f"{label}{name}", expected=expected, actual=actual, ok=ok))
    return checks


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def auth_headers(case: dict[str, Any], api_key: str) -> dict[str, str]:
    mode = case.get("auth")
    if mode == "none":
        return {}
    if mode == "invalid":
        return {"Authorization": "Bearer sk-ag-invalid-key-000000000000"}
    return {"Authorization": f"Bearer {api_key}"}


def run_case(
    client: httpx.Client,
    case: dict[str, Any],
    api_key: str,
    nonce: str,
    run_id: str,
    admin_token: str | None,
) -> CaseResult:
    result = CaseResult(case=case)
    payload = build_payload(case, nonce, run_id)
    headers = auth_headers(case, api_key)
    stream = bool(payload.get("stream"))

    for _ in range(int(case.get("repeat", 1))):
        started = time.perf_counter()
        response = client.post(CHAT_PATH, json=payload, headers=headers)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        result.probes.append(parse_probe(response, stream, elapsed_ms))

    if case.get("assert_first") and len(result.probes) >= 2:
        result.checks.extend(
            check_case(client, result.probes[0], case["assert_first"], "first.", admin_token)
        )
    result.checks.extend(check_case(client, result.probes[-1], case["assert"], "", admin_token))
    return result


def mint_api_key(
    client: httpx.Client,
    admin_token: str,
    project_id: str | None,
    allowed_models: str = "",
) -> str:
    """Mint a key scoped to the models the dataset actually exercises.

    Scope matters: an unscoped key makes the "forbidden model" case meaningless,
    because the gateway lets unknown models fall through to the default route.
    Placeholder models (``__forbidden_model__``) are deliberately left out.
    """
    admin = {"X-Admin-Token": admin_token}
    if not project_id:
        projects = client.get(PROJECTS_PATH, headers=admin)
        if projects.status_code != 200:
            raise HarnessError(f"cannot list projects: HTTP {projects.status_code} {projects.text[:200]}")
        items = projects.json().get("items") or []
        if not items:
            raise HarnessError("no project exists; run scripts/seed.py first")
        project_id = items[0]["id"]

    created = client.post(
        KEYS_PATH,
        params={
            "project_id": project_id,
            "name": f"eval-runner-{int(time.time())}",
            "allowed_models": allowed_models,
        },
        headers=admin,
    )
    if created.status_code not in (200, 201):
        raise HarnessError(f"cannot mint API key: HTTP {created.status_code} {created.text[:200]}")
    return created.json()["api_key"]


def preflight(client: httpx.Client, admin_token: str, cases: list[dict[str, Any]]) -> list[str]:
    """Warn about datasets whose virtual model has no route policy.

    An unrouted model silently falls through to the default provider, which makes
    a "should be 403/502" case look like a cheerful 200. Say it loudly.
    """
    try:
        response = client.get(MODELS_PATH, headers={"X-Admin-Token": admin_token})
    except httpx.HTTPError:
        return []
    if response.status_code != 200:
        return []
    policies = {p.get("match_model") for p in response.json().get("policies") or []}
    missing = sorted(
        {
            case["model"]
            for case in cases
            if case["model"] not in policies and not case["model"].startswith("__")
        }
    )
    if not missing:
        return []
    return [
        f"warning: no route policy for {missing}; those cases will hit the default provider. "
        "Run `uv run python evals/bootstrap.py` to install eval fixtures."
    ]


def render_report(
    runs: list[DatasetRun],
    base_url: str,
    started: datetime,
    duration_s: float,
    gateway: dict[str, Any],
) -> str:
    total = sum(len(run.cases) for run in runs)
    passed = sum(run.passed for run in runs)
    failed = total - passed
    verdict = "PASS" if failed == 0 else "FAIL"

    lines = [
        "# AIGate eval report",
        "",
        f"- **verdict**: {verdict} ({passed}/{total} cases passed)",
        f"- **base url**: `{base_url}`",
        f"- **gateway**: {gateway.get('app', '?')} {gateway.get('version', '?')} ({gateway.get('env', '?')})",
        f"- **started**: {started.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **duration**: {duration_s:.1f}s",
        "",
        "## Datasets",
        "",
        "| dataset | cases | passed | failed |",
        "|---|---:|---:|---:|",
    ]
    for run in runs:
        lines.append(f"| `{run.path.name}` | {len(run.cases)} | {run.passed} | {run.failed} |")

    for run in runs:
        lines += [
            "",
            f"## {run.path.name}",
            "",
            "| # | id | intent | result | http | ms | request_id |",
            "|---:|---|---|---|---:|---:|---|",
        ]
        for index, result in enumerate(run.cases, start=1):
            case = result.case
            probe = result.last
            status = probe.status if probe else "-"
            elapsed = probe.elapsed_ms if probe else "-"
            request_id = (probe.request_id if probe else None) or "-"
            mark = "PASS" if result.passed else "FAIL"
            lines.append(
                f"| {index} | `{case['id']}` | {case['intent']} | {mark} | {status} | "
                f"{elapsed} | `{request_id}` |"
            )

    failures = [(run, result) for run in runs for result in run.cases if not result.passed]
    if failures:
        lines += ["", "## Failures", ""]
        for run, result in failures:
            case = result.case
            lines += [f"### `{case['id']}` — {case['intent']}", ""]
            if result.error:
                lines += [f"harness error: `{result.error}`", ""]
                continue
            lines += ["| assertion | expected | actual |", "|---|---|---|"]
            for check in result.checks:
                if check.ok:
                    continue
                expected = json.dumps(check.expected, ensure_ascii=False)
                actual = json.dumps(check.actual, ensure_ascii=False, default=str)
                lines.append(f"| `{check.name}` | `{expected}` | `{actual}` |")
            probe = result.last
            if probe is not None:
                lines += [
                    "",
                    f"- http: {probe.status}, error_code: `{probe.error_code}`, "
                    f"request_id: `{probe.request_id}`",
                    f"- dataset: `{run.path.name}`",
                    "",
                    "```text",
                    probe.content[:400] or "<empty>",
                    "```",
                ]
            lines.append("")

    lines += ["", "## Notes", ""]
    lines.append("- `fallback_attempts` is read from the control-plane trace (`/admin/v1/traces/{id}`).")
    lines.append("- Cache cases use a per-run `{nonce}` so a warm DB cannot fake a miss.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run AIGate YAML eval datasets against a live gateway.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("datasets", nargs="*", help="dataset names or paths (default: smoke.yaml)")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AIGATE_EVAL_BASE_URL", "http://127.0.0.1:8080"),
        help="gateway base URL",
    )
    parser.add_argument("--api-key", default=os.environ.get("AIGATE_EVAL_API_KEY"), help="data-plane API key")
    parser.add_argument(
        "--admin-token",
        default=os.environ.get("AIGATE_ADMIN_TOKEN", "dev-admin-token"),
        help="control-plane token (trace lookups + key minting)",
    )
    parser.add_argument("--project-id", default=os.environ.get("AIGATE_EVAL_PROJECT_ID"))
    parser.add_argument(
        "--allow-all-models",
        action="store_true",
        help="mint an unscoped key instead of one limited to the dataset's models",
    )
    parser.add_argument("--filter", default=None, help="only run cases whose id contains this text")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout (seconds)")
    parser.add_argument("--md", default=None, help="also write the markdown report here")
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="also write machine-readable JSON here",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    names = args.datasets or [DEFAULT_DATASET]

    try:
        paths = [resolve_dataset(name) for name in names]
        datasets = {path: load_dataset(path) for path in paths}
    except DatasetError as exc:
        print(f"eval harness error: {exc}", file=sys.stderr)
        return 2

    run_id = uuid.uuid4().hex[:8]
    started = datetime.now()
    started_perf = time.perf_counter()
    runs: list[DatasetRun] = []

    try:
        with httpx.Client(
            base_url=args.base_url.rstrip("/"),
            timeout=httpx.Timeout(args.timeout),
            trust_env=False,
        ) as client:
            try:
                health = client.get("/health")
            except httpx.HTTPError as exc:
                raise HarnessError(f"gateway unreachable at {args.base_url}: {exc}") from exc
            if health.status_code != 200:
                raise HarnessError(f"gateway health check failed: HTTP {health.status_code}")
            gateway = health.json()

            selected: dict[Path, list[dict[str, Any]]] = {}
            for path, cases in datasets.items():
                if args.filter:
                    cases = [case for case in cases if args.filter in case["id"]]
                if not cases:
                    raise HarnessError(f"--filter {args.filter!r} matched no case in {path.name}")
                selected[path] = cases
            all_cases = [case for cases in selected.values() for case in cases]

            scope = ""
            if not args.allow_all_models:
                scope = ",".join(
                    sorted({case["model"] for case in all_cases if not case["model"].startswith("__")})
                )
            api_key = args.api_key or mint_api_key(client, args.admin_token, args.project_id, scope)

            for warning in preflight(client, args.admin_token, all_cases):
                print(warning, file=sys.stderr)

            for path, cases in selected.items():
                run = DatasetRun(path=path, cases=[])
                for case in cases:
                    try:
                        run.cases.append(
                            run_case(
                                client,
                                case,
                                api_key,
                                uuid.uuid4().hex[:8],
                                run_id,
                                args.admin_token,
                            )
                        )
                    except httpx.HTTPError as exc:
                        raise HarnessError(f"{case['id']}: transport error: {exc}") from exc
                runs.append(run)
    except HarnessError as exc:
        print(f"eval harness error: {exc}", file=sys.stderr)
        return 2

    duration_s = time.perf_counter() - started_perf
    report = render_report(runs, args.base_url, started, duration_s, gateway)
    print(report)

    if args.md:
        Path(args.md).write_text(report, encoding="utf-8")
        print(f"markdown report written to {args.md}", file=sys.stderr)
    if args.json_path:
        payload = {
            "verdict": "pass" if all(run.failed == 0 for run in runs) else "fail",
            "base_url": args.base_url,
            "started": started.isoformat(timespec="seconds"),
            "duration_s": round(duration_s, 3),
            "datasets": [
                {
                    "name": run.path.name,
                    "passed": run.passed,
                    "failed": run.failed,
                    "cases": [
                        {
                            "id": result.case["id"],
                            "intent": result.case["intent"],
                            "passed": result.passed,
                            "http_status": result.last.status if result.last else None,
                            "request_id": result.last.request_id if result.last else None,
                            "checks": [
                                {
                                    "assertion": check.name,
                                    "expected": check.expected,
                                    "actual": check.actual,
                                    "ok": check.ok,
                                }
                                for check in result.checks
                            ],
                        }
                        for result in run.cases
                    ],
                }
                for run in runs
            ],
        }
        Path(args.json_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json report written to {args.json_path}", file=sys.stderr)

    failed = sum(run.failed for run in runs)
    total = sum(len(run.cases) for run in runs)
    print(f"\n{total - failed}/{total} cases passed", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
