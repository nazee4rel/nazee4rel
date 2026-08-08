import { redirect } from "next/navigation";

import { getCurrentUser } from "@/lib/api";

export default async function Home() {
  const user = await getCurrentUser();
  redirect(user ? "/overview" : "/login");
}
