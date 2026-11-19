"use client";

/**
 * DEV-ONLY GALLERY — not production data.
 *
 * Component showcase for the Atlas design system. The hardcoded arrays below
 * (MOCK_EXPENSES, TABLE_COLUMNS, NAV_ITEMS) are static design-time fixtures used
 * ONLY to render primitives in every visual state — they are NOT business data
 * and NEVER reach a production screen. This route 404s in a production build
 * (see the NODE_ENV guard in GalleryPage). P6-T1: excluded from the mock-zero
 * gate as an explicitly-annotated, prod-unreachable dev fixture.
 */

import * as React from "react";
import { notFound } from "next/navigation";
import { Plane, Receipt, FileText } from "lucide-react";
import {
  AtlasButton,
  StatusBadge,
  ProvenanceBadge,
  ConfidenceIndicator,
  CitationChip,
  AIReasoningPanel,
  KPICard,
  RecommendationCard,
  StatusCard,
  AIInsightCard,
  DataTable,
  FormField,
  Modal,
  Stepper,
  useAtlasToast,
  EmptyState,
  LoadingState,
  ErrorState,
} from "@/components/atlas";
import type { ColumnDef, SortDirection } from "@/components/atlas";

/* ================================================================
   ATLAS COMPONENT GALLERY
   Renders every primitive in every state. Static/mock data only.
   Route: /gallery — standalone, no shell, no auth.
   ================================================================ */

// ---- Gallery section wrapper ----
function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-20">
      <h2 className="atlas-text-heading text-[var(--color-primary-900)] mb-6 pb-2 border-b border-[var(--card-border-color)]">
        {title}
      </h2>
      {children}
    </section>
  );
}

function SubSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-8">
      <h3 className="atlas-text-title text-[#334155] mb-4">{title}</h3>
      {children}
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <span className="atlas-text-caption text-[#94A3B8] block mb-1">
      {children}
    </span>
  );
}

// ---- Color swatch ----
function Swatch({
  name,
  color,
  textColor = "white",
}: {
  name: string;
  color: string;
  textColor?: string;
}) {
  return (
    <div className="text-center">
      <div
        className="h-14 w-full rounded-atlas-md shadow-atlas-1 flex items-center justify-center mb-1"
        style={{ backgroundColor: color, color: textColor }}
      >
        <span className="text-[10px] font-mono opacity-80">{color}</span>
      </div>
      <span className="atlas-text-caption text-[#64748B]">{name}</span>
    </div>
  );
}

// ---- Mock table data ----
interface ExpenseRow {
  id: string;
  report: string;
  amount: string;
  status: string;
  policy: string;
  submitted: string;
}

// DEV-ONLY GALLERY — not production data. Static design-time fixture for the
// DataTable showcase below; never rendered on a product screen.
const MOCK_EXPENSES: ExpenseRow[] = [
  {
    id: "1",
    report: "NYC Conference",
    amount: "$3,247",
    status: "Submitted",
    policy: "In policy",
    submitted: "Jun 15",
  },
  {
    id: "2",
    report: "London Meeting",
    amount: "$5,891",
    status: "Draft",
    policy: "2 issues",
    submitted: "—",
  },
  {
    id: "3",
    report: "Q2 Team Dinner",
    amount: "$487",
    status: "Approved",
    policy: "In policy",
    submitted: "Jun 10",
  },
  {
    id: "4",
    report: "Client Visit SF",
    amount: "$1,200",
    status: "Submitted",
    policy: "In policy",
    submitted: "Jun 12",
  },
  {
    id: "5",
    report: "Training Retreat",
    amount: "$2,100",
    status: "Rejected",
    policy: "Out of policy",
    submitted: "Jun 8",
  },
];

const TABLE_COLUMNS: ColumnDef<ExpenseRow>[] = [
  {
    key: "report",
    header: "Report Name",
    sortable: true,
    filterable: true,
    render: (row) => <span className="font-medium">{row.report}</span>,
  },
  {
    key: "amount",
    header: "Amount",
    sortable: true,
    render: (row) => row.amount,
    width: "w-[100px]",
  },
  {
    key: "status",
    header: "Status",
    sortable: true,
    filterable: true,
    render: (row) => {
      const v =
        row.status === "Approved"
          ? "approved"
          : row.status === "Rejected"
            ? "rejected"
            : "pending";
      return <StatusBadge variant={v as "approved" | "rejected" | "pending"} />;
    },
  },
  {
    key: "policy",
    header: "Policy",
    render: (row) => {
      const v = row.policy === "In policy" ? "in-policy" : "out-of-policy";
      return <StatusBadge variant={v as "in-policy" | "out-of-policy"} />;
    },
  },
  {
    key: "submitted",
    header: "Submitted",
    render: (row) => <span className="text-[#64748B]">{row.submitted}</span>,
    width: "w-[100px]",
  },
];

// ---- NAV ITEMS ----
const NAV_ITEMS = [
  { id: "tokens", label: "Design Tokens" },
  { id: "buttons", label: "Buttons" },
  { id: "cards", label: "Cards" },
  { id: "badges", label: "Badges & Chips" },
  { id: "trust", label: "Trust Components" },
  { id: "tables", label: "Tables" },
  { id: "forms", label: "Forms" },
  { id: "modals", label: "Modals" },
  { id: "steppers", label: "Steppers" },
  { id: "toasts", label: "Toasts" },
  { id: "ui-states", label: "Shared UI States" },
];

// ================================================================
// GALLERY PAGE
// ================================================================
export default function GalleryPage() {
  // DEV-ONLY: this design-system showcase is not part of the product. In a
  // production build NODE_ENV is statically "production", so this route 404s
  // and the dev-only fixtures below never ship to users.
  if (process.env.NODE_ENV === "production") {
    notFound();
  }

  const { showToast } = useAtlasToast();

  // Modal state
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [destructiveOpen, setDestructiveOpen] = React.useState(false);
  const [reasonOpen, setReasonOpen] = React.useState(false);

  // Table state
  const [sortColumn, setSortColumn] = React.useState<string | undefined>();
  const [sortDirection, setSortDirection] = React.useState<SortDirection>(null);
  const [selectedKeys, setSelectedKeys] = React.useState<Set<string>>(
    new Set(),
  );
  const [tablePage, setTablePage] = React.useState(1);

  // Form state
  const [formValue, setFormValue] = React.useState("Hilton Garden");
  const [formValue2, setFormValue2] = React.useState("$247.50");
  const [formValue3, setFormValue3] = React.useState("");

  return (
    <div className="flex min-h-screen">
      {/* Sticky sidebar TOC */}
      <nav className="hidden lg:block w-56 shrink-0 border-r border-[var(--card-border-color)] bg-[var(--surface-card)] sticky top-0 h-screen overflow-y-auto p-4">
        <h1 className="atlas-text-title text-[var(--color-primary-900)] mb-4">
          Atlas Gallery
        </h1>
        <ul className="space-y-1">
          {NAV_ITEMS.map((item) => (
            <li key={item.id}>
              <a
                href={`#${item.id}`}
                className="block px-3 py-2 rounded-atlas-md atlas-text-body text-[#64748B] hover:bg-[var(--color-primary-50)] hover:text-[var(--color-primary-600)] transition-colors duration-atlas-fast"
              >
                {item.label}
              </a>
            </li>
          ))}
        </ul>
      </nav>

      {/* Main content */}
      <main className="flex-1 max-w-5xl mx-auto px-6 py-8 space-y-16">
        {/* Header */}
        <div>
          <h1 className="atlas-text-display text-[var(--color-primary-900)]">
            Atlas Design System
          </h1>
          <p className="atlas-text-body text-[#64748B] mt-2">
            P5-T1 Component Gallery — Every primitive in every state.
          </p>
        </div>

        {/* ============================================ */}
        {/* 1. DESIGN TOKENS */}
        {/* ============================================ */}
        <Section id="tokens" title="Design Tokens">
          <SubSection title="Primary Palette — Indigo">
            <div className="grid grid-cols-4 md:grid-cols-8 gap-3">
              <Swatch name="50" color="#EEF2FF" textColor="#312E81" />
              <Swatch name="100" color="#E0E7FF" textColor="#312E81" />
              <Swatch name="200" color="#C7D2FE" textColor="#312E81" />
              <Swatch name="400" color="#818CF8" />
              <Swatch name="500" color="#6366F1" />
              <Swatch name="600" color="#4F46E5" />
              <Swatch name="700" color="#4338CA" />
              <Swatch name="900" color="#312E81" />
            </div>
          </SubSection>

          <SubSection title="Semantic Colors">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Swatch name="Success" color="#059669" />
              <Swatch name="Warning" color="#D97706" />
              <Swatch name="Error" color="#DC2626" />
              <Swatch name="Info" color="#2563EB" />
            </div>
          </SubSection>

          <SubSection title="AI Colors">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Swatch name="AI Primary" color="#7C3AED" />
              <Swatch name="AI Surface" color="#F5F3FF" textColor="#7C3AED" />
              <Swatch name="Gradient Start" color="#7C3AED" />
              <Swatch name="Gradient End" color="#4F46E5" />
            </div>
          </SubSection>

          <SubSection title="Surface Colors">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Swatch name="Background" color="#F8FAFC" textColor="#334155" />
              <Swatch name="Card" color="#FFFFFF" textColor="#334155" />
              <Swatch name="Sidebar" color="#0F172A" />
              <Swatch name="Table Header" color="#F8FAFC" textColor="#334155" />
            </div>
          </SubSection>

          <SubSection title="Typography Scale">
            <div className="space-y-3 bg-[var(--surface-card)] p-6 rounded-atlas-md border border-[var(--card-border-color)]">
              <p className="atlas-text-display">Display — 32px / 600</p>
              <p className="atlas-text-heading">Heading — 24px / 600</p>
              <p className="atlas-text-title">Title — 20px / 500</p>
              <p className="atlas-text-subtitle">Subtitle — 16px / 500</p>
              <p className="atlas-text-body">Body — 14px / 400</p>
              <p className="atlas-text-caption text-[#64748B]">
                Caption — 12px / 400
              </p>
              <p className="atlas-text-overline text-[#64748B]">
                Overline — 11px / 600 / Uppercase
              </p>
            </div>
          </SubSection>

          <SubSection title="Spacing Scale">
            <div className="space-y-2 bg-[var(--surface-card)] p-6 rounded-atlas-md border border-[var(--card-border-color)]">
              {[
                { name: "space-1", value: "4px" },
                { name: "space-2", value: "8px" },
                { name: "space-3", value: "12px" },
                { name: "space-4", value: "16px" },
                { name: "space-6", value: "24px" },
                { name: "space-8", value: "32px" },
                { name: "space-12", value: "48px" },
                { name: "space-16", value: "64px" },
              ].map(({ name, value }) => (
                <div key={name} className="flex items-center gap-3">
                  <span className="atlas-text-caption text-[#64748B] w-20">
                    {name}
                  </span>
                  <div
                    className="h-4 rounded-sm bg-[var(--color-primary-400)]"
                    style={{ width: value }}
                  />
                  <span className="atlas-text-caption text-[#94A3B8]">
                    {value}
                  </span>
                </div>
              ))}
            </div>
          </SubSection>

          <SubSection title="Elevation Levels">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
              {[0, 1, 2, 3].map((level) => (
                <div
                  key={level}
                  className="h-24 rounded-atlas-md bg-[var(--surface-card)] flex items-center justify-center"
                  style={{ boxShadow: `var(--shadow-${level})` }}
                >
                  <span className="atlas-text-body text-[#64748B]">
                    Level {level}
                  </span>
                </div>
              ))}
            </div>
          </SubSection>

          <SubSection title="Border Radius">
            <div className="flex flex-wrap gap-6">
              {[
                { name: "sm", value: "4px" },
                { name: "md", value: "8px" },
                { name: "lg", value: "12px" },
                { name: "xl", value: "16px" },
                { name: "full", value: "9999px" },
              ].map(({ name, value }) => (
                <div key={name} className="text-center">
                  <div
                    className="h-16 w-16 bg-[var(--color-primary-100)] border-2 border-[var(--color-primary-400)] mb-1"
                    style={{ borderRadius: value }}
                  />
                  <span className="atlas-text-caption text-[#64748B]">
                    {name}
                  </span>
                </div>
              ))}
            </div>
          </SubSection>
        </Section>

        {/* ============================================ */}
        {/* 2. BUTTONS */}
        {/* ============================================ */}
        <Section id="buttons" title="Buttons">
          <SubSection title="Variants × Sizes">
            <div className="space-y-4">
              {(["primary", "secondary", "destructive", "ghost"] as const).map(
                (variant) => (
                  <div key={variant} className="space-y-2">
                    <Label>{variant}</Label>
                    <div className="flex flex-wrap items-center gap-3">
                      <AtlasButton variant={variant} size="sm">
                        Small
                      </AtlasButton>
                      <AtlasButton variant={variant} size="md">
                        Medium
                      </AtlasButton>
                      <AtlasButton variant={variant} size="lg">
                        Large
                      </AtlasButton>
                    </div>
                  </div>
                ),
              )}
            </div>
          </SubSection>

          <SubSection title="Loading State">
            <div className="flex flex-wrap items-center gap-3">
              <AtlasButton variant="primary" loading>
                Submitting…
              </AtlasButton>
              <AtlasButton variant="secondary" loading>
                Processing…
              </AtlasButton>
              <AtlasButton variant="destructive" loading>
                Deleting…
              </AtlasButton>
            </div>
          </SubSection>

          <SubSection title="Disabled State">
            <div className="flex flex-wrap items-center gap-3">
              <AtlasButton variant="primary" disabled>
                Disabled
              </AtlasButton>
              <AtlasButton
                variant="primary"
                disabled
                disabledReason="Resolve issues to submit"
              >
                Hover for reason
              </AtlasButton>
              <AtlasButton
                variant="secondary"
                disabled
                disabledReason="Missing required fields"
              >
                Disabled secondary
              </AtlasButton>
            </div>
          </SubSection>

          <SubSection title="With Icons">
            <div className="flex flex-wrap items-center gap-3">
              <AtlasButton variant="primary" size="md">
                <Plane className="h-4 w-4" /> Book Flight
              </AtlasButton>
              <AtlasButton variant="secondary" size="md">
                <Receipt className="h-4 w-4" /> Upload Receipt
              </AtlasButton>
              <AtlasButton variant="ghost" size="md">
                <FileText className="h-4 w-4" /> View Report
              </AtlasButton>
            </div>
          </SubSection>
        </Section>

        {/* ============================================ */}
        {/* 3. CARDS */}
        {/* ============================================ */}
        <Section id="cards" title="Cards">
          <SubSection title="KPI Cards">
            <div className="grid gap-4 md:grid-cols-4">
              <KPICard
                label="Upcoming Trips"
                value="2"
                trend="+1 this week"
                trendDirection="up"
                accentColor="primary"
              />
              <KPICard
                label="Pending Approvals"
                value="3"
                trend="2 urgent"
                trendDirection="up"
                accentColor="warning"
              />
              <KPICard
                label="Expense Status"
                value="$2,847"
                trend="▲ 12% vs last month"
                trendDirection="up"
                accentColor="success"
                sparklineData={[20, 35, 25, 40, 50, 45, 60, 55, 70, 65, 80]}
              />
              <KPICard
                label="Receipt Processing"
                value="5 pending"
                trend="3 auto-processed"
                trendDirection="flat"
                accentColor="info"
              />
            </div>
          </SubSection>

          <SubSection title="KPI Card — Loading">
            <div className="grid gap-4 md:grid-cols-3">
              <KPICard label="" value="" loading accentColor="primary" />
              <KPICard label="" value="" loading accentColor="success" />
              <KPICard label="" value="" loading accentColor="warning" />
            </div>
          </SubSection>

          <SubSection title="Recommendation Card (AI)">
            <div className="grid gap-4 md:grid-cols-2">
              <RecommendationCard
                title="Delta DL 487 · JFK → SFO"
                rationale="Best value within policy — saves $213 vs next option"
                source="Atlas Inventory"
                policyClause="§3.2 cabin class"
                inPolicy
                onSelect={() =>
                  showToast({ variant: "success", title: "Flight selected" })
                }
              />
              <RecommendationCard
                title="Hilton Midtown · $189/night"
                rationale="Preferred supplier, within per-diem limit"
                source="Atlas Inventory"
                policyClause="§4.1 lodging"
                inPolicy
                onSelect={() =>
                  showToast({ variant: "success", title: "Hotel selected" })
                }
              />
            </div>
          </SubSection>

          <SubSection title="Recommendation Card — Out of Policy">
            <div className="max-w-md">
              <RecommendationCard
                title="Business Class · UA 234"
                rationale="Exceeds cabin class policy for this route"
                source="Atlas Inventory"
                policyClause="§3.2 cabin class"
                inPolicy={false}
              />
            </div>
          </SubSection>

          <SubSection title="Status Card (Entity)">
            <div className="grid gap-4 md:grid-cols-3">
              <StatusCard
                title="Trip: NYC Tech Conference"
                subtitle="Jun 20–23 · $3,247"
                statusColor="success"
                status="approved"
                policyStatus="in-policy"
              />
              <StatusCard
                title="Trip: London Meeting"
                subtitle="Jul 5–8 · $5,891"
                statusColor="warning"
                status="pending"
                policyStatus="out-of-policy"
                onClick={() => {}}
              />
              <StatusCard
                title="Report: Q2 Dinner"
                subtitle="$487"
                statusColor="error"
                status="rejected"
              />
            </div>
          </SubSection>

          <SubSection title="AI Insight Card">
            <div className="max-w-lg">
              <AIInsightCard
                body="Within policy with one exception: meal per diem exceeded by $23."
                citation="Travel Policy §4.1"
                confidence={94}
                reasoning={[
                  "Checked policy limits → within bounds for airfare and lodging",
                  "Compared meal expenses against per-diem → $98 exceeds $75 limit by $23",
                  "Verified preferred-supplier alignment → matches profile preferences",
                ]}
              />
            </div>
          </SubSection>
        </Section>

        {/* ============================================ */}
        {/* 4. BADGES & CHIPS */}
        {/* ============================================ */}
        <Section id="badges" title="Badges & Chips">
          <SubSection title="Status Badges">
            <div className="flex flex-wrap items-center gap-3">
              <StatusBadge variant="in-policy" />
              <StatusBadge variant="out-of-policy" />
              <StatusBadge variant="pending" />
              <StatusBadge variant="approved" />
              <StatusBadge variant="rejected" />
            </div>
          </SubSection>

          <SubSection title="Provenance Badges">
            <div className="flex flex-wrap items-center gap-3">
              <ProvenanceBadge type="deterministic" />
              <ProvenanceBadge type="ai_inferred" />
              <ProvenanceBadge type="user_edited" />
            </div>
          </SubSection>
        </Section>

        {/* ============================================ */}
        {/* 5. TRUST COMPONENTS */}
        {/* ============================================ */}
        <Section id="trust" title="Trust Components">
          <SubSection title="Confidence Indicator">
            <div className="space-y-3">
              <div>
                <Label>High (≥90%)</Label>
                <ConfidenceIndicator value={94} />
              </div>
              <div>
                <Label>Medium (70–89%)</Label>
                <ConfidenceIndicator value={76} />
              </div>
              <div>
                <Label>Low (&lt;70%)</Label>
                <ConfidenceIndicator value={42} />
              </div>
              <div>
                <Label>Without label</Label>
                <ConfidenceIndicator value={88} showLabel={false} />
              </div>
            </div>
          </SubSection>

          <SubSection title="Citation Chip">
            <div className="flex flex-wrap gap-3">
              <CitationChip
                clauseRef="Travel Policy §4.1"
                onClick={() =>
                  showToast({
                    variant: "info",
                    title: "Citation clicked: §4.1",
                  })
                }
              />
              <CitationChip clauseRef="Expense Policy §2.3" />
              <CitationChip clauseRef="Lodging Guidelines §5.0" />
            </div>
          </SubSection>

          <SubSection title="AI Reasoning Panel — Collapsed">
            <div className="max-w-lg">
              <AIReasoningPanel
                steps={[
                  "Checked policy limits → within bounds",
                  "Compared 4 inventory options → selected lowest in-policy fare",
                  "Verified preferred-supplier alignment → matches profile",
                ]}
              />
            </div>
          </SubSection>

          <SubSection title="AI Reasoning Panel — Expanded">
            <div className="max-w-lg">
              <AIReasoningPanel
                title="Why this assessment?"
                steps={[
                  "Retrieved relevant policy clauses (§3.2, §4.1)",
                  "Evaluated airfare $487 against cap $800 → within bounds",
                  "Evaluated lodging $189/nt against per-diem $200 → within bounds",
                  "Flagged meal $98 against per-diem $75 → exceeded by $23",
                  "Overall: compliant with one exception",
                ]}
                defaultExpanded
              />
            </div>
          </SubSection>
        </Section>

        {/* ============================================ */}
        {/* 6. TABLES */}
        {/* ============================================ */}
        <Section id="tables" title="Tables">
          <SubSection title="Default — Sortable, Selectable, Expandable">
            <DataTable
              columns={TABLE_COLUMNS}
              data={MOCK_EXPENSES}
              getRowKey={(row) => row.id}
              selectable
              selectedKeys={selectedKeys}
              onSelectionChange={setSelectedKeys}
              expandable
              renderExpanded={(row) => (
                <div className="space-y-2">
                  <p className="atlas-text-body text-[#334155]">
                    <strong>Lineage:</strong> {row.report} → receipt → expense
                    entry
                  </p>
                  <ProvenanceBadge type="deterministic" />
                </div>
              )}
              sortColumn={sortColumn}
              sortDirection={sortDirection}
              onSort={(col, dir) => {
                setSortColumn(col);
                setSortDirection(dir);
              }}
              pagination={{ page: tablePage, pageSize: 10, total: 47 }}
              onPageChange={setTablePage}
            />
          </SubSection>

          <SubSection title="Loading State">
            <DataTable
              columns={TABLE_COLUMNS}
              data={[]}
              getRowKey={(row) => row.id}
              state="loading"
            />
          </SubSection>

          <SubSection title="Empty State">
            <DataTable
              columns={TABLE_COLUMNS}
              data={[]}
              getRowKey={(row) => row.id}
              state="empty"
              emptyState={{
                heading: "No expenses found",
                subtext:
                  "Start by uploading a receipt or creating an expense report.",
                ctaLabel: "Create Report",
                onCta: () =>
                  showToast({
                    variant: "info",
                    title: "Create report clicked",
                  }),
              }}
            />
          </SubSection>

          <SubSection title="Error State">
            <DataTable
              columns={TABLE_COLUMNS}
              data={[]}
              getRowKey={(row) => row.id}
              state="error"
              errorState={{
                heading: "Unable to load expenses",
                subtext: "We're having trouble connecting. Your data is safe.",
                onRetry: () =>
                  showToast({ variant: "info", title: "Retry clicked" }),
              }}
            />
          </SubSection>
        </Section>

        {/* ============================================ */}
        {/* 7. FORMS */}
        {/* ============================================ */}
        <Section id="forms" title="Forms">
          <SubSection title="AI-Filled Fields with Provenance">
            <div className="max-w-md space-y-4">
              <FormField
                id="merchant"
                label="Merchant"
                value={formValue}
                onChange={setFormValue}
                provenance="deterministic"
                confidence={98}
              />
              <FormField
                id="amount"
                label="Amount"
                value={formValue2}
                onChange={setFormValue2}
                provenance="ai_inferred"
                confidence={87}
                helperText="AI inferred from receipt context"
              />
              <FormField
                id="category"
                label="Category"
                value="Lodging"
                provenance="ai_inferred"
                confidence={72}
                disabled
              />
            </div>
          </SubSection>

          <SubSection title="Validation Error">
            <div className="max-w-md">
              <FormField
                id="email"
                label="Email"
                value={formValue3}
                onChange={setFormValue3}
                error="Please enter a valid email address"
                required
                placeholder="user@example.com"
              />
            </div>
          </SubSection>

          <SubSection title="Default Field with Helper">
            <div className="max-w-md">
              <FormField
                id="notes"
                label="Notes"
                value=""
                placeholder="Add any notes about this expense..."
                helperText="Optional. Notes are visible to your approver."
              />
            </div>
          </SubSection>
        </Section>

        {/* ============================================ */}
        {/* 8. MODALS */}
        {/* ============================================ */}
        <Section id="modals" title="Modals">
          <div className="flex flex-wrap gap-3">
            <AtlasButton variant="primary" onClick={() => setConfirmOpen(true)}>
              Open Confirm Modal
            </AtlasButton>
            <AtlasButton
              variant="destructive"
              onClick={() => setDestructiveOpen(true)}
            >
              Open Destructive Modal
            </AtlasButton>
            <AtlasButton
              variant="secondary"
              onClick={() => setReasonOpen(true)}
            >
              Open Reason Modal
            </AtlasButton>
          </div>

          <Modal
            variant="confirm"
            title="Submit Report"
            description="Are you sure you want to submit this expense report? It will be sent to your manager for approval."
            open={confirmOpen}
            onOpenChange={setConfirmOpen}
            onConfirm={() => {
              setConfirmOpen(false);
              showToast({ variant: "success", title: "Report submitted" });
            }}
          />

          <Modal
            variant="destructive"
            title="Delete Report"
            description="This action cannot be undone. The report and all associated entries will be permanently deleted."
            confirmLabel="Delete Report"
            open={destructiveOpen}
            onOpenChange={setDestructiveOpen}
            onConfirm={() => {
              setDestructiveOpen(false);
              showToast({ variant: "error", title: "Report deleted" });
            }}
          />

          <Modal
            variant="reason"
            title="Reject Report"
            description="Please provide a reason for rejecting this expense report. The employee will see this message."
            confirmLabel="Reject"
            open={reasonOpen}
            onOpenChange={setReasonOpen}
            onConfirm={(reason) => {
              setReasonOpen(false);
              showToast({
                variant: "info",
                title: `Rejected with reason: ${reason}`,
              });
            }}
          />
        </Section>

        {/* ============================================ */}
        {/* 9. STEPPERS */}
        {/* ============================================ */}
        <Section id="steppers" title="Steppers">
          <SubSection title="Step 1 — Request (Active)">
            <Stepper
              steps={[
                "Request",
                "Recommendation",
                "Selection",
                "Approval",
                "Confirmation",
              ]}
              currentStep={0}
            />
          </SubSection>

          <SubSection title="Step 3 — Selection (Partially Complete)">
            <Stepper
              steps={[
                "Request",
                "Recommendation",
                "Selection",
                "Approval",
                "Confirmation",
              ]}
              currentStep={2}
            />
          </SubSection>

          <SubSection title="Step 5 — All Complete">
            <Stepper
              steps={[
                "Request",
                "Recommendation",
                "Selection",
                "Approval",
                "Confirmation",
              ]}
              currentStep={4}
              completedSteps={[0, 1, 2, 3, 4]}
            />
          </SubSection>
        </Section>

        {/* ============================================ */}
        {/* 10. TOASTS */}
        {/* ============================================ */}
        <Section id="toasts" title="Toasts">
          <div className="flex flex-wrap gap-3">
            <AtlasButton
              variant="primary"
              onClick={() =>
                showToast({
                  variant: "success",
                  title: "Report submitted successfully",
                  description: "Your manager has been notified.",
                })
              }
            >
              Success Toast
            </AtlasButton>
            <AtlasButton
              variant="destructive"
              onClick={() =>
                showToast({
                  variant: "error",
                  title: "Failed to submit report",
                  description: "Please try again.",
                })
              }
            >
              Error Toast
            </AtlasButton>
            <AtlasButton
              variant="secondary"
              onClick={() =>
                showToast({
                  variant: "info",
                  title: "Processing receipt...",
                  description: "This may take a few seconds.",
                })
              }
            >
              Info Toast
            </AtlasButton>
          </div>
        </Section>

        {/* ============================================ */}
        {/* 11. SHARED UI STATES */}
        {/* ============================================ */}
        <Section id="ui-states" title="Shared UI States">
          <SubSection title="Empty State">
            <div className="border border-[var(--card-border-color)] rounded-atlas-md bg-[var(--surface-card)]">
              <EmptyState
                icon={Plane}
                heading="No upcoming trips"
                subtext="Plan your next trip with AI assistance"
                ctaLabel="Start Planning"
                onCta={() =>
                  showToast({
                    variant: "info",
                    title: "Start planning clicked",
                  })
                }
              />
            </div>
          </SubSection>

          <SubSection title="Loading State — Cards">
            <LoadingState variant="card" rows={3} />
          </SubSection>

          <SubSection title="Loading State — Table">
            <LoadingState variant="table" rows={5} />
          </SubSection>

          <SubSection title="Loading State — Form">
            <LoadingState variant="form" rows={4} />
          </SubSection>

          <SubSection title="Error State">
            <div className="border border-[var(--card-border-color)] rounded-atlas-md bg-[var(--surface-card)]">
              <ErrorState
                heading="Unable to load expenses"
                subtext="We're having trouble connecting. Your data is safe."
                onRetry={() =>
                  showToast({ variant: "info", title: "Retry clicked" })
                }
                onSecondary={() =>
                  showToast({
                    variant: "info",
                    title: "Contact Support clicked",
                  })
                }
              />
            </div>
          </SubSection>
        </Section>

        {/* Footer */}
        <div className="border-t border-[var(--card-border-color)] pt-8 pb-16">
          <p className="atlas-text-caption text-[#94A3B8] text-center">
            Atlas Design System · P5-T1 Component Gallery · All primitives
            rendered with static/mock data
          </p>
        </div>
      </main>
    </div>
  );
}
