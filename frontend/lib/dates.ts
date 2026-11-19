/** Shared date-parsing helpers for display formatting. */

/**
 * C7-20: parse a value for calendar display. A date-only "YYYY-MM-DD" string
 * (e.g. expense_date) is a CALENDAR date with no time zone; `new Date("2026-06-10")`
 * parses it as UTC midnight, which renders as Jun 9 in any zone west of UTC.
 * Build it as LOCAL midnight instead. Full timestamps keep `new Date` semantics.
 */
export function parseDateForDisplay(value: string): Date {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (m) {
    const [, y, mo, d] = m;
    const local = new Date(Number(y), Number(mo) - 1, Number(d));
    // Reject impossible dates (e.g. 2026-02-31 rolls over to Mar 3).
    if (
      local.getFullYear() !== Number(y) ||
      local.getMonth() !== Number(mo) - 1 ||
      local.getDate() !== Number(d)
    ) {
      return new Date(NaN);
    }
    return local;
  }
  return new Date(value);
}
