import { UsersScreen } from "@/components/users/users-screen";

/**
 * Users administration (product gap 3). Finance-admin only: the nav item and
 * route gate key on users.read (nav-config), and every write is refused
 * server-side for any other role.
 */
export default function UsersPage() {
  return <UsersScreen />;
}
