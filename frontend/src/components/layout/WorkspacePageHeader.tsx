import type { ReactNode } from "react";

type Props = {
  eyebrow: string;
  title: ReactNode;
  description: ReactNode;
  actions?: ReactNode;
};

export default function WorkspacePageHeader({ eyebrow, title, description, actions }: Props) {
  return (
    <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
      <div className="min-w-0">
        <p className="text-xs font-semibold tracking-[0.16em] text-[#002fa7]">{eyebrow}</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight text-gray-950">{title}</h1>
        <div className="mt-2 max-w-3xl text-sm leading-6 text-gray-500">{description}</div>
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}
