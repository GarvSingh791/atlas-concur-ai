import { TripList } from "@/components/trips/trip-list";

export default function TripsPage() {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-[var(--space-4)] p-[var(--space-4)]">
      <h1 className="atlas-text-heading text-foreground">My Trips</h1>
      <TripList />
    </div>
  );
}
