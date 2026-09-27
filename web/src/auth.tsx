import { createContext, ReactNode, useContext } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, Role, User } from "./api";
import { live } from "./live";

const RANK: Record<Role, number> = { viewer: 0, operator: 1, admin: 2 };

interface AuthState {
  user: User;
  can: (role: Role) => boolean;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

/** Public: which absolute URLs the login page may return to (the simulator). */
export function useAuthConfig() {
  return useQuery({
    queryKey: ["auth-config"],
    queryFn: () => api<{ simulator_url: string | null; dashboard_url: string | null }>("/auth/config"),
    staleTime: Infinity,
  });
}

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await api<User>("/auth/me");
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: 60_000,
  });
}

export function AuthProvider({ user, children }: { user: User; children: ReactNode }) {
  const qc = useQueryClient();
  const state: AuthState = {
    user,
    can: (role) => RANK[user.role] >= RANK[role],
    logout: async () => {
      try {
        await api("/auth/logout", { method: "POST" });
      } finally {
        live.stop();
        qc.clear();
        window.location.replace("/logged-out");
      }
    },
  };
  return <AuthContext.Provider value={state}>{children}</AuthContext.Provider>;
}
