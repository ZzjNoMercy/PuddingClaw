"use client";

import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";

import type {
  GbrainAggregator,
  GbrainExtractableSpec,
  GbrainMappingRule,
  GbrainPageSubtype,
  GbrainPageType,
  GbrainResolver,
  GbrainSchemaPackManifest,
  GbrainSubtypeField,
} from "@/lib/api";

const inputClass =
  "h-9 w-full rounded-xl border border-black/[0.08] bg-white px-3 text-sm text-gray-800 outline-none transition focus:border-[#002fa7]/35 focus:ring-2 focus:ring-[#002fa7]/10";
const areaClass = `${inputClass} h-20 resize-y py-2`;
const resolverModes = ["frontmatter", "body_first_link", "slug", "body_excerpt", "frontmatter_field"] as const;
const subtypeFields: GbrainSubtypeField[] = ["subtype", "legacy_type", "origin", "format", "kind", "period", "domain"];
const aggregators: GbrainAggregator[] = ["scalar_brier", "weighted_brier", "count_based", "cluster_summary"];

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block min-w-0">
      <span className="mb-1.5 flex items-baseline gap-2 text-xs font-medium text-gray-700">
        {label}{hint ? <span className="font-normal text-gray-400">{hint}</span> : null}
      </span>
      {children}
    </label>
  );
}

export function StringListEditor({
  value,
  onChange,
  placeholder = "输入一项",
}: {
  value: string[];
  onChange: (value: string[]) => void;
  placeholder?: string;
}) {
  const move = (index: number, delta: -1 | 1) => {
    const target = index + delta;
    if (target < 0 || target >= value.length) return;
    const next = [...value];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };
  return (
    <div className="space-y-2">
      {value.map((item, index) => (
        <div key={index} className="flex gap-1.5">
          <input className={inputClass} value={item} placeholder={placeholder} onChange={(event) => onChange(value.map((current, itemIndex) => itemIndex === index ? event.target.value : current))} />
          <button type="button" aria-label="上移" disabled={index === 0} onClick={() => move(index, -1)} className="rounded-lg px-2 text-gray-400 hover:bg-black/[0.04] disabled:opacity-25"><ArrowUp className="h-3.5 w-3.5" /></button>
          <button type="button" aria-label="下移" disabled={index === value.length - 1} onClick={() => move(index, 1)} className="rounded-lg px-2 text-gray-400 hover:bg-black/[0.04] disabled:opacity-25"><ArrowDown className="h-3.5 w-3.5" /></button>
          <button type="button" aria-label="删除" onClick={() => onChange(value.filter((_, itemIndex) => itemIndex !== index))} className="rounded-lg px-2 text-red-400 hover:bg-red-50"><Trash2 className="h-3.5 w-3.5" /></button>
        </div>
      ))}
      <button type="button" onClick={() => onChange([...value, ""])} className="inline-flex h-8 items-center gap-1 rounded-lg bg-black/[0.035] px-2.5 text-[11px] text-gray-600 hover:bg-black/[0.06]"><Plus className="h-3 w-3" /> 添加一项</button>
    </div>
  );
}

export function ReorderButtons({ index, count, onMove }: { index: number; count: number; onMove: (delta: -1 | 1) => void }) {
  return (
    <div className="flex gap-1">
      <button type="button" aria-label="上移" disabled={index === 0} onClick={() => onMove(-1)} className="rounded-lg p-2 text-gray-400 hover:bg-black/[0.04] disabled:opacity-25"><ArrowUp className="h-3.5 w-3.5" /></button>
      <button type="button" aria-label="下移" disabled={index === count - 1} onClick={() => onMove(1)} className="rounded-lg p-2 text-gray-400 hover:bg-black/[0.04] disabled:opacity-25"><ArrowDown className="h-3.5 w-3.5" /></button>
    </div>
  );
}

function Section({
  title,
  description,
  count,
  onAdd,
  children,
}: {
  title: string;
  description: string;
  count?: number;
  onAdd?: () => void;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-2xl border border-black/[0.06] bg-white p-4">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
            {count !== undefined ? <span className="rounded-full bg-black/[0.045] px-2 py-0.5 text-[10px] text-gray-500">{count}</span> : null}
          </div>
          <p className="mt-1 text-[11px] leading-5 text-gray-400">{description}</p>
        </div>
        {onAdd ? (
          <button type="button" onClick={onAdd} className="inline-flex h-8 items-center gap-1 rounded-full bg-[#002fa7]/10 px-3 text-xs font-medium text-[#002fa7] hover:bg-[#002fa7]/15">
            <Plus className="h-3.5 w-3.5" /> 添加
          </button>
        ) : null}
      </div>
      <div className="mt-3 space-y-3">{children}</div>
    </section>
  );
}

function DeleteButton({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="inline-flex h-9 items-center gap-1 rounded-xl px-3 text-xs text-red-500 hover:bg-red-50">
      <Trash2 className="h-3.5 w-3.5" /> 删除
    </button>
  );
}

function resolverMode(value: GbrainResolver | undefined): typeof resolverModes[number] | "none" {
  if (value === undefined) return "none";
  return typeof value === "object" ? "frontmatter_field" : value;
}

function resolverForMode(mode: typeof resolverModes[number]): GbrainResolver {
  return mode === "frontmatter_field" ? { frontmatter_field: "" } : mode;
}

function ResolverEditor({
  label,
  value,
  optional = false,
  onChange,
}: {
  label: string;
  value?: GbrainResolver;
  optional?: boolean;
  onChange: (value: GbrainResolver | undefined) => void;
}) {
  const mode = resolverMode(value);
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      <Field label={label}>
        <select
          className={inputClass}
          value={mode}
          onChange={(event) => {
            const next = event.target.value;
            onChange(next === "none" ? undefined : resolverForMode(next as typeof resolverModes[number]));
          }}
        >
          {optional ? <option value="none">未设置</option> : null}
          {resolverModes.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </Field>
      {typeof value === "object" ? (
        <Field label={`${label}.frontmatter_field`}>
          <input className={inputClass} value={value.frontmatter_field} onChange={(event) => onChange({ frontmatter_field: event.target.value })} />
        </Field>
      ) : null}
    </div>
  );
}

function FrontmatterValueEditor({
  value,
  onChange,
}: {
  value: GbrainPageSubtype["when"]["frontmatter_value"];
  onChange: (value: GbrainPageSubtype["when"]["frontmatter_value"]) => void;
}) {
  const kind = value === undefined ? "unset" : typeof value;
  return (
    <div className="grid grid-cols-[120px_1fr] gap-2">
      <select
        className={inputClass}
        value={kind}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next === "unset" ? undefined : next === "number" ? 0 : next === "boolean" ? false : "");
        }}
      >
        <option value="unset">未设置</option>
        <option value="string">字符串</option>
        <option value="number">数字</option>
        <option value="boolean">布尔</option>
      </select>
      {kind === "boolean" ? (
        <select className={inputClass} value={String(value)} onChange={(event) => onChange(event.target.value === "true")}>
          <option value="false">false</option><option value="true">true</option>
        </select>
      ) : kind === "number" ? (
        <input type="number" className={inputClass} value={Number(value)} onChange={(event) => onChange(Number(event.target.value))} />
      ) : kind === "string" ? (
        <input className={inputClass} value={String(value)} onChange={(event) => onChange(event.target.value)} />
      ) : <div className="flex h-9 items-center px-3 text-xs text-gray-400">不参与匹配</div>}
    </div>
  );
}

export function PageTypeAdvancedEditor({
  page,
  onChange,
}: {
  page: GbrainPageType;
  onChange: (patch: Partial<GbrainPageType>) => void;
}) {
  const spec = typeof page.extractable === "object" ? page.extractable : null;
  const [rememberedSpec, setRememberedSpec] = useState<GbrainExtractableSpec>(spec || { eval_dimensions: [] });
  useEffect(() => {
    if (spec) setRememberedSpec(spec);
  }, [spec]);
  const subtypes = page.subtypes || [];
  const updateSubtype = (index: number, patch: Partial<GbrainPageSubtype>) => {
    onChange({ subtypes: subtypes.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item) });
  };
  return (
    <details className="mt-3 rounded-xl border border-black/[0.055] bg-white/70 p-3">
      <summary className="cursor-pointer text-xs font-medium text-gray-600">高级：ExtractableSpec 与 subtypes</summary>
      <div className="mt-3 space-y-4">
        <div>
          <Field label="extractable mode">
            <select
              className={inputClass}
              value={spec ? "spec" : String(page.extractable)}
              onChange={(event) => {
                const mode = event.target.value;
                onChange({ extractable: mode === "spec" ? rememberedSpec : mode === "true" });
              }}
            >
              <option value="false">false</option>
              <option value="true">true</option>
              <option value="spec">ExtractableSpec</option>
            </select>
          </Field>
          {spec ? (
            <div className="mt-3 grid gap-3 md:grid-cols-2">
              <Field label="prompt_template"><textarea className={areaClass} value={spec.prompt_template || ""} onChange={(event) => onChange({ extractable: { ...spec, prompt_template: event.target.value || undefined } })} /></Field>
              <Field label="fixture_corpus"><input className={inputClass} value={spec.fixture_corpus || ""} onChange={(event) => onChange({ extractable: { ...spec, fixture_corpus: event.target.value || undefined } })} /></Field>
              <Field label="eval_dimensions"><StringListEditor value={spec.eval_dimensions} onChange={(value) => onChange({ extractable: { ...spec, eval_dimensions: value } })} /></Field>
              <Field label="benchmark_min_recall" hint="0–1"><input type="number" min="0" max="1" step="0.01" className={inputClass} value={spec.benchmark_min_recall ?? ""} onChange={(event) => onChange({ extractable: { ...spec, benchmark_min_recall: event.target.value === "" ? undefined : Number(event.target.value) } })} /></Field>
              <Field label="verifier_path" hint="官方保留字段"><input className={inputClass} value={spec.verifier_path || ""} onChange={(event) => onChange({ extractable: { ...spec, verifier_path: event.target.value || undefined } })} /></Field>
            </div>
          ) : null}
        </div>
        <div>
          <div className="flex items-center justify-between">
            <p className="text-xs font-medium text-gray-700">subtypes ({subtypes.length})</p>
            <button type="button" onClick={() => onChange({ subtypes: [...subtypes, { name: "", when: {} }] })} className="text-xs font-medium text-[#002fa7]">+ 添加 subtype</button>
          </div>
          <div className="mt-2 space-y-2">
            {subtypes.map((subtype, index) => (
              <div key={index} className="grid gap-2 rounded-xl bg-black/[0.025] p-3 md:grid-cols-2">
                <Field label="name"><input className={inputClass} value={subtype.name} onChange={(event) => updateSubtype(index, { name: event.target.value })} /></Field>
                <Field label="when.path_pattern"><input className={inputClass} value={subtype.when.path_pattern || ""} onChange={(event) => updateSubtype(index, { when: { ...subtype.when, path_pattern: event.target.value || undefined } })} /></Field>
                <Field label="when.frontmatter_field"><input className={inputClass} value={subtype.when.frontmatter_field || ""} onChange={(event) => updateSubtype(index, { when: { ...subtype.when, frontmatter_field: event.target.value || undefined } })} /></Field>
                <Field label="when.frontmatter_value"><FrontmatterValueEditor value={subtype.when.frontmatter_value} onChange={(value) => updateSubtype(index, { when: { ...subtype.when, frontmatter_value: value } })} /></Field>
                <div className="md:col-span-2 flex justify-end"><DeleteButton onClick={() => onChange({ subtypes: subtypes.filter((_, itemIndex) => itemIndex !== index) || undefined })} /></div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </details>
  );
}

function MappingRuleEditor({
  rule,
  onChange,
  onDelete,
  index,
  count,
  onMove,
}: {
  rule: GbrainMappingRule;
  onChange: (rule: GbrainMappingRule) => void;
  onDelete: () => void;
  index: number;
  count: number;
  onMove: (delta: -1 | 1) => void;
}) {
  const switchKind = (kind: GbrainMappingRule["kind"]) => {
    if (kind === "retype") onChange({ kind, from_type: "", to_type: "", subtype_field: "subtype" });
    if (kind === "page_to_link") onChange({ kind, from_type: "", link_type: "", source_slug_from: "slug", target_slug_from: "body_first_link" });
    if (kind === "page_to_alias") onChange({ kind, from_type: "", canonical_from: "body_first_link", alias_slug_from: "slug" });
  };
  return (
    <div className="rounded-xl bg-black/[0.025] p-3">
      <div className="grid gap-3 md:grid-cols-2">
        <Field label="kind"><select className={inputClass} value={rule.kind} onChange={(event) => switchKind(event.target.value as GbrainMappingRule["kind"])}><option value="retype">retype</option><option value="page_to_link">page_to_link</option><option value="page_to_alias">page_to_alias</option></select></Field>
        <Field label="from_type"><input className={inputClass} value={rule.from_type} onChange={(event) => onChange({ ...rule, from_type: event.target.value })} /></Field>
        {rule.kind === "retype" ? <>
          <Field label="to_type"><input className={inputClass} value={rule.to_type} onChange={(event) => onChange({ ...rule, to_type: event.target.value })} /></Field>
          <Field label="subtype"><input className={inputClass} value={rule.subtype || ""} onChange={(event) => onChange({ ...rule, subtype: event.target.value || undefined })} /></Field>
          <Field label="subtype_field"><select className={inputClass} value={rule.subtype_field} onChange={(event) => onChange({ ...rule, subtype_field: event.target.value as GbrainSubtypeField })}>{subtypeFields.map((item) => <option key={item}>{item}</option>)}</select></Field>
          <Field label="path_filter"><input className={inputClass} value={rule.path_filter || ""} onChange={(event) => onChange({ ...rule, path_filter: event.target.value || undefined })} /></Field>
        </> : null}
        {rule.kind === "page_to_link" ? <>
          <Field label="link_type"><input className={inputClass} value={rule.link_type} onChange={(event) => onChange({ ...rule, link_type: event.target.value })} /></Field>
          <Field label="inverse"><input className={inputClass} value={rule.inverse || ""} onChange={(event) => onChange({ ...rule, inverse: event.target.value || undefined })} /></Field>
          <div className="md:col-span-2"><ResolverEditor label="source_slug_from" value={rule.source_slug_from} onChange={(value) => value && onChange({ ...rule, source_slug_from: value })} /></div>
          <div className="md:col-span-2"><ResolverEditor label="target_slug_from" value={rule.target_slug_from} onChange={(value) => value && onChange({ ...rule, target_slug_from: value })} /></div>
          <label className="inline-flex items-center gap-2 text-xs text-gray-600"><input type="checkbox" checked={rule.preserve_notes ?? false} onChange={(event) => onChange({ ...rule, preserve_notes: event.target.checked })} /> preserve_notes</label>
        </> : null}
        {rule.kind === "page_to_alias" ? <>
          <div className="md:col-span-2"><ResolverEditor label="canonical_from" value={rule.canonical_from} onChange={(value) => value && onChange({ ...rule, canonical_from: value })} /></div>
          <div className="md:col-span-2"><ResolverEditor label="alias_slug_from" value={rule.alias_slug_from} onChange={(value) => value && onChange({ ...rule, alias_slug_from: value })} /></div>
          <div className="md:col-span-2"><ResolverEditor label="notes_from" value={rule.notes_from} optional onChange={(value) => onChange({ ...rule, notes_from: value })} /></div>
        </> : null}
      </div>
      <div className="mt-2 flex justify-end gap-2"><ReorderButtons index={index} count={count} onMove={onMove} /><DeleteButton onClick={onDelete} /></div>
    </div>
  );
}

export default function AdvancedSchemaEditor({
  draft,
  onChange,
}: {
  draft: GbrainSchemaPackManifest;
  onChange: (draft: GbrainSchemaPackManifest) => void;
}) {
  const mappingRules = draft.mapping_rules || [];
  const calibration = draft.calibration_domains || [];
  return (
    <div className="space-y-4">
      <Section title="Borrow From" description="从其他官方 pack 选择性借用 page_types 或 link_types；空列表不会借用该类。" count={draft.borrow_from.length} onAdd={() => onChange({ ...draft, borrow_from: [...draft.borrow_from, { pack: "", types: [], link_types: [] }] })}>
        {draft.borrow_from.map((item, index) => <div key={index} className="grid gap-3 rounded-xl bg-black/[0.025] p-3 md:grid-cols-[1fr_1fr_1fr_auto]">
          <Field label="pack"><input className={inputClass} value={item.pack} onChange={(event) => onChange({ ...draft, borrow_from: draft.borrow_from.map((value, itemIndex) => itemIndex === index ? { ...value, pack: event.target.value } : value) })} /></Field>
          <Field label="types"><StringListEditor value={item.types || []} onChange={(types) => onChange({ ...draft, borrow_from: draft.borrow_from.map((value, itemIndex) => itemIndex === index ? { ...value, types } : value) })} /></Field>
          <Field label="link_types"><StringListEditor value={item.link_types || []} onChange={(link_types) => onChange({ ...draft, borrow_from: draft.borrow_from.map((value, itemIndex) => itemIndex === index ? { ...value, link_types } : value) })} /></Field>
          <div className="pt-5"><DeleteButton onClick={() => onChange({ ...draft, borrow_from: draft.borrow_from.filter((_, itemIndex) => itemIndex !== index) })} /></div>
        </div>)}
      </Section>

      <Section title="Enrichable Types" description="声明允许 enrich 的页面类型及可选 rubric。" count={draft.enrichable_types.length} onAdd={() => onChange({ ...draft, enrichable_types: [...draft.enrichable_types, { type: "" }] })}>
        {draft.enrichable_types.map((item, index) => <div key={index} className="grid gap-3 rounded-xl bg-black/[0.025] p-3 md:grid-cols-[1fr_2fr_auto]">
          <Field label="type"><input className={inputClass} value={item.type} onChange={(event) => onChange({ ...draft, enrichable_types: draft.enrichable_types.map((value, itemIndex) => itemIndex === index ? { ...value, type: event.target.value } : value) })} /></Field>
          <Field label="rubric"><input className={inputClass} value={item.rubric || ""} onChange={(event) => onChange({ ...draft, enrichable_types: draft.enrichable_types.map((value, itemIndex) => itemIndex === index ? { ...value, rubric: event.target.value || undefined } : value) })} /></Field>
          <div className="pt-5"><DeleteButton onClick={() => onChange({ ...draft, enrichable_types: draft.enrichable_types.filter((_, itemIndex) => itemIndex !== index) })} /></div>
        </div>)}
      </Section>

      <Section title="Filing Rules" description="定义 kind 到目录的归档规则。" count={draft.filing_rules.length} onAdd={() => onChange({ ...draft, filing_rules: [...draft.filing_rules, { kind: "", directory: "", examples: [] }] })}>
        {draft.filing_rules.map((item, index) => <div key={index} className="grid gap-3 rounded-xl bg-black/[0.025] p-3 md:grid-cols-2">
          <Field label="kind"><input className={inputClass} value={item.kind} onChange={(event) => onChange({ ...draft, filing_rules: draft.filing_rules.map((value, itemIndex) => itemIndex === index ? { ...value, kind: event.target.value } : value) })} /></Field>
          <Field label="directory"><input className={inputClass} value={item.directory} onChange={(event) => onChange({ ...draft, filing_rules: draft.filing_rules.map((value, itemIndex) => itemIndex === index ? { ...value, directory: event.target.value } : value) })} /></Field>
          <Field label="examples"><StringListEditor value={item.examples} onChange={(examples) => onChange({ ...draft, filing_rules: draft.filing_rules.map((value, itemIndex) => itemIndex === index ? { ...value, examples } : value) })} /></Field>
          <Field label="description"><input className={inputClass} value={item.description || ""} onChange={(event) => onChange({ ...draft, filing_rules: draft.filing_rules.map((value, itemIndex) => itemIndex === index ? { ...value, description: event.target.value || undefined } : value) })} /></Field>
          <div className="md:col-span-2 flex justify-end"><DeleteButton onClick={() => onChange({ ...draft, filing_rules: draft.filing_rules.filter((_, itemIndex) => itemIndex !== index) })} /></div>
        </div>)}
      </Section>

      <Section title="Phases" description="声明额外参与的 cycle phases；不改变 gbrain 核心 phases。">
        <Field label="phases"><StringListEditor value={draft.phases || []} onChange={(phases) => onChange({ ...draft, phases })} /></Field>
      </Section>

      <Section title="Calibration Domains" description="名称开放，aggregator 使用官方闭合枚举。" count={calibration.length} onAdd={() => onChange({ ...draft, calibration_domains: [...calibration, { name: "", aggregator: "scalar_brier", page_types: [] }] })}>
        {calibration.map((item, index) => <div key={index} className="grid gap-3 rounded-xl bg-black/[0.025] p-3 md:grid-cols-[1fr_1fr_1.5fr_auto]">
          <Field label="name"><input className={inputClass} value={item.name} onChange={(event) => onChange({ ...draft, calibration_domains: calibration.map((value, itemIndex) => itemIndex === index ? { ...value, name: event.target.value } : value) })} /></Field>
          <Field label="aggregator"><select className={inputClass} value={item.aggregator} onChange={(event) => onChange({ ...draft, calibration_domains: calibration.map((value, itemIndex) => itemIndex === index ? { ...value, aggregator: event.target.value as GbrainAggregator } : value) })}>{aggregators.map((value) => <option key={value}>{value}</option>)}</select></Field>
          <Field label="page_types"><StringListEditor value={item.page_types} onChange={(page_types) => onChange({ ...draft, calibration_domains: calibration.map((value, itemIndex) => itemIndex === index ? { ...value, page_types } : value) })} /></Field>
          <div className="pt-5"><DeleteButton onClick={() => onChange({ ...draft, calibration_domains: calibration.filter((_, itemIndex) => itemIndex !== index) })} /></div>
        </div>)}
      </Section>

      <Section title="Migration From" description="声明可升级来源 pack 与版本范围（例如 1.x、1.2.x 或精确 SemVer）。">
        <label className="inline-flex items-center gap-2 text-xs text-gray-600"><input type="checkbox" checked={Boolean(draft.migration_from)} onChange={(event) => onChange({ ...draft, migration_from: event.target.checked ? { pack: "", version: "" } : undefined })} /> 启用 migration_from</label>
        {draft.migration_from ? <div className="grid gap-3 md:grid-cols-2"><Field label="pack"><input className={inputClass} value={draft.migration_from.pack} onChange={(event) => onChange({ ...draft, migration_from: { ...draft.migration_from!, pack: event.target.value } })} /></Field><Field label="version"><input className={inputClass} value={draft.migration_from.version} onChange={(event) => onChange({ ...draft, migration_from: { ...draft.migration_from!, version: event.target.value } })} /></Field></div> : null}
      </Section>

      <Section title="Mapping Rules" description="官方 discriminated union：retype、page_to_link、page_to_alias。规则顺序有语义。" count={mappingRules.length} onAdd={() => onChange({ ...draft, mapping_rules: [...mappingRules, { kind: "retype", from_type: "", to_type: "", subtype_field: "subtype" }] })}>
        {mappingRules.map((rule, index) => <MappingRuleEditor key={index} rule={rule} index={index} count={mappingRules.length} onChange={(next) => onChange({ ...draft, mapping_rules: mappingRules.map((value, itemIndex) => itemIndex === index ? next : value) })} onMove={(delta) => { const target = index + delta; if (target < 0 || target >= mappingRules.length) return; const next = [...mappingRules]; [next[index], next[target]] = [next[target], next[index]]; onChange({ ...draft, mapping_rules: next }); }} onDelete={() => onChange({ ...draft, mapping_rules: mappingRules.filter((_, itemIndex) => itemIndex !== index) })} />)}
      </Section>
    </div>
  );
}
