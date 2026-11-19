"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import type { Conversation, ConversationDetail } from "@/lib/types";

export const conversationQueryKeys = {
  conversation: (id: string) => ["conversations", id] as const,
};

export function useConversation(id: string | null) {
  return useQuery<ConversationDetail>({
    queryKey: conversationQueryKeys.conversation(id ?? ""),
    queryFn: () => api.get<ConversationDetail>(`/conversations/${id}`),
    enabled: !!id,
  });
}

export function useCreateConversation() {
  const qc = useQueryClient();
  return useMutation<
    Conversation,
    Error,
    { context_type: "travel_planning" | "policy_qa"; context_ref_id?: string }
  >({
    mutationFn: (body) => api.post<Conversation>("/conversations", { body }),
    onSuccess: (conv) => {
      qc.setQueryData(conversationQueryKeys.conversation(conv.id), conv);
    },
  });
}
