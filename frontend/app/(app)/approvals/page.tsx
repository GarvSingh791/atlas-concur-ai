import * as React from "react";

import { ApprovalsScreen } from "@/components/approvals/approvals-screen";
import { LoadingState } from "@/components/atlas";

export default function ApprovalsPage() {
  // useSearchParams (URL-synced filters) needs a Suspense boundary.
  return (
    <React.Suspense fallback={<LoadingState variant="table" rows={5} />}>
      <ApprovalsScreen />
    </React.Suspense>
  );
}
