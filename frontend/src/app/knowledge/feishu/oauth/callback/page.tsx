"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";

import { completeFeishuUserOAuth } from "@/lib/knowledgeSourcesApi";

export default function FeishuOAuthCallbackPage() {
  const [status, setStatus] = useState<"working" | "success" | "error">("working");
  const [message, setMessage] = useState("正在完成飞书授权…");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code") || "";
    const state = params.get("state") || "";
    const remoteError = params.get("error") || params.get("error_description") || "";
    if (remoteError || !code || !state) {
      const text = remoteError || "回调缺少 code 或 state。";
      setStatus("error");
      setMessage(text);
      window.opener?.postMessage({ type: "pudding-feishu-oauth-complete", ok: false, error: text }, window.location.origin);
      return;
    }
    void completeFeishuUserOAuth(state, code)
      .then(() => {
        setStatus("success");
        setMessage("授权完成，可以返回 PuddingKnowledge。 ");
        window.opener?.postMessage({ type: "pudding-feishu-oauth-complete", ok: true }, window.location.origin);
        window.setTimeout(() => window.close(), 900);
      })
      .catch((error: unknown) => {
        const text = error instanceof Error ? error.message : "飞书授权失败。";
        setStatus("error");
        setMessage(text);
        window.opener?.postMessage({ type: "pudding-feishu-oauth-complete", ok: false, error: text }, window.location.origin);
      });
  }, []);

  return (
    <main className="grid min-h-screen place-items-center bg-[#f1f3f6] p-6">
      <div className="w-full max-w-sm rounded-3xl border border-black/[0.06] bg-white p-8 text-center shadow-xl shadow-slate-950/5">
        <div className="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-[#002fa7]/[0.06] text-[#002fa7]">
          {status === "working" ? <Loader2 className="h-7 w-7 animate-spin" /> : status === "success" ? <CheckCircle2 className="h-7 w-7" /> : <XCircle className="h-7 w-7 text-red-600" />}
        </div>
        <h1 className="mt-5 text-lg font-semibold text-gray-950">飞书用户授权</h1>
        <p className="mt-2 text-sm leading-6 text-gray-500">{message}</p>
        {status !== "working" ? <button type="button" onClick={() => window.close()} className="mt-6 h-10 rounded-xl bg-[#002fa7] px-5 text-sm font-semibold text-white">关闭窗口</button> : null}
      </div>
    </main>
  );
}
