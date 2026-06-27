"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";

export default function AppControlPage() {
  const router = useRouter();
  const [isElectron, setIsElectron] = useState(false);
  const [backendStatus, setBackendStatus] = useState<{ status: string; error: string | null; url: string } | null>(null);
  const [infraStatus, setInfraStatus] = useState<{
    docker: boolean;
    higress: string;
    milvus: string;
    status: string;
    error: string | null;
  } | null>(null);
  const [loading, setLoading] = useState<{ backend: boolean; infra: boolean }>({
    backend: false,
    infra: false,
  });

  useEffect(() => {
    const electron = typeof window !== "undefined" && !!window.electron;
    setIsElectron(electron);

    if (!electron) return;

    // 初始状态
    window.electron?.getBackendStatus().then((status) => {
      setBackendStatus(status);
      // 如果 backend 没运行，自动启动
      if (status.status !== "running") {
        setLoading((p) => ({ ...p, backend: true }));
        window.electron?.startBackend().finally(() => {
          setLoading((p) => ({ ...p, backend: false }));
        });
      }
    });
    window.electron?.getInfraStatus().then(setInfraStatus);

    // 监听状态变化
    const handleBackendStatus = (_event: unknown, status: unknown) => {
      setBackendStatus(status as { status: string; error: string | null; url: string });
    };
    const handleInfraStatus = (_event: unknown, status: unknown) => {
      setInfraStatus(status as { docker: boolean; higress: string; milvus: string; status: string; error: string | null });
    };

    window.electron?.onBackendStatusChange(handleBackendStatus);
    window.electron?.onInfraStatusChange(handleInfraStatus);

    return () => {
      window.electron?.removeAllListeners("backend-status-change");
      window.electron?.removeAllListeners("infra-status-change");
    };
  }, []);

  const handleStopBackend = async () => {
    if (!window.electron) return;
    setLoading((p) => ({ ...p, backend: true }));
    await window.electron.stopBackend();
    setLoading((p) => ({ ...p, backend: false }));
  };

  const handleStartInfra = async () => {
    if (!window.electron) return;
    setLoading((p) => ({ ...p, infra: true }));
    await window.electron.startInfra();
    setLoading((p) => ({ ...p, infra: false }));
  };

  const handleStopInfra = async () => {
    if (!window.electron) return;
    setLoading((p) => ({ ...p, infra: true }));
    await window.electron.stopInfra();
    setLoading((p) => ({ ...p, infra: false }));
  };

  const statusColor: Record<string, string> = {
    running: "text-green-600 bg-green-50 border-green-200",
    stopped: "text-gray-500 bg-gray-50 border-gray-200",
    starting: "text-yellow-600 bg-yellow-50 border-yellow-200",
    error: "text-red-600 bg-red-50 border-red-200",
    partial: "text-yellow-600 bg-yellow-50 border-yellow-200",
    unknown: "text-gray-500 bg-gray-50 border-gray-200",
  };

  if (!isElectron) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-50 p-8">
        <div className="max-w-md w-full bg-white rounded-xl shadow-sm border border-gray-200 p-6 text-center">
          <h1 className="text-lg font-semibold text-gray-900 mb-2">请在 Electron 中运行</h1>
          <p className="text-sm text-gray-600">此控制面板需要在 PuddingClaw 桌面应用内打开。</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 p-8">
      <div className="max-w-2xl mx-auto space-y-6">
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
          <h1 className="text-2xl font-semibold text-gray-900 mb-2">PuddingClaw 控制台</h1>
          <p className="text-sm text-gray-500">Backend 已设为自动启动，Docker 基础设施需要手动启动。</p>
        </div>

        {/* Backend */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-gray-900">PuddingClaw Backend</h2>
            {backendStatus && (
              <span className={`px-3 py-1 rounded-full text-xs font-medium border ${statusColor[backendStatus.status] || statusColor.unknown}`}>
                {loading.backend && backendStatus.status !== "running" ? "启动中..." : backendStatus.status}
              </span>
            )}
          </div>

          {backendStatus && (
            <div className="space-y-2 mb-4 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-600">API 地址</span>
                <span className="font-mono text-gray-900">{backendStatus.url}</span>
              </div>
              {backendStatus.error && (
                <div className="text-red-600 text-xs mt-2">{backendStatus.error}</div>
              )}
            </div>
          )}

          <div className="flex gap-3">
            <button
              onClick={handleStopBackend}
              disabled={loading.backend || backendStatus?.status === "stopped"}
              className="px-4 py-2 bg-white text-gray-700 border border-gray-300 rounded-lg text-sm font-medium hover:bg-gray-50 disabled:opacity-40 transition-colors"
            >
              停止 Backend
            </button>
          </div>
        </div>

        {/* Docker Infra */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-gray-900">Docker 基础设施</h2>
            {infraStatus && (
              <span className={`px-3 py-1 rounded-full text-xs font-medium border ${statusColor[infraStatus.status] || statusColor.unknown}`}>
                {infraStatus.status}
              </span>
            )}
          </div>

          {infraStatus && (
            <div className="space-y-2 mb-4 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-600">Docker Desktop</span>
                <span className={infraStatus.docker ? "text-green-600" : "text-red-600"}>
                  {infraStatus.docker ? "运行中" : "未运行"}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-600">Higress</span>
                <span className="capitalize text-gray-900">{infraStatus.higress}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-600">Milvus</span>
                <span className="capitalize text-gray-900">{infraStatus.milvus}</span>
              </div>
              {infraStatus.error && (
                <div className="text-red-600 text-xs mt-2">{infraStatus.error}</div>
              )}
            </div>
          )}

          <div className="flex gap-3">
            <button
              onClick={handleStartInfra}
              disabled={loading.infra}
              className="px-4 py-2 bg-[#002fa7] text-white rounded-lg text-sm font-medium hover:bg-[#001f7a] disabled:opacity-40 transition-colors"
            >
              {loading.infra ? "启动中..." : "启动 Infra"}
            </button>
            <button
              onClick={handleStopInfra}
              disabled={loading.infra}
              className="px-4 py-2 bg-white text-gray-700 border border-gray-300 rounded-lg text-sm font-medium hover:bg-gray-50 disabled:opacity-40 transition-colors"
            >
              停止 Infra
            </button>
          </div>
        </div>

        {/* Enter App */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
          <button
            onClick={() => router.push("/")}
            disabled={backendStatus?.status !== "running"}
            className="w-full px-4 py-3 bg-[#002fa7] text-white rounded-lg text-sm font-medium hover:bg-[#001f7a] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            进入 PuddingClaw
          </button>
          {backendStatus?.status !== "running" && (
            <p className="text-xs text-gray-500 mt-2 text-center">等待 Backend 启动完成...</p>
          )}
        </div>
      </div>
    </div>
  );
}
