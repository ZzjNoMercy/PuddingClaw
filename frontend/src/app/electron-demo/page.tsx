"use client";

import { useState } from "react";

export default function ElectronDemoPage() {
  const [path, setPath] = useState<string | null>(null);
  const [isElectron, setIsElectron] = useState<boolean>(
    typeof window !== "undefined" && !!window.electron
  );

  const handleSelectFolder = async () => {
    if (!window.electron) {
      alert("请在 Electron 环境中运行此页面");
      return;
    }

    const selected = await window.electron.selectProjectFolder();
    setPath(selected);
  };

  return (
    <div className="flex flex-col items-center justify-center min-h-screen p-8 bg-gray-50">
      <div className="max-w-md w-full bg-white rounded-xl shadow-sm border border-gray-200 p-6">
        <h1 className="text-xl font-semibold text-gray-900 mb-4">
          Electron 文件夹选择测试
        </h1>

        <div className="space-y-4">
          <div className="text-sm text-gray-600">
            运行环境：{isElectron ? "Electron ✅" : "普通浏览器 ❌"}
          </div>

          <button
            onClick={handleSelectFolder}
            disabled={!isElectron}
            className="w-full px-4 py-2 bg-[#002fa7] text-white rounded-lg text-sm font-medium
                       hover:bg-[#001f7a] disabled:opacity-40 disabled:cursor-not-allowed
                       transition-colors"
          >
            选择项目文件夹
          </button>

          {path && (
            <div className="p-3 bg-gray-50 rounded-lg border border-gray-100">
              <div className="text-xs text-gray-500 mb-1">已选择路径：</div>
              <div className="text-sm text-gray-900 break-all font-mono">
                {path}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
