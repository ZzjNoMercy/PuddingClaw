import Link from "next/link";
import { Puzzle, Terminal } from "lucide-react";

const extensionNames: Record<string, string> = {
  knowledge: "知识库",
  analytics: "智能问数",
  headless_worker: "Agent Worker",
};

export default function ExtensionDisabledPage({
  searchParams,
}: {
  searchParams?: { extension?: string };
}) {
  const extension = searchParams?.extension || "extension";
  const displayName = extensionNames[extension] || "该扩展";

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 px-6 text-slate-900">
      <section className="w-full max-w-lg rounded-3xl border border-slate-200 bg-white p-8 shadow-sm">
        <div className="mb-6 flex h-12 w-12 items-center justify-center rounded-2xl bg-blue-50 text-blue-600">
          <Puzzle className="h-6 w-6" aria-hidden="true" />
        </div>
        <h1 className="text-2xl font-semibold tracking-tight">{displayName}功能尚未启用</h1>
        <p className="mt-3 leading-7 text-slate-600">
          当前运行配置没有加载这项扩展。启用后，相关页面、API、工具和后台任务才会加载。
        </p>
        <div className="mt-6 rounded-2xl bg-slate-950 p-4 text-sm text-slate-100">
          <div className="mb-2 flex items-center gap-2 text-slate-400">
            <Terminal className="h-4 w-4" aria-hidden="true" />
            <span>在终端运行</span>
          </div>
          <code>puddingclaw init</code>
        </div>
        <div className="mt-7 flex items-center gap-4">
          <Link href="/" className="rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-blue-700">
            返回工作台
          </Link>
          <span className="text-sm text-slate-500">完成配置后需要重启 PuddingClaw</span>
        </div>
      </section>
    </main>
  );
}
