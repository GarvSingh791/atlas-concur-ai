/**
 * Atlas Design System — Barrel Export
 *
 * All shared primitives for the Atlas component library.
 * Import from "@/components/atlas" in downstream screens.
 */

// Buttons
export { AtlasButton, atlasButtonVariants } from "./button";
export type { AtlasButtonProps } from "./button";

// Badges
export {
  StatusBadge,
  humanizeStatus,
  isStatusBadgeVariant,
} from "./status-badge";
export type { StatusBadgeVariant, StatusBadgeProps } from "./status-badge";

// Trust components
export { ProvenanceBadge } from "./provenance-badge";
export type { ProvenanceType, ProvenanceBadgeProps } from "./provenance-badge";

export { ConfidenceIndicator } from "./confidence-indicator";
export type { ConfidenceIndicatorProps } from "./confidence-indicator";

export { CitationChip } from "./citation-chip";
export type { CitationChipProps } from "./citation-chip";

export { AIReasoningPanel } from "./ai-reasoning-panel";
export type { AIReasoningPanelProps } from "./ai-reasoning-panel";

// Cards
export { KPICard } from "./kpi-card";
export type { KPICardProps, TrendDirection, AccentColor } from "./kpi-card";

export { RecommendationCard } from "./recommendation-card";
export type { RecommendationCardProps } from "./recommendation-card";

export { StatusCard } from "./status-card";
export type { StatusCardProps } from "./status-card";

export { AIInsightCard } from "./ai-insight-card";
export type { AIInsightCardProps } from "./ai-insight-card";

// Tables
export { DataTable } from "./data-table";
export type { DataTableProps, ColumnDef, SortDirection } from "./data-table";

// Forms
export { FormField } from "./form-field";
export type { FormFieldProps } from "./form-field";

// Modals
export { Modal } from "./modal";
export type { ModalProps, ModalVariant } from "./modal";

// Steppers
export { Stepper } from "./stepper";
export type { StepperProps } from "./stepper";

// Toasts
export { useAtlasToast } from "./atlas-toast";
export type { AtlasToastOptions, ToastVariant } from "./atlas-toast";

// Shared UI states
export { EmptyState } from "./empty-state";
export type { EmptyStateProps } from "./empty-state";

export { LoadingState } from "./loading-state";
export type { LoadingStateProps, LoadingVariant } from "./loading-state";

export { ErrorState } from "./error-state";
export type { ErrorStateProps } from "./error-state";

// AI Assistant (P5-T10)
export { AssistantPanel } from "./assistant/assistant-panel";
export type { AssistantPanelProps } from "./assistant/assistant-panel";

export { AssistantTurn } from "./assistant/assistant-turn";
export type { AssistantTurnProps } from "./assistant/assistant-turn";

export { AssistantInput } from "./assistant/assistant-input";
export type { AssistantInputProps } from "./assistant/assistant-input";

export { ToolActivityIndicator } from "./assistant/tool-activity-indicator";
export type { ToolActivityIndicatorProps } from "./assistant/tool-activity-indicator";
