"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  CheckCircle,
  CreditCard,
  FileText,
  Loader2,
  Plane,
  Receipt,
  Search,
  ShieldCheck,
} from "lucide-react";

import { humanizeStatus } from "@/components/atlas/status-badge";
import { useSession } from "@/lib/auth/session-provider";
import { useDebouncedValue } from "@/lib/hooks/use-debounced-value";
import {
  MIN_SEARCH_LENGTH,
  normaliseSearchQuery,
  useGlobalSearch,
} from "@/lib/query/search-hooks";
import { resolveLink } from "@/lib/shell/links";
import type { SearchGroupKey, SearchResult } from "@/lib/types/shell";
import { cn } from "@/lib/utils";

/**
 * Global search — product gap 3 (replaces the inert "Search Atlas…" box).
 *
 * Command-style combobox over GET /search:
 *   - "/" anywhere (outside a text field) focuses it;
 *   - typing is debounced 250 ms and NEVER queries below 2 characters;
 *   - ArrowUp / ArrowDown move through navigable results, Enter opens the
 *     active one, Escape closes the list (a second Escape blurs);
 *   - grouped results with loading / empty / error states;
 *   - each result opens its subject only on a page the session may open
 *     (resolveLink → canonical link, else the approval fallback, else the row
 *     is shown but not navigable). A manager opening a teammate's report lands
 *     on the read-only report view.
 */

export const SEARCH_DEBOUNCE_MS = 250;

const GROUP_ICON: Record<SearchGroupKey, typeof Search> = {
  trips: Plane,
  reports: FileText,
  entries: CreditCard,
  receipts: Receipt,
  approvals: CheckCircle,
  policy_rules: ShieldCheck,
};

interface FlatResult {
  domId: string;
  result: SearchResult;
  href: string | null;
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  );
}

export function GlobalSearch() {
  const router = useRouter();
  const { permissions, role } = useSession();
  const [value, setValue] = React.useState("");
  const [open, setOpen] = React.useState(false);
  const [activeIndex, setActiveIndex] = React.useState(-1);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const rootRef = React.useRef<HTMLDivElement>(null);
  const listboxId = React.useId();

  const debounced = useDebouncedValue(value, SEARCH_DEBOUNCE_MS);
  const query = normaliseSearchQuery(debounced);
  const typed = normaliseSearchQuery(value);
  const search = useGlobalSearch(debounced);
  const ready = query.length >= MIN_SEARCH_LENGTH;
  // While the user is still typing (debounce pending) the shown data is stale.
  const pending = typed !== query && typed.length >= MIN_SEARCH_LENGTH;

  const groups = React.useMemo(
    () => (ready ? (search.data?.groups ?? []) : []),
    [ready, search.data],
  );

  const flat = React.useMemo<FlatResult[]>(() => {
    const out: FlatResult[] = [];
    for (const g of groups) {
      for (const r of g.items) {
        out.push({
          domId: `${listboxId}-${g.key}-${r.id}`,
          result: r,
          href: resolveLink(r.link, r.fallback_link, permissions, role),
        });
      }
    }
    return out;
  }, [groups, listboxId, permissions, role]);

  const navigable = flat.filter((f) => f.href !== null);

  // Reset the highlight whenever the result set changes.
  React.useEffect(() => {
    setActiveIndex(-1);
  }, [query, search.data]);

  // "/" focuses the search box from anywhere that is not a text field.
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTypingTarget(e.target)) return;
      e.preventDefault();
      inputRef.current?.focus();
      setOpen(true);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Click outside closes the list.
  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const go = (item: FlatResult) => {
    if (!item.href) return;
    setOpen(false);
    setValue("");
    inputRef.current?.blur();
    router.push(item.href);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
      if (navigable.length === 0) return;
      const delta = e.key === "ArrowDown" ? 1 : -1;
      setActiveIndex((i) =>
        i === -1
          ? delta > 0
            ? 0
            : navigable.length - 1
          : (i + delta + navigable.length) % navigable.length,
      );
    } else if (e.key === "Enter") {
      const target =
        activeIndex >= 0
          ? navigable[activeIndex]
          : navigable.length === 1
            ? navigable[0]
            : undefined;
      if (target) {
        e.preventDefault();
        go(target);
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      if (open) {
        setOpen(false);
      } else {
        setValue("");
        inputRef.current?.blur();
      }
    }
  };

  const active = activeIndex >= 0 ? navigable[activeIndex] : undefined;
  const showPanel = open && typed.length > 0;

  return (
    <div ref={rootRef} className="relative hidden w-64 sm:block md:w-80">
      <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3">
        <Search className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
      </div>
      <input
        ref={inputRef}
        type="text"
        role="combobox"
        aria-label="Search Atlas"
        aria-autocomplete="list"
        aria-expanded={showPanel}
        aria-controls={listboxId}
        aria-activedescendant={active?.domId}
        autoComplete="off"
        spellCheck={false}
        maxLength={100}
        value={value}
        onChange={(e) => {
          setValue(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        className="block w-full rounded-full border-0 bg-[#F1F5F9] py-1.5 pl-10 pr-10 text-sm text-foreground placeholder:text-muted-foreground focus:ring-2 focus:ring-[var(--focus-ring-color)] sm:leading-6 transition-shadow"
        placeholder="Search Atlas..."
        data-testid="global-search-input"
      />
      <kbd
        className="pointer-events-none absolute inset-y-0 right-3 my-auto hidden h-5 items-center rounded border border-border px-1.5 text-[10px] text-muted-foreground md:flex"
        aria-hidden="true"
      >
        /
      </kbd>

      {showPanel && (
        <div
          className="absolute left-0 right-0 top-full z-50 mt-2 max-h-[28rem] overflow-y-auto rounded-atlas-lg border border-border bg-card py-1 shadow-atlas-3"
          data-testid="global-search-panel"
        >
          <ul id={listboxId} role="listbox" aria-label="Search results">
            <PanelStatus
              typedLength={typed.length}
              ready={ready}
              loading={
                (search.isFetching && !search.data) ||
                pending ||
                search.isLoading
              }
              isError={search.isError && !pending}
              empty={
                ready &&
                !pending &&
                !search.isFetching &&
                groups.length === 0 &&
                !search.isError
              }
              query={query}
            />
            {!pending &&
              groups.map((g) => {
                const Icon = GROUP_ICON[g.key] ?? Search;
                return (
                  <li key={g.key} role="presentation">
                    <div
                      className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground"
                      role="presentation"
                    >
                      {g.label}
                    </div>
                    <ul role="group" aria-label={g.label}>
                      {g.items.map((r) => {
                        const flatItem = flat.find(
                          (f) => f.result === r,
                        ) as FlatResult;
                        const isActive = active?.domId === flatItem.domId;
                        const disabled = flatItem.href === null;
                        return (
                          <li
                            key={r.id}
                            id={flatItem.domId}
                            role="option"
                            aria-selected={isActive}
                            aria-disabled={disabled || undefined}
                            data-testid={`search-result-${g.key}-${r.id}`}
                            onMouseDown={(e) => e.preventDefault()}
                            onClick={() => go(flatItem)}
                            className={cn(
                              "flex items-start gap-2.5 px-3 py-2",
                              disabled
                                ? "cursor-default opacity-70"
                                : "cursor-pointer hover:bg-secondary",
                              isActive && "bg-secondary",
                            )}
                            title={
                              disabled
                                ? "No page you can open shows this item"
                                : undefined
                            }
                          >
                            <Icon
                              className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
                              aria-hidden="true"
                            />
                            <span className="flex min-w-0 flex-1 flex-col">
                              <span className="truncate text-sm text-foreground">
                                {r.title}
                              </span>
                              {r.subtitle && (
                                <span className="truncate text-xs text-muted-foreground">
                                  {r.subtitle}
                                </span>
                              )}
                            </span>
                            {r.status && (
                              <span className="shrink-0 rounded-full bg-secondary px-2 py-0.5 text-[10px] text-muted-foreground">
                                {humanizeStatus(r.status)}
                              </span>
                            )}
                          </li>
                        );
                      })}
                    </ul>
                  </li>
                );
              })}
          </ul>
        </div>
      )}
    </div>
  );
}

function PanelStatus({
  typedLength,
  ready,
  loading,
  isError,
  empty,
  query,
}: {
  typedLength: number;
  ready: boolean;
  loading: boolean;
  isError: boolean;
  empty: boolean;
  query: string;
}) {
  let content: React.ReactNode = null;
  if (typedLength > 0 && typedLength < MIN_SEARCH_LENGTH) {
    content = `Type at least ${MIN_SEARCH_LENGTH} characters to search.`;
  } else if (loading) {
    content = (
      <span className="flex items-center gap-2">
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        Searching…
      </span>
    );
  } else if (isError) {
    content = (
      <span role="alert">Search is unavailable right now. Try again.</span>
    );
  } else if (empty && ready) {
    content = `No results for “${query}”.`;
  }
  if (content === null) return null;
  return (
    <li
      role="presentation"
      className="px-3 py-3 text-sm text-muted-foreground"
      data-testid="global-search-status"
      aria-live="polite"
    >
      {content}
    </li>
  );
}
