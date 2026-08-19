"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  Eye,
  EyeOff,
  FolderTree,
  KeyRound,
  Loader2,
  ShieldCheck,
  UserRound,
  UsersRound,
  X,
} from "lucide-react";

import {
  bindFeishuTenantAuth,
  configureFeishuScope,
  createFeishuApp,
  createKnowledgeSource,
  listFeishuNodes,
  listFeishuSpaces,
  rotateFeishuApp,
  startFeishuUserOAuth,
  startKnowledgeSourceSync,
  testFeishuApp,
  updateKnowledgeSource,
  type FeishuSpace,
  type FeishuWikiNode,
  type KnowledgeSource,
} from "@/lib/knowledgeSourcesApi";

type Step = "credential" | "authorization" | "scope";

type Props = {
  open: boolean;
  existingSource?: KnowledgeSource | null;
  onClose: () => void;
  onConnected: (source: KnowledgeSource) => void | Promise<void>;
};

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error || "未知错误");
}

function NodeRow({
  node,
  depth,
  selectedToken,
  childrenByParent,
  expanded,
  loadingToken,
  onToggle,
  onSelect,
}: {
  node: FeishuWikiNode;
  depth: number;
  selectedToken: string;
  childrenByParent: Record<string, FeishuWikiNode[]>;
  expanded: Set<string>;
  loadingToken: string;
  onToggle: (node: FeishuWikiNode) => void;
  onSelect: (node: FeishuWikiNode) => void;
}) {
  const token = node.node_token;
  const isExpanded = expanded.has(token);
  const children = childrenByParent[token] || [];
  return (
    <div>
      <div
        className={`flex min-h-11 items-center gap-2 rounded-xl px-2 transition ${
          selectedToken === token ? "bg-[#002fa7]/[0.07] text-[#002fa7]" : "hover:bg-black/[0.025]"
        }`}
        style={{ paddingLeft: `${8 + depth * 18}px` }}
      >
        {node.has_child ? (
          <button
            type="button"
            className="grid h-7 w-7 shrink-0 place-items-center rounded-lg text-gray-400 hover:bg-white"
            onClick={() => onToggle(node)}
            aria-label={isExpanded ? "折叠" : "展开"}
          >
            {loadingToken === token ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : isExpanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          </button>
        ) : <span className="h-7 w-7 shrink-0" />}
        <button type="button" className="flex min-w-0 flex-1 items-center gap-3 text-left" onClick={() => onSelect(node)}>
          <span className={`grid h-5 w-5 shrink-0 place-items-center rounded-full border ${selectedToken === token ? "border-[#002fa7] bg-[#002fa7] text-white" : "border-gray-300 bg-white"}`}>
            {selectedToken === token ? <Check className="h-3 w-3" /> : null}
          </span>
          <span className="truncate text-sm font-medium">{node.title || "未命名节点"}</span>
          <span className="ml-auto shrink-0 text-[10px] uppercase tracking-wide text-gray-400">{node.obj_type || "wiki"}</span>
        </button>
      </div>
      {isExpanded ? children.map((child) => (
        <NodeRow
          key={child.node_token}
          node={child}
          depth={depth + 1}
          selectedToken={selectedToken}
          childrenByParent={childrenByParent}
          expanded={expanded}
          loadingToken={loadingToken}
          onToggle={onToggle}
          onSelect={onSelect}
        />
      )) : null}
    </div>
  );
}

export default function FeishuConnectionWizard({ open, existingSource = null, onClose, onConnected }: Props) {
  const [step, setStep] = useState<Step>("credential");
  const [appId, setAppId] = useState("");
  const [appSecret, setAppSecret] = useState("");
  const [appName, setAppName] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [authType, setAuthType] = useState<"tenant" | "user">("tenant");
  const [sourceName, setSourceName] = useState("飞书知识库");
  const [appCredentialId, setAppCredentialId] = useState("");
  const [source, setSource] = useState<KnowledgeSource | null>(null);
  const [spaces, setSpaces] = useState<FeishuSpace[]>([]);
  const [spaceId, setSpaceId] = useState("");
  const [nodes, setNodes] = useState<FeishuWikiNode[]>([]);
  const [childrenByParent, setChildrenByParent] = useState<Record<string, FeishuWikiNode[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [rootNodeToken, setRootNodeToken] = useState("");
  const [rootNodeTitle, setRootNodeTitle] = useState("");
  const [schedule, setSchedule] = useState("60");
  const [publishVector, setPublishVector] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loadingToken, setLoadingToken] = useState("");
  const [error, setError] = useState("");
  const [nameSaved, setNameSaved] = useState(false);
  const [redirectUri, setRedirectUri] = useState("/knowledge/feishu/oauth/callback");
  const oauthCompletedRef = useRef(false);
  const prevSpaceIdRef = useRef("");

  const reset = useCallback(() => {
    const existingAppId = typeof existingSource?.config.app_credential_id === "string" ? existingSource.config.app_credential_id : "";
    const existingSpaceId = typeof existingSource?.config.space_id === "string" ? existingSource.config.space_id : "";
    const existingRootToken = typeof existingSource?.config.root_node_token === "string" ? existingSource.config.root_node_token : "";
    const existingInterval = Number(existingSource?.schedule?.interval_minutes || 0);
    setStep(existingAppId ? "authorization" : "credential");
    setAppId("");
    setAppSecret("");
    setAppName("");
    setShowSecret(false);
    setAuthType(existingSource?.auth_type === "user" ? "user" : "tenant");
    setSourceName(existingSource?.name || "飞书知识库");
    setAppCredentialId(existingAppId);
    setSource(existingSource);
    setSpaces([]);
    setSpaceId(existingSpaceId);
    setNodes([]);
    setChildrenByParent({});
    setExpanded(new Set());
    setRootNodeToken(existingRootToken);
    setRootNodeTitle("");
    setSchedule(existingInterval > 0 ? String(existingInterval) : existingSource ? "manual" : "60");
    setPublishVector(existingSource ? existingSource.config.publish_vector !== false : true);
    prevSpaceIdRef.current = existingSpaceId;
    setError("");
    setNameSaved(false);
  }, [existingSource]);

  useEffect(() => {
    if (open) reset();
    else setAppSecret("");
  }, [open, reset]);

  useEffect(() => {
    setRedirectUri(`${window.location.origin}/knowledge/feishu/oauth/callback`);
  }, []);

  useEffect(() => {
    if (!open || !source || authType !== "user") return;
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data as { type?: string; ok?: boolean; error?: string };
      if (data?.type !== "pudding-feishu-oauth-complete") return;
      if (!data.ok) {
        setError(data.error || "飞书用户授权失败。请重试。 ");
        setBusy(false);
        return;
      }
      oauthCompletedRef.current = true;
      void loadSpaces(source.id);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [authType, open, source]);

  const stepIndex = step === "credential" ? 0 : step === "authorization" ? 1 : 2;
  const selectedSpace = useMemo(() => spaces.find((item) => item.space_id === spaceId), [spaceId, spaces]);

  async function saveCredential() {
    if (!appId.trim() || appSecret.length < 8) return;
    setBusy(true);
    setError("");
    try {
      const app = existingSource && appCredentialId
        ? await rotateFeishuApp(appCredentialId, { app_id: appId.trim(), app_secret: appSecret })
        : await createFeishuApp({ app_id: appId.trim(), app_secret: appSecret, app_name: appName.trim() });
      const validated = await testFeishuApp(app.id);
      setAppCredentialId(validated.id);
      setAppSecret("");
      setStep("authorization");
    } catch (nextError) {
      setError(messageOf(nextError));
    } finally {
      setBusy(false);
    }
  }

  async function loadSpaces(sourceId: string) {
    setBusy(true);
    setError("");
    try {
      const result = await listFeishuSpaces(sourceId);
      setSpaces(result);
      setStep("scope");
      if (result.length === 1) setSpaceId(result[0].space_id);
    } catch (nextError) {
      setError(messageOf(nextError));
    } finally {
      setBusy(false);
    }
  }

  async function saveName() {
    const name = sourceName.trim();
    if (!source || !name || name === source.name) return;
    setBusy(true);
    setError("");
    try {
      const updated = await updateKnowledgeSource(source.id, { name });
      setSource(updated);
      setNameSaved(true);
      await onConnected(updated);
    } catch (nextError) {
      setError(messageOf(nextError));
    } finally {
      setBusy(false);
    }
  }

  async function authorize() {
    if (!appCredentialId) return;
    setBusy(true);
    setError("");
    try {
      let created = source || await createKnowledgeSource({
        connector_key: "feishu_wiki",
        name: sourceName.trim() || "飞书知识库",
        auth_type: authType,
        schedule: { interval_minutes: 0 },
      });
      if (source && sourceName.trim() && sourceName.trim() !== created.name) {
        // Non-auth metadata is saved independently of re-validation.
        created = await updateKnowledgeSource(created.id, { name: sourceName.trim() });
        setNameSaved(true);
      }
      setSource(created);
      if (authType === "tenant") {
        const bound = await bindFeishuTenantAuth(created.id, appCredentialId);
        setSource(bound);
        await loadSpaces(created.id);
        return;
      }
      const oauth = await startFeishuUserOAuth(created.id, appCredentialId, redirectUri);
      const popup = window.open(oauth.authorization_url, "pudding-feishu-oauth", "popup,width=720,height=760");
      if (!popup) throw new Error("浏览器阻止了授权窗口，请允许弹窗后重试。");
      oauthCompletedRef.current = false;
      popup.focus();
      const closeWatcher = window.setInterval(() => {
        if (!popup.closed) return;
        window.clearInterval(closeWatcher);
        if (!oauthCompletedRef.current) {
          setBusy(false);
          setError("授权窗口已关闭，尚未完成飞书用户授权。 ");
        }
      }, 500);
    } catch (nextError) {
      setBusy(false);
      setError(messageOf(nextError));
    }
  }

  useEffect(() => {
    if (!source || !spaceId) {
      setNodes([]);
      return;
    }
    if (prevSpaceIdRef.current !== spaceId) {
      // Space changed by the user: the saved root-node selection no longer
      // applies. Initial mount (incl. edit-mode prefill) keeps it.
      prevSpaceIdRef.current = spaceId;
      setRootNodeToken("");
      setRootNodeTitle("");
      setChildrenByParent({});
      setExpanded(new Set());
    }
    setBusy(true);
    setError("");
    void listFeishuNodes(source.id, spaceId)
      .then(setNodes)
      .catch((nextError) => setError(messageOf(nextError)))
      .finally(() => setBusy(false));
  }, [source, spaceId]);

  async function toggleNode(node: FeishuWikiNode) {
    const token = node.node_token;
    if (expanded.has(token)) {
      setExpanded((current) => { const next = new Set(current); next.delete(token); return next; });
      return;
    }
    setExpanded((current) => new Set(current).add(token));
    if (!source || childrenByParent[token]) return;
    setLoadingToken(token);
    try {
      const children = await listFeishuNodes(source.id, spaceId, token);
      setChildrenByParent((current) => ({ ...current, [token]: children }));
    } catch (nextError) {
      setError(messageOf(nextError));
    } finally {
      setLoadingToken("");
    }
  }

  async function finish() {
    if (!source || !spaceId) return;
    setBusy(true);
    setError("");
    try {
      const configured = await configureFeishuScope(source.id, {
        space_id: spaceId,
        root_node_token: rootNodeToken,
        publish_vector: publishVector,
        interval_minutes: schedule === "manual" ? 0 : Number(schedule),
      });
      await startKnowledgeSourceSync(source.id, "incremental");
      await onConnected({ ...configured, status: "syncing" });
      onClose();
    } catch (nextError) {
      setError(messageOf(nextError));
    } finally {
      setBusy(false);
    }
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[120] grid place-items-center bg-slate-950/30 p-4 backdrop-blur-[2px]" role="dialog" aria-modal="true" aria-label="连接飞书知识库">
      <div className="flex max-h-[92vh] w-full max-w-4xl flex-col overflow-hidden rounded-[28px] border border-white/70 bg-white shadow-2xl shadow-slate-950/15">
        <div className="flex items-start justify-between border-b border-black/[0.06] px-6 py-5 sm:px-8">
          <div>
            <h2 className="text-xl font-semibold tracking-tight text-gray-950">{existingSource ? "编辑飞书连接" : "连接飞书知识库"}</h2>
            <div className="mt-4 flex items-center gap-2">
              {["应用凭据", "授权身份", "同步范围"].map((label, index) => (
                <div key={label} className="flex items-center gap-2">
                  <span className={`grid h-7 w-7 place-items-center rounded-full text-xs font-bold ${index <= stepIndex ? "bg-[#002fa7] text-white" : "border border-gray-200 text-gray-400"}`}>
                    {index < stepIndex ? <Check className="h-3.5 w-3.5" /> : index + 1}
                  </span>
                  <span className={`hidden text-xs font-semibold sm:inline ${index <= stepIndex ? "text-[#002fa7]" : "text-gray-400"}`}>{label}</span>
                  {index < 2 ? <span className="h-px w-8 bg-gray-200 sm:w-14" /> : null}
                </div>
              ))}
            </div>
          </div>
          <button type="button" onClick={onClose} className="grid h-10 w-10 place-items-center rounded-xl bg-gray-50 text-gray-500 hover:bg-gray-100" aria-label="关闭">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6 sm:px-8">
          {step === "credential" ? (
            <div className="mx-auto max-w-xl space-y-5">
              <div className="rounded-2xl border border-[#002fa7]/10 bg-[#002fa7]/[0.035] p-4 text-sm leading-6 text-gray-600">
                <div className="flex items-center gap-2 font-semibold text-gray-900"><ShieldCheck className="h-4 w-4 text-[#002fa7]" />凭据只进入后端加密凭据库</div>
                <p className="mt-1">前端不会保存或回显 App Secret；tenant_access_token 与 user_access_token 也不会返回浏览器。</p>
              </div>
              <label className="block"><span className="text-xs font-semibold text-gray-700">应用名称（可选）</span><input value={appName} onChange={(event) => setAppName(event.target.value)} placeholder="例如：PuddingKnowledge 连接器" className="mt-2 h-11 w-full rounded-xl border border-black/10 px-3.5 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/10" /></label>
              <label className="block"><span className="text-xs font-semibold text-gray-700">App ID</span><input value={appId} onChange={(event) => setAppId(event.target.value)} autoComplete="off" placeholder="cli_xxxxxxxxxxxxxxxx" className="mt-2 h-11 w-full rounded-xl border border-black/10 px-3.5 font-mono text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/10" /></label>
              <label className="block"><span className="text-xs font-semibold text-gray-700">App Secret</span><span className="relative mt-2 block"><input value={appSecret} onChange={(event) => setAppSecret(event.target.value)} type={showSecret ? "text" : "password"} autoComplete="new-password" placeholder="仅本次提交使用" className="h-11 w-full rounded-xl border border-black/10 px-3.5 pr-11 font-mono text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/10" /><button type="button" onClick={() => setShowSecret((value) => !value)} className="absolute right-1.5 top-1.5 grid h-8 w-8 place-items-center rounded-lg text-gray-400 hover:bg-gray-50">{showSecret ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}</button></span></label>
              <a href="https://open.feishu.cn/app" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 text-xs font-semibold text-[#002fa7] hover:underline">前往飞书开放平台配置应用 <ExternalLink className="h-3.5 w-3.5" /></a>
            </div>
          ) : null}

          {step === "authorization" ? (
            <div className="mx-auto max-w-2xl space-y-6">
              <div>
                <label className="block text-xs font-semibold text-gray-700">来源名称</label>
                <div className="mt-2 flex items-center gap-2">
                  <input value={sourceName} onChange={(event) => { setSourceName(event.target.value); setNameSaved(false); }} className="h-11 min-w-0 flex-1 rounded-xl border border-black/10 px-3.5 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/10" />
                  {existingSource && source ? (
                    nameSaved && sourceName.trim() === source.name ? (
                      <span className="inline-flex shrink-0 items-center gap-1 text-xs font-semibold text-emerald-600"><Check className="h-3.5 w-3.5" />已保存</span>
                    ) : sourceName.trim() && sourceName.trim() !== source.name ? (
                      <button type="button" disabled={busy} onClick={() => void saveName()} className="inline-flex h-11 shrink-0 items-center gap-2 rounded-xl border border-[#002fa7]/25 px-4 text-xs font-semibold text-[#002fa7] hover:bg-[#002fa7]/[0.04] disabled:opacity-40">保存名称</button>
                    ) : null
                  ) : null}
                </div>
                {existingSource ? <p className="mt-1.5 text-[11px] text-gray-400">名称等非授权信息可直接保存，无需重新验证身份。</p> : null}
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <button type="button" onClick={() => setAuthType("tenant")} className={`rounded-2xl border p-5 text-left transition ${authType === "tenant" ? "border-[#002fa7]/30 bg-[#002fa7]/[0.045] ring-2 ring-[#002fa7]/10" : "border-black/[0.07] hover:border-[#002fa7]/20"}`}>
                  <div className="flex items-center justify-between"><UsersRound className="h-5 w-5 text-[#002fa7]" />{authType === "tenant" ? <Check className="h-4 w-4 text-[#002fa7]" /> : null}</div>
                  <div className="mt-4 text-sm font-semibold">应用身份</div><p className="mt-1 text-xs leading-5 text-gray-500">使用 tenant_access_token。适合组织统一同步，能读取应用已获授权的内容。</p>
                </button>
                <button type="button" onClick={() => setAuthType("user")} className={`rounded-2xl border p-5 text-left transition ${authType === "user" ? "border-[#002fa7]/30 bg-[#002fa7]/[0.045] ring-2 ring-[#002fa7]/10" : "border-black/[0.07] hover:border-[#002fa7]/20"}`}>
                  <div className="flex items-center justify-between"><UserRound className="h-5 w-5 text-[#002fa7]" />{authType === "user" ? <Check className="h-4 w-4 text-[#002fa7]" /> : null}</div>
                  <div className="mt-4 text-sm font-semibold">用户身份</div><p className="mt-1 text-xs leading-5 text-gray-500">通过 OAuth 获取 user_access_token。同步范围遵循该用户在飞书中的可见权限。</p>
                </button>
              </div>
              {existingSource ? <button type="button" onClick={() => setStep("credential")} className="text-xs font-semibold text-[#002fa7] hover:underline">当前应用凭据不可用？配置新的 App ID / App Secret</button> : null}
              <div className="rounded-2xl border border-black/[0.06] bg-gray-50 p-4 text-xs leading-5 text-gray-500">
                <div className="flex items-center gap-2 font-semibold text-gray-800"><KeyRound className="h-4 w-4" />Token 生命周期由后端托管</div>
                <p className="mt-1">tenant token 自动缓存和刷新；用户授权包含 offline_access，refresh token 按飞书规则轮换。</p>
                {authType === "user" ? <div className="mt-3 border-t border-black/[0.06] pt-3"><div className="font-semibold text-gray-700">请在飞书应用后台登记重定向 URL</div><code className="mt-1 block break-all rounded-lg bg-white px-2.5 py-2 text-[11px] text-[#002fa7]">{redirectUri}</code></div> : null}
              </div>
            </div>
          ) : null}

          {step === "scope" ? (
            <div className="grid gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
              <div>
                <div className="text-xs font-semibold text-gray-700">选择知识空间</div>
                <div className="mt-2 space-y-1 rounded-2xl border border-black/[0.07] p-2">
                  {spaces.length ? spaces.map((space) => (
                    <button type="button" key={space.space_id} onClick={() => setSpaceId(space.space_id)} className={`flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left text-sm ${spaceId === space.space_id ? "bg-[#002fa7] text-white" : "hover:bg-gray-50"}`}>
                      <FolderTree className="h-4 w-4 shrink-0" /><span className="min-w-0 flex-1 truncate font-medium">{space.name || space.space_id}</span>
                    </button>
                  )) : <div className="px-3 py-8 text-center text-xs text-gray-400">当前身份看不到可同步的 Wiki 空间。</div>}
                </div>
                <label className="mt-4 block text-xs font-semibold text-gray-700">自动同步</label>
                <select value={schedule} onChange={(event) => { setSchedule(event.target.value); event.currentTarget.blur(); }} className="mt-2 h-10 w-full rounded-xl border border-black/10 bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40">
                  <option value="manual">仅手动</option><option value="15">每 15 分钟</option><option value="60">每小时</option><option value="360">每 6 小时</option><option value="1440">每天</option>
                </select>
              </div>
              <div className="min-w-0">
                <div className="flex items-end justify-between gap-3"><div><div className="text-xs font-semibold text-gray-700">选择同步根节点</div><p className="mt-1 text-xs text-gray-400">不选节点时同步整个空间；选择后递归同步其下文档和子节点。</p></div>{rootNodeToken ? <button type="button" onClick={() => { setRootNodeToken(""); setRootNodeTitle(""); }} className="shrink-0 text-xs font-semibold text-[#002fa7]">同步整个空间</button> : null}</div>
                <div className="mt-3 max-h-[340px] min-h-[240px] overflow-y-auto rounded-2xl border border-black/[0.07] p-2">
                  {!spaceId ? <div className="grid min-h-[220px] place-items-center text-xs text-gray-400">先选择一个知识空间</div> : busy && !nodes.length ? <div className="grid min-h-[220px] place-items-center"><Loader2 className="h-5 w-5 animate-spin text-[#002fa7]" /></div> : nodes.length ? nodes.map((node) => (
                    <NodeRow key={node.node_token} node={node} depth={0} selectedToken={rootNodeToken} childrenByParent={childrenByParent} expanded={expanded} loadingToken={loadingToken} onToggle={toggleNode} onSelect={(next) => { setRootNodeToken(next.node_token); setRootNodeTitle(next.title); }} />
                  )) : <div className="grid min-h-[220px] place-items-center text-xs text-gray-400">这个空间没有可读取的 Wiki 节点。</div>}
                </div>
                <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-xl bg-gray-50 px-4 py-3 text-xs"><span className="text-gray-500">同步范围：<strong className="text-gray-800">{selectedSpace?.name || (spaceId ? "已保存空间" : "未选择")}{rootNodeToken ? ` / ${rootNodeTitle || "指定根节点"}` : spaceId ? " / 整个空间" : ""}</strong></span><label className="flex items-center gap-2 font-medium text-gray-700"><input type="checkbox" checked={publishVector} onChange={(event) => setPublishVector(event.target.checked)} className="accent-[#002fa7]" />同步后写入向量索引</label></div>
              </div>
            </div>
          ) : null}

          {error ? <div className="mx-auto mt-5 max-w-2xl rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div> : null}
        </div>

        <div className="flex items-center justify-between border-t border-black/[0.06] px-6 py-4 sm:px-8">
          <button type="button" onClick={step === "credential" || step === "scope" ? onClose : () => setStep("credential")} className="h-10 rounded-xl border border-black/10 px-4 text-sm font-semibold text-gray-600 hover:bg-gray-50">{step === "credential" ? "取消" : step === "scope" ? "稍后设置" : "上一步"}</button>
          {step === "credential" ? <button type="button" disabled={busy || !appId.trim() || appSecret.length < 8} onClick={() => void saveCredential()} className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-5 text-sm font-semibold text-white disabled:opacity-40">{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}验证并继续</button> : null}
          {step === "authorization" ? <button type="button" disabled={busy} onClick={() => void authorize()} className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-5 text-sm font-semibold text-white disabled:opacity-40">{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : authType === "user" ? <ExternalLink className="h-4 w-4" /> : null}{authType === "tenant" ? "验证应用身份" : "打开飞书授权"}</button> : null}
          {step === "scope" ? <button type="button" disabled={busy || !spaceId} onClick={() => void finish()} className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-5 text-sm font-semibold text-white disabled:opacity-40">{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}保存并开始同步</button> : null}
        </div>
      </div>
    </div>
  );
}
