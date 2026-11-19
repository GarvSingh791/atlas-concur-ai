"use client";

import { useSession } from "@/lib/auth/session-provider";
import { EmployeeDashboard } from "@/components/dashboard/employee-dashboard";
import { ManagerDashboard } from "@/components/dashboard/manager-dashboard";
import { FinanceDashboard } from "@/components/dashboard/finance-dashboard";

export default function DashboardPage() {
  const { role } = useSession();

  if (role === "finance_admin") return <FinanceDashboard />;
  if (role === "manager") return <ManagerDashboard />;
  return <EmployeeDashboard />; // default for employee + loading state
}
