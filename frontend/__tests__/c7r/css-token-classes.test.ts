// @vitest-environment node
/**
 * UAT round 2 — Issue 3 (receipts "Confidence" label covering Edit/Save).
 *
 * Root cause: Tailwind 3.4 reads an arbitrary `text-[var(--x)]` with no type
 * hint as a COLOUR, so `text-[var(--text-caption-size)]` compiled to
 * `color: var(--text-caption-size)` — no font-size at all (16px inherited), and
 * the invalid colour also beat the grey caption colour. tailwind-merge (via
 * `cn()`) likewise classified it as a colour and DROPPED it next to a real
 * colour class. The fix is the `length:` type hint on every size token.
 *
 * These guards keep the mistake from coming back:
 *   1. no source file under a Tailwind content root may use `text-[var(--…)]`
 *      for anything but a colour variable;
 *   2. the typed size class compiles to `font-size` and survives `cn()`;
 *   3. the receipts field-table container query in globals.css compiles.
 */
import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import path from "node:path";
import postcss from "postcss";
import tailwindcss from "tailwindcss";

import { cn } from "@/lib/utils";
import tailwindConfig from "@/tailwind.config";

const ROOT = path.resolve(__dirname, "../..");
// Every Tailwind content root (tailwind.config.ts) plus stores/ defensively.
const SOURCE_ROOTS = ["app", "components", "features", "lib", "stores"];

function walk(dir: string, out: string[] = []): string[] {
  if (!existsSync(dir)) return out;
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(ts|tsx)$/.test(name)) out.push(full);
  }
  return out;
}

/** A `text-[var(--name)]` (no type hint) is only safe for colour variables. */
function isColourVar(name: string): boolean {
  return /^color-/.test(name) || /-color$/.test(name);
}

async function compile(css: string, rawContent: string): Promise<string> {
  const result = await postcss([
    tailwindcss({
      ...tailwindConfig,
      content: [{ raw: rawContent, extension: "html" }],
    }),
  ]).process(css, { from: undefined });
  return result.css;
}

describe("Issue 3 — font-size token classes", () => {
  it("no untyped text-[var(--…)] class points at a non-colour variable", () => {
    const offenders: string[] = [];
    const re = /text-\[var\(--([a-zA-Z0-9-]+)\)\]/g;
    for (const root of SOURCE_ROOTS) {
      for (const file of walk(path.join(ROOT, root))) {
        const lines = readFileSync(file, "utf8").split("\n");
        lines.forEach((line, i) => {
          for (const m of Array.from(line.matchAll(re))) {
            if (!isColourVar(m[1])) {
              offenders.push(
                `${path.relative(ROOT, file)}:${i + 1}  ${m[0]}  → use text-[length:var(--${m[1]})]`,
              );
            }
          }
        });
      }
    }
    expect(offenders, offenders.join("\n")).toEqual([]);
  });

  it("the walk actually sees the typed size classes (guard is not vacuous)", () => {
    let typed = 0;
    for (const root of SOURCE_ROOTS) {
      for (const file of walk(path.join(ROOT, root))) {
        const src = readFileSync(file, "utf8");
        typed += (
          src.match(/text-\[length:var\(--text-[a-z-]+-size\)\]/g) ?? []
        ).length;
      }
    }
    expect(typed).toBeGreaterThanOrEqual(50);
  });

  it("font-size token classes survive cn() merging with a colour class", () => {
    expect(cn("text-[length:var(--text-caption-size)]", "text-[#64748B]")).toBe(
      "text-[length:var(--text-caption-size)] text-[#64748B]",
    );
    const merged = cn(
      "text-[length:var(--text-caption-size)] font-medium",
      "text-[var(--color-ai-primary)]",
    );
    expect(merged).toContain("text-[length:var(--text-caption-size)]");
    expect(merged).toContain("text-[var(--color-ai-primary)]");
  });

  it("size before leading keeps both classes through cn()", () => {
    // tailwind-merge lets a font-size override an EARLIER leading-*; authors
    // must put the size class first (all current usages do).
    expect(
      cn(
        "text-[length:var(--text-body-size)] leading-[var(--text-body-line-height)]",
      ),
    ).toBe(
      "text-[length:var(--text-body-size)] leading-[var(--text-body-line-height)]",
    );
  });

  it("the typed class compiles to font-size (and the untyped one to color)", async () => {
    const css = await compile(
      "@tailwind utilities;",
      '<span class="text-[length:var(--text-caption-size)] text-[var(--text-body-size)]"></span>',
    );
    expect(css).toMatch(
      /\.text-\\\[length\\:var\\\(--text-caption-size\\\)\\\]\s*\{\s*font-size:\s*var\(--text-caption-size\)/,
    );
    // Documents the trap the guard above prevents.
    expect(css).toMatch(
      /\.text-\\\[var\\\(--text-body-size\\\)\\\]\s*\{\s*color:\s*var\(--text-body-size\)/,
    );
  });
});

describe("Issue 3 — receipts field-table container query", () => {
  const globals = readFileSync(path.join(ROOT, "styles/globals.css"), "utf8");

  it("globals.css defines the size container and the stacked/four-column grid", () => {
    expect(globals).toContain(".atlas-field-table");
    expect(globals).toMatch(/container-type:\s*inline-size/);
    expect(globals).toMatch(/@container\s+atlas-fields\s*\(min-width:/);
    expect(globals).toContain(".atlas-field-grid");
    expect(globals).toContain(".atlas-field-header");
  });

  it("the container query survives a Tailwind compile of globals.css", async () => {
    const fieldRow = readFileSync(
      path.join(ROOT, "components/receipts/field-row.tsx"),
      "utf8",
    );
    const review = readFileSync(
      path.join(ROOT, "components/receipts/extraction-review.tsx"),
      "utf8",
    );
    const css = await compile(globals, fieldRow + review);
    expect(css).toMatch(/container-type:\s*inline-size/);
    expect(css).toMatch(/@container atlas-fields \(min-width:\s*36rem\)/);
    expect(css).toMatch(
      /grid-template-areas:\s*"label value confidence source"/,
    );
    // Every grid cell can shrink below its content (the overlap root cause).
    for (const cell of ["label", "value", "confidence", "source"]) {
      expect(css).toContain(`[data-cell="${cell}"]`);
    }
  });
});
