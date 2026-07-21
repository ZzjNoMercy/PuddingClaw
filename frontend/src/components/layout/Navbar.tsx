"use client";

import { Bell, Check, ChevronDown, Cpu, Loader2, PanelLeft, PanelRight, RefreshCw, X } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";
import {
  getSemanticDimensionBuildJob,
  listTaskNotifications,
  markTaskNotificationRead,
  type SemanticDimensionBuildJobDetail,
  type TaskNotification,
} from "@/lib/api";
import { useApp } from "@/lib/store";

interface NavbarProps {
  sidebarOpen?: boolean;
  toggleSidebar?: () => void;
  inspectorOpen?: boolean;
  inspectorAvailable?: boolean;
  toggleInspector?: () => void;
  onToggleTrace?: () => void;
  traceSpanCount?: number;
  traceActive?: boolean;
  /** Hide sidebar/inspector toggles on non-chat pages */
  showPanelToggles?: boolean;
  /** Optional centered title (e.g. current session name) */
  title?: string;
  /** Compact mode for rendering only the sidebar toggle inside the drawer. */
  compact?: boolean;
}

export default function Navbar({
  sidebarOpen,
  toggleSidebar,
  inspectorOpen,
  inspectorAvailable = true,
  toggleInspector,
  onToggleTrace,
  traceSpanCount,
  traceActive,
  showPanelToggles = false,
  title,
  compact = false,
}: NavbarProps) {
  const router = useRouter();
  const { setSessionId } = useApp();
  const [notifications, setNotifications] = useState<TaskNotification[]>([]);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const notificationMenuRef = useRef<HTMLDivElement>(null);
  const [notificationJobDetail, setNotificationJobDetail] = useState<SemanticDimensionBuildJobDetail | null>(null);
  const [notificationJobTitle, setNotificationJobTitle] = useState("");
  const [notificationJobLoading, setNotificationJobLoading] = useState(false);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const items = await listTaskNotifications(false, 20);
        if (active) {
          setNotifications(items);
        }
      } catch {
        // The bell is an enhancement; pages remain usable while backend is restarting.
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!notificationsOpen) return;

    const closeOnOutsideInteraction = (event: PointerEvent) => {
      if (!notificationMenuRef.current?.contains(event.target as Node)) {
        setNotificationsOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setNotificationsOpen(false);
    };

    document.addEventListener("pointerdown", closeOnOutsideInteraction);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsideInteraction);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [notificationsOpen]);

  const unreadCount = notifications.filter((item) => !item.read_at).length;
  const markRead = async (notification: TaskNotification) => {
    if (notification.read_at) return;
    try {
      const updated = await markTaskNotificationRead(notification.id);
      setNotifications((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      const sessionId = String(updated.payload?.session_id || "").trim();
      if (sessionId) {
        setSessionId(sessionId);
        router.push("/");
      }
    } catch {
      // Keep the notification visible when the request fails.
    }
  };

  const openNotificationDetail = async (notification: TaskNotification) => {
    const jobId = String(notification.subject_id || notification.payload?.job_id || "").trim();
    if (notification.subject_type !== "semantic_dimension_build_job" || !jobId) {
      await markRead(notification);
      return;
    }
    setNotificationsOpen(false);
    setNotificationJobTitle(notification.title);
    setNotificationJobDetail(null);
    setNotificationJobLoading(true);
    try {
      if (!notification.read_at) {
        const updated = await markTaskNotificationRead(notification.id);
        setNotifications((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      }
      setNotificationJobDetail(await getSemanticDimensionBuildJob(jobId));
    } catch {
      // Keep the detail modal closed if the task can no longer be loaded.
    } finally {
      setNotificationJobLoading(false);
    }
  };

  if (compact) {
    return (
      <div className="flex h-full items-center">
        {showPanelToggles && toggleSidebar ? (
          <button
            onClick={toggleSidebar}
            className={`w-8 h-8 flex items-center justify-center rounded-lg transition-all ${
              sidebarOpen
                ? "bg-[#002fa7]/[0.08] text-[#002fa7] shadow-sm"
                : "text-gray-400 hover:text-gray-700 hover:bg-black/[0.04]"
            }`}
            title="切换侧栏"
            aria-label="切换侧栏"
          >
            <PanelLeft className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>
    );
  }

  return (
    <nav className="glass-nav sticky top-0 z-50 h-11 flex items-center justify-between px-3">
      {/* Left — Sidebar toggle (chat page only) or spacer */}
      <div className="w-[120px] flex items-center">
        {showPanelToggles && toggleSidebar ? (
          <button
            onClick={toggleSidebar}
            className={`w-8 h-8 flex items-center justify-center rounded-lg transition-all ${
              sidebarOpen
                ? "bg-[#002fa7]/[0.08] text-[#002fa7] shadow-sm"
                : "text-gray-400 hover:text-gray-700 hover:bg-black/[0.04]"
            }`}
            title="切换侧栏"
            aria-label="切换侧栏"
          >
            <PanelLeft className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>

      {/* Center — Title */}
      <div className="flex min-w-0 flex-1 items-center justify-center gap-2 px-4">
        {title ? (
          <button className="flex items-center gap-1.5 text-[14px] font-medium text-gray-800 hover:bg-black/[0.04] px-3 py-1.5 rounded-lg transition-colors">
            <span className="truncate">{title}</span>
            <ChevronDown className="w-[14px] h-[14px] text-gray-400" />
          </button>
        ) : null}
      </div>

      {/* Right — Inspector toggle (chat page only) or spacer */}
      <div className="w-[160px] flex justify-end gap-1.5 relative">
        <div ref={notificationMenuRef} className="relative">
          <button
            onClick={() => setNotificationsOpen((value) => !value)}
            className={`relative w-8 h-8 flex items-center justify-center rounded-lg transition-all ${
              notificationsOpen ? "bg-[#002fa7]/[0.08] text-[#002fa7]" : "text-gray-400 hover:text-gray-700 hover:bg-black/[0.04]"
            }`}
            title="任务通知"
            aria-label="任务通知"
            aria-expanded={notificationsOpen}
          >
            <Bell className="w-[16px] h-[16px]" />
            {unreadCount > 0 ? (
              <span className="absolute -right-1 -top-1 min-w-4 h-4 rounded-full bg-[#d92d20] px-1 text-[10px] leading-4 font-semibold text-white">
                {unreadCount > 9 ? "9+" : unreadCount}
              </span>
            ) : null}
          </button>
          {notificationsOpen ? (
            <div className="absolute right-0 top-10 z-[70] w-[360px] overflow-hidden rounded-lg border border-black/[0.08] bg-white shadow-xl">
            <div className="flex items-center justify-between border-b border-black/[0.06] px-4 py-3">
              <span className="text-[13px] font-semibold text-gray-900">通知中心</span>
              <span className="text-[11px] text-gray-400">{unreadCount ? `${unreadCount} 条未读` : "已读完"}</span>
            </div>
            <div className="max-h-[360px] overflow-y-auto">
              {notifications.length ? notifications.map((notification) => (
                <button
                  key={notification.id}
                  onClick={() => void openNotificationDetail(notification)}
                  className={`block w-full border-b border-black/[0.05] px-4 py-3 text-left transition-colors hover:bg-black/[0.025] ${
                    notification.read_at ? "opacity-60" : "bg-[#f7f9ff]"
                  }`}
                >
                  <div className="flex items-start gap-2">
                    <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[#0b8a62]" />
                    <div className="min-w-0">
                      <div className="truncate text-[12px] font-semibold text-gray-800">{notification.title}</div>
                      <div className="mt-1 text-[11px] leading-4 text-gray-500">{notification.body}</div>
                      {notification.subject_id ? <div className="mt-1 font-mono text-[10px] text-gray-400">{notification.subject_id}</div> : null}
                    </div>
                  </div>
                </button>
              )) : (
                <div className="px-4 py-8 text-center text-[12px] text-gray-400">暂无任务通知</div>
              )}
            </div>
            </div>
          ) : null}
        </div>
        {showPanelToggles && onToggleTrace ? (
          <button
            onClick={onToggleTrace}
            className={`h-8 flex items-center gap-1.5 rounded-lg px-2.5 text-[12px] font-semibold transition-all ${
              traceActive
                ? "bg-[#002fa7]/[0.08] text-[#002fa7] shadow-sm"
                : "text-gray-500 hover:text-gray-800 hover:bg-black/[0.04]"
            }`}
            title={traceActive ? "关闭 Trace 看板" : "打开 Trace 看板"}
            aria-label={traceActive ? "关闭 Trace 看板" : "打开 Trace 看板"}
          >
            <Cpu className="w-[15px] h-[15px]" />
            <span className="hidden lg:inline">Trace</span>
            {typeof traceSpanCount === "number" && traceSpanCount > 0 ? (
              <span className="rounded-full bg-black/[0.06] px-1.5 py-0.5 text-[10px] font-medium">
                {traceSpanCount}
              </span>
            ) : null}
          </button>
        ) : null}
        {showPanelToggles && toggleInspector ? (
          <button
            onClick={toggleInspector}
            disabled={!inspectorAvailable}
            className={`w-8 h-8 flex items-center justify-center rounded-lg transition-all ${
              !inspectorAvailable
                ? "cursor-not-allowed text-gray-200"
                : inspectorOpen
                ? "bg-[#002fa7]/[0.08] text-[#002fa7] shadow-sm"
                : "text-gray-400 hover:text-gray-700 hover:bg-black/[0.04]"
            }`}
            title={inspectorAvailable ? "切换右侧面板" : "暂无可展示内容"}
            aria-label={inspectorAvailable ? "切换右侧面板" : "右侧面板暂无内容"}
          >
            <PanelRight className="w-[16px] h-[16px]" />
          </button>
        ) : null}
      </div>
      {notificationJobLoading || notificationJobDetail ? (
        <NotificationJobDetailModal
          detail={notificationJobDetail}
          title={notificationJobTitle}
          loading={notificationJobLoading}
          onClose={() => {
            setNotificationJobDetail(null);
            setNotificationJobLoading(false);
          }}
        />
      ) : null}
    </nav>
  );
}

function formatDateTime(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function jobStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "处理中",
    waiting_for_publish_confirmation: "等待发布确认",
    published: "已发布",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status] || status || "未知状态";
}

function NotificationJobDetailModal({
  detail,
  title,
  loading,
  onClose,
}: {
  detail: SemanticDimensionBuildJobDetail | null;
  title: string;
  loading: boolean;
  onClose: () => void;
}) {
  const job = detail?.job;
  const status = String(job?.status || "");
  const artifactPaths = job?.result_summary?.artifact_paths;

  const dialog = (
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
      <section className="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-[18px] bg-white shadow-2xl ring-1 ring-black/[0.08]" role="dialog" aria-modal="true" aria-label="任务明细">
        <header className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-full bg-[#002fa7]/[0.08] px-2 py-0.5 text-[11px] font-semibold text-[#002fa7]">语义维度</span>
              {status ? <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${status === "waiting_for_publish_confirmation" ? "bg-amber-50 text-amber-700" : status === "failed" ? "bg-red-50 text-red-700" : status === "published" || status === "succeeded" ? "bg-emerald-50 text-emerald-700" : "bg-blue-50 text-blue-700"}`}>{jobStatusLabel(status)}</span> : null}
            </div>
            <h2 className="mt-2 truncate text-lg font-semibold text-gray-950">{title || "语义维度构建任务"}</h2>
            {job?.id ? <p className="mt-1 font-mono text-xs text-gray-400">{job.id}</p> : null}
          </div>
          <button type="button" onClick={onClose} className="rounded-lg p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-gray-900" aria-label="关闭任务明细">
            <X className="h-5 w-5" />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-5">
          {loading ? (
            <div className="flex min-h-56 items-center justify-center text-sm text-gray-400"><Loader2 className="mr-2 h-4 w-4 animate-spin" />正在读取任务明细…</div>
          ) : job ? (
            <>
              <div className="grid gap-3 sm:grid-cols-3">
                <JobMetric title="状态" value={jobStatusLabel(status)} />
                <JobMetric title="进度" value={`${Number(job.progress || 0)}%`} icon={<RefreshCw className="h-4 w-4" />} />
                <JobMetric title="完成时间" value={formatDateTime(job.finished_at || job.updated_at)} />
              </div>
              {status === "waiting_for_publish_confirmation" ? (
                <p className="mt-5 rounded-lg border border-amber-500/15 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-800">构建产物已在 staging 校验完成，尚未发布，不会参与正式分析。</p>
              ) : null}
              {job.staging_path ? <InfoBlock title="Staging 目录" value={job.staging_path} /> : null}
              {artifactPaths && typeof artifactPaths === "object" ? <InfoBlock title="构建产物" value={JSON.stringify(artifactPaths, null, 2)} mono /> : null}
              {job.result_summary ? <InfoBlock title="构建摘要" value={JSON.stringify(job.result_summary, null, 2)} mono /> : null}
              <section className="mt-5 rounded-lg border border-black/[0.06] p-4">
                <div className="flex items-center justify-between"><h3 className="text-sm font-semibold text-gray-950">执行事件</h3><span className="text-xs text-gray-400">{detail.events.length} 条</span></div>
                <ol className="mt-3 space-y-3">
                  {detail.events.map((event) => <li key={event.id} className="border-l-2 border-[#002fa7]/20 pl-3"><p className="text-sm text-gray-700">{event.message}</p><p className="mt-1 text-xs text-gray-400">{formatDateTime(event.created_at)}</p></li>)}
                </ol>
              </section>
            </>
          ) : null}
        </div>
      </section>
    </div>
  );
  return typeof document === "undefined" ? null : createPortal(dialog, document.body);
}

function JobMetric({ title, value, icon }: { title: string; value: string; icon?: ReactNode }) {
  return <div className="rounded-lg border border-black/[0.06] bg-white p-3"><div className="flex items-center gap-1.5 text-xs text-gray-400">{icon}{title}</div><p className="mt-2 truncate text-sm font-semibold text-gray-950" title={value}>{value}</p></div>;
}

function InfoBlock({ title, value, mono = false }: { title: string; value: string; mono?: boolean }) {
  return <section className="mt-5 rounded-lg border border-black/[0.06] p-4"><h3 className="text-sm font-semibold text-gray-950">{title}</h3><pre className={`mt-3 max-h-56 overflow-auto whitespace-pre-wrap break-all text-xs leading-5 text-gray-600 ${mono ? "rounded-lg bg-gray-950 p-3 font-mono text-gray-100" : "font-mono"}`}>{value}</pre></section>;
}
