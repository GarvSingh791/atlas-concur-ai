export type ConversationContextType =
  | "travel_planning"
  | "expense"
  | "policy_qa"
  | "general";

export interface Conversation {
  id: string;
  context_type: ConversationContextType;
  context_ref_id: string | null;
  created_at: string;
}

export interface Citation {
  kind: string;
  ref: string;
  detail?: string;
  source?: string;
  /** Backend-emitted document class: "policy"|"faq"|"howto"|"reference"|null.
   *  Drives the document-qualified chip prefix (R6-T2). */
  document_type?: string;
}

/** Message author — mirrors database/models/conversation_message.py MessageRole.
 *  In practice only user/assistant are returned, but the backend enum also
 *  permits system/tool; consumers filter to user|assistant where needed. */
export type MessageRole = "user" | "assistant" | "system" | "tool";

/** Persisted assistant-message metadata (backend ai/service.append_policy_reply). */
export interface PersistedAnswerMeta {
  citations: Citation[];
  answer_type?: "grounded" | "silent" | "needs_review" | null;
  needs_review?: boolean;
  confidence?: number | null;
  confidence_source?: string | null;
  answer_structured?: Record<string, unknown> | null;
}

export interface ConversationMessage {
  id: string;
  role: MessageRole;
  content: string;
  tool_calls: Record<string, unknown> | null;
  /** Persisted citation list. Router-persisted policy replies (C7-09:
   *  policy_qa / general) also carry the answer metadata alongside it so a
   *  reloaded turn can render the same trust surface. */
  citations: PersistedAnswerMeta | null;
  sequence: number;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: ConversationMessage[];
}

export interface MessageCreate {
  content: string;
  payload?: Record<string, unknown>;
  explain_with_llm?: boolean;
}
