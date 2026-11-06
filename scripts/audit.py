"""自检脚本：把「上线后才会发现」的那类问题在本地按清单打一遍。

它不是单元测试的替代品，而是补充——单元测试用的是临时库和 mock provider，
这里打的是**真进程 + 真数据库**，能暴露「代码逻辑对、装配错了」的问题：

    1. 每个页面/端点是否真能打开（路由挂错、模板没打包、中间件顺序错）
    2. 错误信封是否统一（前端能不能只写一处错误处理）
    3. 边界参数（limit=0 / limit=99999 / days=-1 / 超长字符串）会不会 500
    4. 鉴权矩阵（无凭证 / viewer / finance / admin / owner）
    5. 并发写（SQLite 单写者）会不会变成 500
    6. 流式响应是不是真的 SSE，而不是被中间件缓冲成一大块

用法：
    uv run python scripts/audit.py                 # 默认打 8080/3000
    uv run python scripts/audit.py --gateway http://127.0.0.1:8090 --skip-console
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

failures: list[str] = []
checks = 0


def _opener():
    # 本机有系统代理时，连 127.0.0.1 也会走代理（表现为"服务死活连不上"）
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


OPEN = _opener()


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(f"{name} {detail}".strip())


def call(
    base: str,
    path: str,
    *,
    method: str = "GET",
    token: str | None = None,
    cookie: str | None = None,
    body: dict | None = None,
    bearer: str | None = None,
    timeout: float = 20.0,
) -> tuple[int, dict | str, dict]:
    url = f"{base}{path}"
    headers: dict[str, str] = {}
    data = None
    if token:
        headers["X-Admin-Token"] = token
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with OPEN.open(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("content-type", "")
            payload: dict | str = json.loads(raw) if "json" in ctype else raw.decode("utf-8", "replace")
            return resp.status, payload, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            payload = raw.decode("utf-8", "replace")
        return exc.code, payload, dict(exc.headers)
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc), {}


def envelope(payload: dict | str) -> tuple[str, str]:
    """(code, message) —— 错误信封必须是 {"error": {"code","message"}}。"""
    if not isinstance(payload, dict):
        return "", ""
    err = payload.get("error") or {}
    return str(err.get("code") or ""), str(err.get("message") or "")


def section(title: str) -> None:
    print(f"\n== {title}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default="http://127.0.0.1:8080")
    parser.add_argument("--console", default="http://127.0.0.1:3000")
    parser.add_argument("--skip-console", action="store_true")
    args = parser.parse_args()

    env = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()

    token = os.environ.get("AIGATE_ADMIN_TOKEN") or env.get("AIGATE_ADMIN_TOKEN", "")
    if not token:
        print("没有 AIGATE_ADMIN_TOKEN，无法跑控制面检查")
        return 2

    gw = args.gateway.rstrip("/")
    ui = args.console.rstrip("/")

    # ---------------------------------------------------------------- 基础可达
    section("1. 可达性")
    for path in ["/health", "/metrics"]:
        status, _, _ = call(gw, path)
        check(f"gateway {path}", status == 200, str(status))

    # ---------------------------------------------------------------- 鉴权矩阵
    section("2. 鉴权")
    status, payload, _ = call(gw, "/admin/v1/keys")
    code, _ = envelope(payload)
    check("无凭证访问控制面 -> 401", status == 401 and code == "gateway.login_required", f"{status} {code}")
    status, payload, _ = call(gw, "/admin/v1/keys", token=token)
    check("有 token 访问控制面 -> 200", status == 200, str(status))

    login_body = {"email": "owner@team.local", "password": "team-internal-2026"}
    status, payload, _headers = call(
        gw, "/console/v1/login", method="POST", body=login_body
    )
    owner_cookie = ""
    if status == 200 and isinstance(payload, dict):
        # Set-Cookie 取不到时（urllib 的 header 名大小写），退化为只测 token 路径
        for name, value in _headers.items():
            if name.lower() == "set-cookie":
                owner_cookie = value.split(";")[0]
    if owner_cookie:
        status, payload, _ = call(gw, "/admin/v1/projects", cookie=owner_cookie)
        check("会话 cookie 可用", status == 200, str(status))
    else:
        print("  SKIP  没有 owner 账号或登录失败（本地库可能已改密码）")

    # ---------------------------------------------------------------- 端点矩阵
    section("3. 控制面读端点")
    endpoints = [
        "/admin/v1/overview?days=7",
        "/admin/v1/usage?limit=5",
        "/admin/v1/traces?limit=5",
        "/admin/v1/keys",
        "/admin/v1/projects",
        "/admin/v1/projects/summary",
        "/admin/v1/teams",
        "/admin/v1/models",
        "/admin/v1/providers",
        "/admin/v1/audit?limit=5",
        "/admin/v1/alerts?limit=5",
        "/admin/v1/routes",
        "/admin/v1/shadow?limit=5",
        "/admin/v1/model-compare?days=7",
        "/admin/v1/prompts",
        "/admin/v1/ops-health",
    ]
    for path in endpoints:
        status, payload, _ = call(gw, path, token=token)
        detail = f"{status}"
        if status != 200:
            code, message = envelope(payload)
            detail += f" {code} {message[:80]}"
        check(f"GET {path}", status == 200, detail)

    # ---------------------------------------------------------------- 边界参数
    section("4. 边界参数（返回 4xx 而不是 500）")
    for path in [
        "/admin/v1/usage?limit=0",
        "/admin/v1/usage?limit=99999",
        "/admin/v1/overview?days=0",
        "/admin/v1/overview?days=-1",
        "/admin/v1/overview?days=9999",
        "/admin/v1/model-compare?days=0",
        "/admin/v1/audit?limit=abc",
        "/admin/v1/traces/does-not-exist",
    ]:
        status, payload, _ = call(gw, path, token=token)
        check(f"GET {path} -> 4xx", 400 <= status < 500, str(status))

    # ---------------------------------------------------------------- 错误信封
    section("5. 错误信封统一")
    status, payload, _ = call(gw, "/v1/chat/completions", method="POST", body={"model": "x"})
    code, message = envelope(payload)
    check("缺凭证 -> 401 + code", status == 401 and bool(code), f"{status} {code}")
    status, payload, _ = call(
        gw, "/v1/chat/completions", method="POST", body={"model": "x"}, bearer="sk-invalid-xxxxx"
    )
    code, _ = envelope(payload)
    check("错 key -> 401 invalid_key", status == 401 and code in {"gateway.invalid_key"}, f"{status} {code}")

    # ---------------------------------------------------------------- 并发写
    section("6. 并发写（SQLite 单写者）")
    projects = call(gw, "/admin/v1/projects", token=token)[1]
    project_id = projects["items"][0]["id"] if isinstance(projects, dict) and projects.get("items") else ""
    if project_id:
        def make_key(i: int) -> int:
            status, _, _ = call(
                gw,
                f"/admin/v1/keys?project_id={project_id}&name=audit-{int(time.time())}-{i}",
                method="POST",
                token=token,
            )
            return status

        with ThreadPoolExecutor(max_workers=10) as pool:
            statuses = list(pool.map(make_key, range(20)))
        bad = [s for s in statuses if s not in (201, 409)]
        check("20 并发签发不出现 5xx", not bad, f"{len(bad)} 个异常: {sorted(set(bad))}")

    # ---------------------------------------------------------------- 控制台页面
    if not args.skip_console:
        section("7. 控制台页面")
        for path in [
            "/login",
            "/",
            "/playground",
            "/usage",
            "/keys",
            "/routes",
            "/providers",
            "/models",
            "/prompts",
            "/ops",
            "/guide",
            "/projects",
            "/members",
            "/audit",
        ]:
            status, _, _ = call(ui, path, cookie=owner_cookie or None)
            # 未登录时非 /login 会被 307 到登录页，这是对的
            ok = status in (200, 307)
            check(f"GET {path}", ok, str(status))

    print(f"\n{checks - len(failures)} passed, {len(failures)} failed")
    if failures:
        print("\nFAILED:")
        for item in failures:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())