"use client";

import { useEffect, useState } from "react";
import { adminGet } from "@/lib/api";

/**
 * 接入指南：给第一次接这个网关的同事看的一页。
 *
 * 为什么放在控制台里而不是只写在 README：README 是给"要不要用这个项目"的人看的，
 * 这一页是给"今天就要接进去"的人看的——他需要的是能直接粘的代码和一张错误码表。
 * 每一步都从真实配置里取（base_url、虚拟模型名），所以不会出现"文档写的是旧名字"。
 */

type Models = {
  policies: { match_model: string; candidates: { provider: string; model: string }[] }[];
};

type Tab = "curl" | "python" | "node" | "env";

const ERRORS: { code: string; http: string; means: string; next: string }[] = [
  {
    code: "gateway.missing_credentials",
    http: "401",
    means: "没带 Authorization 头",
    next: "加 `Authorization: Bearer <key>`",
  },
  {
    code: "gateway.invalid_key",
    http: "401",
    means: "key 不存在、被吊销，或复制时少了字符",
    next: "在「Key 管理」核对前缀；需要就重签一把",
  },
  {
    code: "gateway.key_expired",
    http: "401",
    means: "key 过了有效期",
    next: "重签一把并替换部署里的密钥",
  },
  {
    code: "gateway.model_not_allowed",
    http: "403",
    means: "这把 key 的白名单里没有这个虚拟模型",
    next: "换模型，或让管理员改这把 key 的允许模型",
  },
  {
    code: "gateway.project_paused",
    http: "403",
    means: "项目被暂停（迁移/下线/欠费）",
    next: "找项目负责人恢复，不要自己重试",
  },
  {
    code: "gateway.quota_exceeded",
    http: "429",
    means: "RPM 超了，`scope` 字段说明是 key / project / team 哪一级",
    next: "按 `retry_after_s` 退避；长期不够就提额",
  },
  {
    code: "gateway.budget_exhausted",
    http: "429",
    means: "日/月预算打满",
    next: "找 owner 提额或等下一周期；不要重试，重试也是 429",
  },
  {
    code: "gateway.provider_unconfigured",
    http: "502",
    means: "路由指向的供应商没有可用密钥",
    next: "管理员在「供应商」页录入密钥",
  },
  {
    code: "gateway.provider_disabled",
    http: "502",
    means: "该供应商被停用",
    next: "管理员在「供应商」页恢复启用",
  },
  {
    code: "gateway.no_candidate",
    http: "503",
    means: "整条候选链都不可用（含熔断）",
    next: "看「运行状态」页的熔断列表",
  },
  {
    code: "gateway.stream_interrupted",
    http: "200*",
    means: "流式已开始吐 token，之后上游挂了",
    next: "这一条只能标记中断、不能降级；按业务决定是否重试（会重复计费）",
  },
];

export default function GuidePage() {
  const [models, setModels] = useState<string[]>(["chat-default"]);
  const [model, setModel] = useState("chat-default");
  const [tab, setTab] = useState<Tab>("curl");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    adminGet<Models>("/models")
      .then((d) => {
        const names = d.policies.map((p) => p.match_model).filter((n) => n !== "__default__");
        if (names.length) {
          setModels(names);
          setModel(names.includes("chat-default") ? "chat-default" : names[0]);
        }
      })
      .catch(() => undefined);
  }, []);

  const base = typeof window === "undefined" ? "http://aigate.internal/v1" : `${window.location.origin}/v1`;
  const gatewayBase = "http://<网关地址>:8080/v1";

  const snippets: Record<Tab, string> = {
    curl: `curl ${gatewayBase}/chat/completions \\
  -H "Authorization: Bearer $AIGATE_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${model}",
    "messages": [{"role": "user", "content": "你好，介绍一下你自己"}],
    "temperature": 0,
    "stream": false
  }'`,
    python: `from openai import OpenAI

client = OpenAI(
    base_url="${gatewayBase}",
    api_key=os.environ["AIGATE_KEY"],   # 业务方只持有这一把
)

resp = client.chat.completions.create(
    model="${model}",                   # 虚拟模型名，换供应商不用改代码
    messages=[{"role": "user", "content": "你好"}],
    temperature=0,
)
print(resp.choices[0].message.content)`,
    node: `import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "${gatewayBase}",
  apiKey: process.env.AIGATE_KEY,
});

const resp = await client.chat.completions.create({
  model: "${model}",
  messages: [{ role: "user", content: "你好" }],
  temperature: 0,
});
console.log(resp.choices[0].message.content);`,
    env: `# 只需要两个变量：网关地址 + 自己项目的那把 key
AIGATE_BASE_URL=${gatewayBase}
AIGATE_KEY=sk-ag-...        # 在「Key 管理」签发，只在创建时显示一次`,
  };

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>接入指南</h1>
          <div className="sub">
            业务方只改 <span className="mono">base_url</span> 和一个虚拟模型名，其余与 OpenAI 完全一致。
          </div>
        </div>
      </div>

      <div className="panel">
        <h2 style={{ textTransform: "none" }}>三步接进来</h2>
        <div className="sub" style={{ lineHeight: 1.9 }}>
          <div>
            <b>1. 拿一把 key</b>：在「Key 管理」签发（开发/预发/生产分开签，生产必须有过期时间）。
          </div>
          <div>
            <b>2. 选一个虚拟模型名</b>：{" "}
            {models.map((m) => (
              <code key={m} style={{ marginRight: 6 }}>
                {m}
              </code>
            ))}
            ——名字背后的供应商、候选链、降级策略由平台维护。
          </div>
          <div>
            <b>3. 把 base_url 指过来</b>：SDK 不用换，只换地址与模型名；流式、错误码、用量字段都与 OpenAI 一致。
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div style={{ flex: 2 }}>
            <label>虚拟模型</label>
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
          <div style={{ flex: "0 0 auto", minWidth: 0 }}>
            <button className="ghost tiny" onClick={() => copy(snippets[tab])}>
              {copied ? "已复制" : "复制代码"}
            </button>
          </div>
        </div>

        <div style={{ display: "flex", gap: 6, margin: "12px 0 8px" }}>
          {(["curl", "python", "node", "env"] as Tab[]).map((t) => (
            <button
              key={t}
              className={tab === t ? "tiny" : "ghost tiny"}
              onClick={() => setTab(t)}
            >
              {t === "env" ? "环境变量" : t}
            </button>
          ))}
        </div>
        <pre className="prompt-body">{snippets[tab]}</pre>
        <div className="sub" style={{ marginTop: 8 }}>
          页面地址：<span className="mono">{base}</span>（本文档里的示例统一写成
          <span className="mono"> {gatewayBase}</span>，按你们实际的内网地址替换）。
        </div>
      </div>

      <div className="panel">
        <h2>错误码</h2>
        <div className="sub" style={{ marginBottom: 10 }}>
          网关所有错误都是 <span className="mono">{`{"error": {"code", "message"}}`}</span>。
          <b>按 code 编程，不要按 message</b>——message 是给人看的，随时会改。
        </div>
        <table>
          <thead>
            <tr>
              <th>code</th>
              <th>HTTP</th>
              <th>含义</th>
              <th>下一步</th>
            </tr>
          </thead>
          <tbody>
            {ERRORS.map((e) => (
              <tr key={e.code}>
                <td className="mono">{e.code}</td>
                <td className="mono">{e.http}</td>
                <td>{e.means}</td>
                <td className="sub">{e.next}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>响应里的 x_aigate</h2>
        <div className="sub" style={{ lineHeight: 1.9 }}>
          <div>
            兼容 OpenAI 的同时，每个响应多一个 <span className="mono">x_aigate</span> 字段：
            request_id、provider、model_used、cost_cny、latency_ms、cache_hit、degraded、pii_masked。
          </div>
          <div>
            <b>degraded=true</b> 表示主供应商失败、这次由备用供应商回答——业务上通常要记一笔，
            否则"为什么这周答案风格变了"会变成玄学。
          </div>
          <div>
            <b>request_id</b> 拿去「调用明细」或直接拼 <span className="mono">/traces/&lt;id&gt;</span>{" "}
            就能看到整条调用链（含 PII 脱敏后的 prompt、每跳耗时、影子流量对照）。
          </div>
        </div>
      </div>
    </>
  );
}