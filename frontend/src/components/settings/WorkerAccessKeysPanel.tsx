"use client";

import { useEffect, useState } from "react";
import { Copy, KeyRound, Loader2, RefreshCw, Shield, Trash2 } from "lucide-react";
import {
  createWorkerAccessKey,
  listWorkerAccessKeys,
  revokeWorkerAccessKey,
  rotateWorkerAccessKey,
  type WorkerAccessKey,
  type WorkerAccessKeySecret,
} from "@/lib/workerAccessApi";

export default function WorkerAccessKeysPanel() {
  const [keys, setKeys] = useState<WorkerAccessKey[]>([]);
  const [name, setName] = useState("");
  const [profile, setProfile] = useState("smart");
  const [secret, setSecret] = useState<WorkerAccessKeySecret | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const refresh = async () => {
    setBusy(true); setMessage("");
    try { setKeys((await listWorkerAccessKeys()).keys); }
    catch (error) { setMessage(error instanceof Error ? error.message : "加载 Worker Key 失败"); }
    finally { setBusy(false); }
  };

  useEffect(() => { void refresh(); }, []);

  const create = async () => {
    setBusy(true); setMessage("");
    try {
      const created = await createWorkerAccessKey({
        name: name.trim(), authority_profile: profile,
        scopes: ["worker:health", "worker:models:read", "worker:runs:create", "worker:runs:read", "worker:runs:cancel"],
      });
      setSecret(created); await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "创建 Worker Key 失败"); }
    finally { setBusy(false); }
  };

  const rotate = async (keyId: string) => {
    setBusy(true); setMessage("");
    try { setSecret(await rotateWorkerAccessKey(keyId)); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "轮换 Worker Key 失败"); }
    finally { setBusy(false); }
  };

  const revoke = async (keyId: string) => {
    setBusy(true); setMessage("");
    try { await revokeWorkerAccessKey(keyId); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "吊销 Worker Key 失败"); }
    finally { setBusy(false); }
  };

  const copySecret = async () => { if (secret?.token) await navigator.clipboard.writeText(secret.token); };

  return <SettingsCard title="Worker 接入" icon={KeyRound} color="#002fa7">
    <div className="space-y-5">
      <p className="text-xs leading-5 text-gray-500">本机通过回环地址管理 Worker Access Key，无需额外管理员 Token。Worker Key 供 PuddingTeams CLI 调用 Headless API；后端只保存哈希，明文只在创建或轮换成功后显示一次。远程部署可通过 PUDDINGCLAW_ADMIN_TOKEN 保护管理接口。</p>
      <div className="grid gap-3 md:grid-cols-[1fr_180px_auto]">
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Key 名称，例如 codex / puddingteams" className="form-input" />
        <select value={profile} onChange={(event) => setProfile(event.target.value)} className="form-input"><option value="smart">SMART</option><option value="workspace">FULL_ACCESS · workspace</option><option value="workspace_network">FULL_ACCESS · workspace + network</option><option value="workspace_package_install">FULL_ACCESS · workspace + package</option></select>
        <button type="button" onClick={create} disabled={busy} className="inline-flex items-center justify-center gap-2 rounded-xl bg-[#002fa7] px-4 py-2 text-xs font-medium text-white hover:bg-[#001f7a] disabled:opacity-50">{busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}创建 Key</button>
      </div>
      {secret && <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-xs text-amber-950"><div className="mb-2 flex items-center gap-2 font-semibold"><Shield className="h-4 w-4" />请立即复制，关闭此页面后不再显示</div><div className="flex items-center gap-2"><code className="min-w-0 flex-1 break-all rounded-lg bg-white px-3 py-2">{secret.token}</code><button type="button" onClick={copySecret} className="rounded-lg border border-amber-300 bg-white p-2" title="复制"><Copy className="h-3.5 w-3.5" /></button></div></div>}
      {message && <p className="text-xs text-rose-600">{message}</p>}
      <div className="divide-y rounded-xl border border-gray-200">{keys.length === 0 ? <p className="p-4 text-xs text-gray-400">暂无 Worker Key</p> : keys.map((item) => <div key={item.key_id} className="flex flex-wrap items-center gap-3 p-4"><div className="min-w-0 flex-1"><div className="text-sm font-medium text-gray-800">{item.name}</div><div className="mt-1 text-[11px] text-gray-500"><code>{item.prefix}…</code> · {item.authority_profile} · {item.revoked_at ? "已吊销" : "有效"}</div></div><button type="button" onClick={() => rotate(item.key_id)} disabled={busy || Boolean(item.revoked_at)} className="inline-flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-[11px] text-gray-600 disabled:opacity-40"><RefreshCw className="h-3 w-3" />轮换</button><button type="button" onClick={() => revoke(item.key_id)} disabled={busy || Boolean(item.revoked_at)} className="inline-flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-[11px] text-rose-600 disabled:opacity-40"><Trash2 className="h-3 w-3" />吊销</button></div>)}</div>
    </div>
  </SettingsCard>;
}

function SettingsCard({ title, icon: Icon, color, children }: { title: string; icon: React.ElementType; color: string; children: React.ReactNode }) {
  return <section className="rounded-2xl border border-gray-200 bg-white p-6 shadow-sm"><div className="mb-5 flex items-center gap-2 text-[15px] font-semibold text-gray-800"><Icon className="h-4 w-4" style={{ color }} />{title}</div>{children}</section>;
}
