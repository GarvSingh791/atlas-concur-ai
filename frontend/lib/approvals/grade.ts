/**
 * Follow-up (grades): display helpers for the approver-grade level.
 *
 * The short forms mirror the backend ladder (config/constants/grades.py); they
 * are labels only — the server decides who may approve and enforces it.
 */

const GRADE_SHORT: Record<string, string> = {
  CSUITE: "C-suite",
  SVP: "SVP",
  VP: "VP",
  DIRECTOR: "Director",
  MANAGER: "Manager",
  SENIOR_IC: "Senior IC",
  IC: "IC",
  INTERN: "Intern",
};

/** "Requires SVP" for a ladder code; null when no grade is required. */
export function requiredGradeLabel(
  code: string | null | undefined,
): string | null {
  if (!code) return null;
  return `Requires ${GRADE_SHORT[code] ?? code}`;
}
