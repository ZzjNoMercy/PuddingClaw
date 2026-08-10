"use client";

import Image from "next/image";
import moonshotLogo from "@lobehub/icons-static-svg/icons/moonshot.svg";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  Bot,
  CheckCircle2,
  ChevronRight,
  Clock3,
  ExternalLink,
  KeyRound,
  Loader2,
  Plug,
  RotateCcw,
  Server,
  Unplug,
  UserRound,
  X,
} from "lucide-react";
import {
  authorizeConnector,
  getConnector,
  installKimiWebBridge,
  listConnectors,
  probeKimiWebBridge,
  revokeConnector,
  setKimiWebBridgeEnabled,
  type ConnectorIdentityStatus,
  type ConnectorInfo,
  type ConnectorStatus,
} from "@/lib/connectorsApi";

const STATUS_COPY: Record<ConnectorStatus, { label: string; dot: string; badge: string }> = {
  connected: { label: "已连接", dot: "bg-emerald-500", badge: "bg-emerald-50 text-emerald-700" },
  authorizing: { label: "授权中", dot: "bg-blue-500", badge: "bg-blue-50 text-blue-700" },
  authorization_required: { label: "需要授权", dot: "bg-amber-500", badge: "bg-amber-50 text-amber-700" },
  repair_required: { label: "需要修复", dot: "bg-red-500", badge: "bg-red-50 text-red-700" },
  revoked: { label: "已断开", dot: "bg-gray-400", badge: "bg-gray-100 text-gray-600" },
  unconfigured: { label: "未连接", dot: "bg-gray-300", badge: "bg-gray-100 text-gray-600" },
  environment_unavailable: { label: "环境不可用", dot: "bg-red-500", badge: "bg-red-50 text-red-700" },
};

const WEBBRIDGE_UPDATE_URL = "https://www.kimi.com/zh-cn/features/webbridge";

function identityLabel(identity: ConnectorIdentityStatus | undefined, kind: "bot" | "user") {
  const status = identity?.status || "unconfigured";
  if (["ready", "active"].includes(status)) return { text: kind === "bot" ? "已就绪" : "有效", ok: true };
  if (status === "authorization_required") return { text: "需要授权", ok: false };
  if (status === "revoked") return { text: "已撤销", ok: false };
  if (status === "repair_required") return { text: "需要修复", ok: false };
  return { text: "未配置", ok: false };
}

function formatRelativeTime(value?: number) {
  if (!value) return "尚未验证";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - value));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return new Date(value * 1000).toLocaleDateString("zh-CN");
}

function ConnectorMark({ connectorId, large = false }: { connectorId: string; large?: boolean }) {
  if (connectorId === "kimi-webbridge") {
    const logoSrc = typeof moonshotLogo === "string" ? moonshotLogo : moonshotLogo.src;
    return (
      <div className={`${large ? "h-16 w-16 rounded-2xl p-2.5" : "h-11 w-11 rounded-xl p-2"} flex shrink-0 items-center justify-center bg-white shadow-sm ring-1 ring-black/[0.06]`}>
        <img src={logoSrc} alt="" className="h-full w-full object-contain" />
      </div>
    );
  }
  if (connectorId !== "lark") {
    return (
      <div className={`${large ? "h-16 w-16 rounded-2xl" : "h-11 w-11 rounded-xl"} flex shrink-0 items-center justify-center bg-indigo-50 text-[#002fa7] shadow-sm ring-1 ring-black/[0.06]`}>
        <Plug className={large ? "h-8 w-8" : "h-5 w-5"} />
      </div>
    );
  }
  return (
    <div className={`${large ? "h-16 w-16 rounded-2xl p-2.5" : "h-11 w-11 rounded-xl p-1.5"} flex shrink-0 items-center justify-center bg-white shadow-sm ring-1 ring-black/[0.06]`}>
      <Image
        src="/brands/feishu-logo.svg"
        alt=""
        width={large ? 64 : 44}
        height={large ? 64 : 44}
        className="h-full w-full object-contain"
        priority={large}
      />
    </div>
  );
}

export default function ConnectorCatalog({
  onUse,
}: {
  onUse: (prompt: string) => void;
}) {
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      setConnectors(await listConnectors());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "连接器加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const refresh = () => { if (document.visibilityState === "visible") void load(); };
    const timer = window.setInterval(refresh, 5000);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [load]);
  const selected = connectors.find((connector) => connector.connector_id === selectedId) || null;
  const closeConnector = useCallback(() => setSelectedId(null), []);
  const refreshSelected = useCallback(async () => {
    if (!selectedId) return;
    const refreshed = await getConnector(selectedId);
    setConnectors((current) => current.map((item) => item.connector_id === refreshed.connector_id ? refreshed : item));
  }, [selectedId]);

  return (
    <div className="flex-1 overflow-y-auto bg-white/30">
      <div className="w-full px-5 pb-8 pt-3">
        {loading ? (
          <div className="flex justify-center py-20"><Loader2 className="h-5 w-5 animate-spin text-gray-400" /></div>
        ) : error ? (
          <div className="rounded-2xl border border-red-100 bg-red-50 px-5 py-8 text-center text-sm text-red-700">
            {error}
            <button type="button" onClick={() => void load()} className="ml-3 font-medium underline">重试</button>
          </div>
        ) : connectors.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-black/10 px-5 py-14 text-center text-sm text-gray-400">暂无可用连接器</div>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {connectors.map((connector) => {
              const status = STATUS_COPY[connector.status];
              return (
                <button
                  key={connector.connector_id}
                  type="button"
                  onClick={() => setSelectedId(connector.connector_id)}
                  className="group flex min-h-28 items-start gap-3.5 rounded-2xl border border-black/[0.06] bg-white p-4 text-left shadow-sm transition-all hover:-translate-y-0.5 hover:border-[#002fa7]/20 hover:shadow-md"
                >
                  <ConnectorMark connectorId={connector.connector_id} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h2 className="text-[16px] font-semibold text-gray-900">{connector.display_name}</h2>
                      <span className={`h-2 w-2 rounded-full ${status.dot}`} aria-hidden />
                      <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${status.badge}`}>{status.label}</span>
                    </div>
                    <p className="mt-1.5 line-clamp-2 text-[13px] leading-5 text-gray-500">{connector.description}</p>
                    <p className="mt-2 text-[11px] text-gray-400">
                      {connector.driver_kind === "managed_local_daemon" ? "本地 WebBridge" : "托管 CLI"}{connector.environment.version ? ` · v${connector.environment.version}` : ""}{connector.driver_kind === "managed_local_daemon" ? "" : ` · ${connector.installed_skill_count} 个技能`}
                    </p>
                  </div>
                  <ChevronRight className="mt-1 h-5 w-5 shrink-0 text-gray-300 transition-transform group-hover:translate-x-0.5 group-hover:text-[#002fa7]" />
                </button>
              );
            })}
          </div>
        )}
      </div>
      {selected ? (
        <ConnectorStatusModal
          connector={selected}
          onClose={closeConnector}
          onChanged={refreshSelected}
          onUse={onUse}
        />
      ) : null}
    </div>
  );
}

function ConnectorStatusModal({
  connector,
  onClose,
  onChanged,
  onUse,
}: {
  connector: ConnectorInfo;
  onClose: () => void;
  onChanged: () => Promise<void>;
  onUse: (prompt: string) => void;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const isWebBridge = connector.driver_kind === "managed_local_daemon";
  const isLark = connector.connector_id === "lark";
  const status = STATUS_COPY[connector.status];
  const app = identityLabel(connector.profile?.app_identity, "bot");
  const user = identityLabel(connector.profile?.user_identity, "user");
  const appVerifiedInCurrentFlow = Boolean(
    connector.active_flow?.completed_phase_ids?.includes("app_configuration"),
  );
  const authorizationAttemptExpired = Boolean(
    connector.active_flow?.expires_at
      && connector.active_flow.expires_at <= Date.now() / 1000,
  );

  useEffect(() => {
    const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), details summary, [tabindex]:not([tabindex="-1"])',
        ) || [],
      ).filter((element) => !element.hasAttribute("disabled"));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousBodyOverflow;
      returnFocus?.focus();
    };
  }, [onClose]);

  useEffect(() => {
    if (!connector.active_flow) return;
    const timer = window.setInterval(() => { void onChanged(); }, 2500);
    return () => window.clearInterval(timer);
  }, [connector.active_flow, onChanged]);

  const run = async (name: string, action: () => Promise<unknown>) => {
    try {
      setBusy(name);
      setError(null);
      await action();
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(null);
    }
  };

  const primaryAction = () => {
    if (connector.status === "environment_unavailable") {
      onUse(`请安装并配置 ${connector.display_name} 的托管 CLI（${connector.environment.package}）。`);
      onClose();
      return;
    }
    if (connector.status === "connected") {
      onUse(`使用 ${connector.display_name} 帮我……`);
      onClose();
      return;
    }
    if (connector.status === "authorizing") {
      onUse(`我已完成 ${connector.display_name} 的浏览器授权，请继续验证并完成当前授权流程。`);
      onClose();
      return;
    }
    const mode = connector.status === "authorization_required" ? "user_reauthorize" : "full_replace";
    void run("authorize", () => authorizeConnector(connector.connector_id, mode));
  };

  const modal = (
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-[2px]" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="connector-title" className="relative flex max-h-[calc(100vh-2rem)] w-full max-w-[700px] flex-col overflow-hidden rounded-3xl border border-white/60 bg-white shadow-2xl">
        <button ref={closeRef} type="button" onClick={onClose} aria-label="关闭连接器状态" className="absolute right-5 top-5 z-10 flex h-9 w-9 items-center justify-center rounded-full bg-white/90 text-gray-400 shadow-sm ring-1 ring-black/[0.04] backdrop-blur transition-colors hover:bg-gray-100 hover:text-gray-700">
          <X className="h-5 w-5" />
        </button>
        <div className="my-2 mr-2 min-h-0 flex-1 overflow-y-auto overscroll-contain px-6 pb-4 pt-3 [scrollbar-gutter:stable] sm:px-9 sm:pb-6">
        <div className="flex flex-col items-center px-8 pb-5 pt-5 text-center">
          <ConnectorMark connectorId={connector.connector_id} large />
          <h2 id="connector-title" className="mt-4 text-2xl font-semibold text-gray-950">{connector.display_name}</h2>
          <p className="mt-2 max-w-xl text-[14px] leading-6 text-gray-500">{connector.description}</p>
          <span className={`mt-3 inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium ${status.badge}`}>
            <span className={`h-2 w-2 rounded-full ${status.dot}`} />{status.label}
          </span>
        </div>

        <div className="space-y-3">
          {isWebBridge ? (
            <>
            <StatusGroup icon={<Server className="h-4 w-4" />} title="连接状态">
              <StatusRow label="本地组件" value={connector.environment.health === "available" ? "已安装" : "未安装"} ok={connector.environment.health === "available"} />
              <StatusRow label="PuddingClaw" value={connector.environment.enabled ? "已启用" : "未启用"} ok={connector.environment.enabled} />
              <StatusRow label="本地 daemon" value={connector.environment.daemon_running ? "运行中" : "未运行"} ok={connector.environment.daemon_running} />
              <StatusRow label="浏览器扩展" value={connector.environment.extension_connected ? "已连接" : "未连接"} ok={connector.environment.extension_connected} />
              <StatusRow label="版本匹配" value={connector.environment.version_compatible === false ? "不匹配，请升级扩展" : "已匹配"} ok={connector.environment.version_compatible !== false} />
            </StatusGroup>
            {connector.environment.version_compatible === false ? (
              <div className="rounded-2xl border border-amber-200 bg-amber-50/80 p-4 text-xs leading-5 text-amber-900">
                <p className="font-semibold">检测到 daemon 与浏览器扩展版本不一致</p>
                <p className="mt-1">当前 daemon：{connector.environment.version || "未知"} · 扩展：{connector.environment.extension_version || "未知"}</p>
                <ol className="mt-2 list-decimal space-y-1 pl-4">
                  <li>打开 Chrome 或 Edge 的扩展管理页：<code>chrome://extensions/</code> 或 <code>edge://extensions/</code>。</li>
                  <li>在商店中更新 Kimi WebBridge；如果是手动安装，请从官方页面下载新版并重新加载解压后的扩展目录。</li>
                  <li>重启浏览器，确认 WebBridge 图标显示已连接。</li>
                  <li>回到这里点击“重新检测”。</li>
                </ol>
                <a href={WEBBRIDGE_UPDATE_URL} target="_blank" rel="noreferrer" className="mt-3 inline-flex items-center gap-1 font-medium text-[#002fa7] underline underline-offset-2">
                  打开 Kimi 官方安装/更新页面 <ExternalLink className="h-3.5 w-3.5" />
                </a>
              </div>
            ) : null}
            </>
          ) : (
            <>
              <StatusGroup icon={<Server className="h-4 w-4" />} title="运行环境">
                <StatusRow label="共享 Toolchain" value={connector.environment.health === "available" ? "可用" : "不可用"} ok={connector.environment.health === "available"} />
                <StatusRow label="执行驱动" value={`托管 CLI${connector.environment.version ? ` · v${connector.environment.version}` : ""}`} />
                <StatusRow label="可用范围" value="所有项目" />
              </StatusGroup>
              <StatusGroup icon={<KeyRound className="h-4 w-4" />} title="授权状态">
                {isLark ? (
                  <>
                <StatusRow
                  icon={<Bot className="h-4 w-4" />}
                  label="应用 / Bot 配置"
                  value={appVerifiedInCurrentFlow ? "本次流程已验证，待提交" : app.text}
                  ok={appVerifiedInCurrentFlow || app.ok}
                />
                <StatusRow
                  icon={<UserRound className="h-4 w-4" />}
                  label="用户数据授权"
                  value={authorizationAttemptExpired ? "链接已过期，等待续发" : connector.active_flow ? "授权进行中" : user.text}
                  ok={!connector.active_flow && user.ok}
                  pending={Boolean(connector.active_flow) && !authorizationAttemptExpired}
                />
                <StatusRow icon={<Clock3 className="h-4 w-4" />} label="最近验证" value={formatRelativeTime(connector.profile?.last_updated_at)} />
                  </>
                ) : (
                  <>
                    <StatusRow
                      icon={<KeyRound className="h-4 w-4" />}
                      label="连接凭证"
                      value={connector.active_flow ? "授权进行中" : connector.profile?.health || "未配置"}
                      ok={connector.status === "connected"}
                      pending={Boolean(connector.active_flow)}
                    />
                    <StatusRow icon={<Clock3 className="h-4 w-4" />} label="最近验证" value={formatRelativeTime(connector.profile?.last_updated_at)} />
                  </>
                )}
              </StatusGroup>
            </>
          )}
        </div>

        {connector.active_flow ? (
          <div className="mt-4 rounded-2xl border border-blue-100 bg-blue-50/70 p-4">
            <p className="text-sm font-semibold text-slate-900">第 {connector.active_flow.phase.step}/{connector.active_flow.phase.total} 步 · {connector.active_flow.phase.title}</p>
            <p className="mt-1 text-xs leading-5 text-slate-600">{connector.active_flow.phase.description}</p>
            {authorizationAttemptExpired ? (
              <p className="mt-2 text-xs font-medium leading-5 text-amber-700">上一授权链接已经过期。回到对话并发送预填内容，Backend 会在同一流程中生成新链接。</p>
            ) : connector.active_flow.user_code ? <p className="mt-2 text-xs text-slate-600">验证码：<span className="font-mono font-semibold text-slate-900">{connector.active_flow.user_code}</span></p> : null}
            {connector.active_flow.verification_url && !authorizationAttemptExpired ? (
              <a href={connector.active_flow.verification_url} target="_blank" rel="noreferrer" className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium text-[#002fa7] underline underline-offset-2">
                打开 {connector.display_name} 授权页面 <ExternalLink className="h-3.5 w-3.5" />
              </a>
            ) : null}
          </div>
        ) : null}

        {error ? <div className="mt-4 flex items-start gap-2 rounded-xl bg-red-50 px-3 py-2.5 text-xs text-red-700"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />{error}</div> : null}

        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          {connector.driver_kind === "managed_local_daemon" ? (
            <>
              <button type="button" disabled={Boolean(busy)} onClick={() => void run("probe", () => probeKimiWebBridge())} className="inline-flex h-10 items-center gap-2 rounded-xl border border-gray-200 px-4 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:opacity-50"><RotateCcw className="h-4 w-4" />重新检测</button>
              {connector.environment.version_compatible === false ? <a href={WEBBRIDGE_UPDATE_URL} target="_blank" rel="noreferrer" className="inline-flex h-10 items-center gap-2 rounded-xl border border-amber-200 px-4 text-sm font-medium text-amber-700 transition-colors hover:bg-amber-50">更新浏览器扩展 <ExternalLink className="h-4 w-4" /></a> : connector.environment.enabled ? <button type="button" disabled={Boolean(busy)} onClick={() => void run("disable", () => setKimiWebBridgeEnabled(false))} className="inline-flex h-10 items-center gap-2 rounded-xl border border-gray-200 px-4 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:opacity-50">停用</button> : connector.environment.health === "available" ? <button type="button" disabled={Boolean(busy)} onClick={() => void run("enable", () => setKimiWebBridgeEnabled(true))} className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-5 text-sm font-medium text-white shadow-sm transition-colors hover:bg-[#001f7a] disabled:opacity-50">启用</button> : <button type="button" disabled={Boolean(busy)} onClick={() => void run("install", () => installKimiWebBridge())} className="inline-flex h-10 items-center gap-2 rounded-xl border border-amber-200 px-4 text-sm font-medium text-amber-700 transition-colors hover:bg-amber-50 disabled:opacity-50">安装说明</button>}
            </>
          ) : null}
          {connector.driver_kind !== "managed_local_daemon" && connector.authorization_supported !== false && connector.status === "connected" ? (
            <button type="button" disabled={Boolean(busy)} onClick={() => void run("reauthorize", () => authorizeConnector(connector.connector_id, "user_reauthorize"))} className="inline-flex h-10 items-center gap-2 rounded-xl border border-gray-200 px-4 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:opacity-50">
              {busy === "reauthorize" ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}重新授权
            </button>
          ) : null}
          {connector.driver_kind !== "managed_local_daemon" && connector.authorization_supported !== false ? <button type="button" disabled={Boolean(busy)} onClick={primaryAction} className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-5 text-sm font-medium text-white shadow-sm transition-colors hover:bg-[#001f7a] disabled:opacity-50">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : connector.status === "connected" ? <ChevronRight className="h-4 w-4" /> : <KeyRound className="h-4 w-4" />}
            {connector.status === "connected" ? `去使用 ${connector.display_name}` : connector.status === "authorizing" ? authorizationAttemptExpired ? "回到对话刷新链接" : "回到对话继续" : connector.status === "authorization_required" ? "重新授权" : connector.status === "environment_unavailable" ? "安装运行环境" : `连接 ${connector.display_name}`}
          </button> : null}
        </div>
        <div className="mt-4 flex flex-wrap items-center justify-center gap-4 text-xs">
          {connector.driver_kind !== "managed_local_daemon" && connector.authorization_supported !== false && connector.status !== "unconfigured" && connector.status !== "environment_unavailable" ? (
            <button type="button" disabled={Boolean(busy)} onClick={() => void run("full", () => authorizeConnector(connector.connector_id, "full_replace"))} className="text-gray-500 hover:text-gray-800 disabled:opacity-50">完整重新配置</button>
          ) : null}
          {connector.profile && connector.status !== "revoked" ? (
            <button type="button" disabled={Boolean(busy)} onClick={() => {
              if (!window.confirm(`断开 ${connector.display_name} 连接会撤销当前 Profile，并使正在进行的授权失效。确定继续吗？`)) return;
              void run("revoke", () => revokeConnector(connector.connector_id));
            }} className="inline-flex items-center gap-1 text-red-500 hover:text-red-700 disabled:opacity-50"><Unplug className="h-3.5 w-3.5" />断开连接…</button>
          ) : null}
        </div>
        <details className="mt-5 rounded-xl border border-black/[0.05] bg-gray-50/70 px-4 py-3 text-xs text-gray-500">
          <summary className="cursor-pointer font-medium text-gray-600">技术信息</summary>
          <dl className="mt-3 grid grid-cols-[100px_1fr] gap-x-3 gap-y-2">
            <dt>实现</dt><dd>{connector.environment.package}</dd>
            <dt>驱动</dt><dd>{isWebBridge ? "本地 daemon" : "托管 CLI"}</dd>
            {isWebBridge ? (
              <>
                <dt>范围</dt><dd>当前用户</dd>
                <dt>版本</dt><dd>{connector.environment.version || "未知"}</dd>
                <dt>扩展版本</dt><dd>{connector.environment.extension_version || "未知"}</dd>
              </>
            ) : (
              <>
                <dt>Profile</dt><dd>{connector.profile?.label || "尚未创建"}</dd>
                <dt>凭证</dt><dd>由 PuddingClaw 加密管理</dd>
              </>
            )}
          </dl>
        </details>
        </div>
      </section>
    </div>
  );
  return createPortal(modal, document.body);
}

function StatusGroup({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return <div className="rounded-2xl border border-black/[0.06] bg-gray-50/60 p-4"><div className="mb-2.5 flex items-center gap-2 text-sm font-semibold text-gray-800">{icon}{title}</div><div className="space-y-2">{children}</div></div>;
}

function StatusRow({ icon, label, value, ok, pending }: { icon?: ReactNode; label: string; value: string; ok?: boolean; pending?: boolean }) {
  return <div className="flex min-h-7 items-center gap-2 text-[13px]"><span className="text-gray-400">{icon}</span><span className="text-gray-500">{label}</span><span className="ml-auto inline-flex items-center gap-1.5 font-medium text-gray-800">{pending ? <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-500" /> : ok === true ? <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" /> : ok === false ? <AlertCircle className="h-3.5 w-3.5 text-amber-500" /> : null}{value}</span></div>;
}
