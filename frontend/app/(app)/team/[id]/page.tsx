import * as React from "react";

import { LoadingState } from "@/components/atlas";
import { TeamMemberScreen } from "@/components/team/team-member-screen";

/** Team member drill-down (Product gap 5) — read-only trips + reports. */
export default function TeamMemberPage({ params }: { params: { id: string } }) {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-4 sm:p-6">
      {/* useSearchParams (linkable tab / report) needs a Suspense boundary. */}
      <React.Suspense fallback={<LoadingState variant="card" rows={2} />}>
        <TeamMemberScreen memberId={params.id} />
      </React.Suspense>
    </div>
  );
}
