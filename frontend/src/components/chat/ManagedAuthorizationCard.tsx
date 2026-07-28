"use client";

import { KeyRound } from "lucide-react";
import type { TimelineItem } from "@/lib/store";
import { managedAuthorizationRequests } from "@/lib/managedAuthorization";

function TerminalQrCode({ value }: { value: string }) {
  const lines = value.split("\n").filter(Boolean);
  if (lines.length < 10) return null;
  const width = Math.max(...lines.map((line) => Array.from(line).length));
  const height = lines.length * 2;
  const modules: string[] = [];
  lines.forEach((line, lineIndex) => {
    Array.from(line.padEnd(width, " ")).forEach((cell, x) => {
      const topIsDark = cell === " " || cell === "▄";
      const bottomIsDark = cell === " " || cell === "▀";
      if (topIsDark) modules.push(`M${x} ${lineIndex * 2}h1v1h-1z`);
      if (bottomIsDark) modules.push(`M${x} ${lineIndex * 2 + 1}h1v1h-1z`);
    });
  });
  return (
    <svg
      role="img"
      aria-label="飞书授权二维码"
      viewBox={`0 0 ${width} ${height}`}
      className="aspect-square h-auto w-64 max-w-full rounded-lg bg-white"
      shapeRendering="crispEdges"
      preserveAspectRatio="xMidYMid meet"
    >
      <rect width={width} height={height} fill="white" />
      <path d={modules.join("")} fill="black" />
    </svg>
  );
}

export default function ManagedAuthorizationCards({ timeline }: { timeline: TimelineItem[] }) {
  const requests = managedAuthorizationRequests(timeline);
  if (requests.length === 0) return null;
  return (
    <div className="mt-3 space-y-3">
      {requests.map((request) => (
        (() => {
          const awaiting = request.status === "awaiting_user";
          const completed = request.status === "completed";
          const statusLabel = awaiting
            ? "等待浏览器操作"
            : completed
              ? "授权已完成"
              : request.status === "expired"
                ? "授权链接已过期"
                : request.status === "cancelled"
                  ? "授权已取消"
                  : "授权验证失败";
          return (
        <section
          key={`${request.flow_id}:${request.phase.id}:${request.attempt ?? 0}`}
          className={`rounded-2xl border p-4 ${awaiting
            ? "border-blue-200 bg-blue-50/70"
            : completed
              ? "border-emerald-200 bg-emerald-50/70"
              : "border-amber-200 bg-amber-50/70"}`}
        >
          <div className="flex flex-wrap items-center gap-2 text-sm font-semibold text-slate-900">
            <KeyRound className="h-4 w-4 text-[#002fa7]" />
            <span>第 {request.phase.step}/{request.phase.total} 步 · {request.phase.title}</span>
            <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${awaiting
              ? "bg-blue-100 text-[#002fa7]"
              : completed
                ? "bg-emerald-100 text-emerald-800"
                : "bg-amber-100 text-amber-800"}`}
            >
              {statusLabel}
            </span>
          </div>
          <p className="mt-1 text-xs leading-5 text-slate-600">{request.phase.description}</p>
          {request.phase.total > 1 ? (
            <p className="mt-1 text-xs leading-5 text-slate-500">
              飞书需要按顺序完成两层认证；当前步骤完成并经 Backend 验证后，才会进入下一步。
            </p>
          ) : null}
          {awaiting && request.qr_ascii ? (
            <div className="mt-3 flex w-fit max-w-full rounded-xl bg-white p-4">
              <TerminalQrCode value={request.qr_ascii} />
            </div>
          ) : null}
          {awaiting && request.user_code ? (
            <div className="mt-3 text-xs text-slate-600">
              验证码：<span className="font-mono font-semibold text-slate-900">{request.user_code}</span>
            </div>
          ) : null}
          {awaiting && request.verification_url ? (
            <a
              href={request.verification_url}
              target="_blank"
              rel="noreferrer"
              className="mt-3 block break-all text-xs font-medium text-[#002fa7] underline underline-offset-2"
            >
              {request.verification_url}
            </a>
          ) : null}
          <p className="mt-3 text-xs font-medium text-slate-700">
            {awaiting
              ? request.completion_hint
              : completed
                ? "本次授权已经 Backend 验证，二维码与链接已安全停用。"
                : request.status === "expired"
                  ? "请使用新生成的授权卡片；不要继续打开这个旧链接。"
                  : "本次授权没有完成；二维码与链接已停用，请根据后续提示继续。"}
          </p>
        </section>
          );
        })()
      ))}
    </div>
  );
}
