/**
 * tau*-normalized display transform for the policy grounding-confidence bar
 * (R6-T2 / B2).
 *
 * The calibrated relevance score for a GROUNDED policy answer lives in a tiny
 * band just above tau* (~0.039299 on live; grounded scores are ~0.04–0.08).
 * Rendered raw through the shared ConfidenceIndicator those values would all
 * paint RED (<70%) — visually equating a faithful, above-threshold answer with
 * "low confidence", which is wrong: any answer that cleared calibration is by
 * definition trustworthy enough to surface.
 *
 * So we remap the calibrated score in LOGIT space onto a display band whose
 * FLOOR is 0.70 (the amber threshold) and whose top (0.90, green) is reached at
 * a calibrated score of P_FULL = 0.90. Result: a grounded answer NEVER renders
 * red; the bar still grows monotonically with the underlying relevance.
 *
 * IMPORTANT: this is a DISPLAY transform only. The raw calibrated score + tau*
 * are always surfaced verbatim in the tooltip — we never hide the real number.
 *
 * The shared ConfidenceIndicator + OCR field banding are left UNTOUCHED; this
 * transform is applied UPSTREAM on the policy surface only.
 */

/** Floor of the display band — the amber/red boundary in ConfidenceIndicator. */
export const DISPLAY_FLOOR = 0.7;
/** Calibrated score at which the display bar reaches full (green). */
export const P_FULL = 0.9;

function logit(x: number): number {
  return Math.log(x / (1 - x));
}

function clamp01(x: number): number {
  return Math.max(0, Math.min(1, x));
}

/**
 * Map a CALIBRATED relevance score onto the [0.70, 1.00] display band.
 *
 * @param calibrated raw calibrated relevance score (float 0..1), expected > tau*
 * @param tauStar    calibrated relevance threshold tau* (float 0..1)
 * @returns display fraction in [0.70, 1.00] (feed *100 into ConfidenceIndicator)
 *
 * Properties (tau* = 0.039299):
 *   displayConfidence(0.039299, tau*) === 0.70   (exactly the floor)
 *   displayConfidence(0.06,     tau*) ≈ 0.7247    (72.5%, amber)
 *   displayConfidence(0.08,     tau*) ≈ 0.7419    (74.2%, amber)
 *   displayConfidence(0.5,      tau*) ≈ 0.8778    (87.8%, amber — NOT green)
 *   strictly monotonic increasing; for all s > tau*, result >= 0.70.
 */
export function displayConfidence(calibrated: number, tauStar: number): number {
  const t = clamp01(
    (logit(calibrated) - logit(tauStar)) / (logit(P_FULL) - logit(tauStar)),
  );
  return DISPLAY_FLOOR + (1 - DISPLAY_FLOOR) * t;
}

/**
 * Resolve the value (fraction 0..1) to render on the policy grounding bar.
 *
 * Applies the tau*-normalized transform ONLY on the calibrated path:
 *   - confidenceSource === "calibrated": transform (needs a threshold; if the
 *     backend omitted one, no safe remap is possible so pass through).
 *   - confidenceSource absent AND raw < 0.5: DEFENSIVE transform. The cosine
 *     path is always >= 0.5, so a sub-0.5 value with no declared source can
 *     only be a calibrated score — remapping guarantees it never renders red.
 *     Falls back to tau* = raw (→ floor 0.70) when no threshold was supplied.
 *   - otherwise (cosine, or a "normal" score): pass through unchanged.
 */
export function resolveGroundingFraction(
  raw: number,
  confidenceSource?: string | null,
  tauStar?: number | null,
): number {
  const isCalibrated = confidenceSource === "calibrated";
  const isDefensive =
    (confidenceSource === undefined || confidenceSource === null) && raw < 0.5;

  if (isCalibrated || isDefensive) {
    // A valid tau* must be strictly between 0 and 1 and below the raw score.
    const tau =
      typeof tauStar === "number" && tauStar > 0 && tauStar < 1 && tauStar < raw
        ? tauStar
        : raw; // → displayConfidence(raw, raw) === floor (0.70)
    return displayConfidence(raw, tau);
  }
  return raw;
}
