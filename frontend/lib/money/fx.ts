/**
 * Currency display helpers — product gap 8.
 *
 * The server owns every conversion (config/constants/fx_rates.py — SIMULATED
 * reference rates, not live market data). This module only mirrors the list of
 * supported codes (for input validation before a PATCH; the server re-checks)
 * and formats the server's `AmountConversion` for display. It never computes a
 * rate or a converted amount itself.
 */

/** Mirror of the backend SUPPORTED_CURRENCIES (USD base reference table). */
export const SUPPORTED_CURRENCIES = [
  "AUD",
  "CAD",
  "CHF",
  "CNY",
  "EUR",
  "GBP",
  "INR",
  "JPY",
  "MXN",
  "SGD",
  "USD",
] as const;

export type SupportedCurrency = (typeof SUPPORTED_CURRENCIES)[number];

/** Zero-decimal currencies (ISO 4217 minor unit 0). */
const ZERO_DECIMAL = new Set(["JPY"]);

/** Mirror of backend `AmountConversion` (backend/app/core/money.py). */
export interface AmountConversion {
  original_amount: string | number;
  original_currency: string;
  /** Null when the original currency is not supported (never a 1:1 guess). */
  converted_amount: string | number | null;
  /** The base / report currency the amount is converted into. */
  currency: string;
  /** Units of `currency` per unit of the original, as the server shows it. */
  rate: string | null;
  supported: boolean;
  rates_as_of?: string;
  rates_source?: string;
}

/** Upper-cased, trimmed code when it is supported, else null. */
export function normalizeSupportedCurrency(
  value: string | null | undefined,
): SupportedCurrency | null {
  const code = (value ?? "").trim().toUpperCase();
  return (SUPPORTED_CURRENCIES as readonly string[]).includes(code)
    ? (code as SupportedCurrency)
    : null;
}

/** "EUR 220.00" / "JPY 25000" — the currency's own minor unit. */
export function formatCodeAmount(
  amount: string | number | null | undefined,
  currency: string,
): string {
  const n = typeof amount === "number" ? amount : Number(amount);
  if (amount === null || amount === undefined || Number.isNaN(n)) return "—";
  const digits = ZERO_DECIMAL.has(currency.toUpperCase()) ? 0 : 2;
  return `${currency.toUpperCase()} ${n.toFixed(digits)}`;
}

/** "EUR 220.00 ≈ USD 238.70 at 1.085", or the not-supported sentence. */
export function describeConversion(conv: AmountConversion): string {
  const original = formatCodeAmount(
    conv.original_amount,
    conv.original_currency,
  );
  if (!conv.supported || conv.converted_amount === null || !conv.rate) {
    return `${original} — ${conv.original_currency} is not a supported currency, so it cannot be converted to ${conv.currency}.`;
  }
  return `${original} ≈ ${formatCodeAmount(conv.converted_amount, conv.currency)} at ${conv.rate}`;
}

/** The provenance note shown next to every converted figure. */
export function conversionSourceNote(conv: AmountConversion): string {
  const asOf = conv.rates_as_of ? `, as of ${conv.rates_as_of}` : "";
  return `Simulated reference rate (not live market data${asOf}).`;
}
