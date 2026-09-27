import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Role, User } from "../../api";
import { useAuth } from "../../auth";
import { formatAge, formatDateTime, secondsSince } from "../../format";
import { ErrorText, Loading, Modal } from "../../components/ui";

const ROLE_HELP: Record<Role, string> = {
  viewer: "View dashboards, trends and alarms",
  operator: "Viewer + acknowledge alarms",
  admin: "Full access: devices, rules, users",
};

function UserModal({ user, onClose }: { user?: User; onClose: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState({
    email: user?.email ?? "",
    name: user?.name ?? "",
    role: (user?.role ?? "viewer") as Role,
    password: "",
    disabled: user?.disabled ?? false,
  });
  const save = useMutation({
    mutationFn: () => {
      if (user) {
        const body: Record<string, unknown> = { name: form.name, role: form.role, disabled: form.disabled };
        if (form.password) body.password = form.password;
        return api(`/users/${user.id}`, { method: "PATCH", body });
      }
      return api("/users", { method: "POST", body: form });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      onClose();
    },
  });
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    save.mutate();
  };
  return (
    <Modal
      title={user ? `Edit ${user.email}` : "Invite user"}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancel</button>
          <button className="primary" onClick={() => submit()} disabled={save.isPending}>
            Save
          </button>
        </>
      }
    >
      <form className="form-grid" onSubmit={submit}>
        <label className="field">
          Email
          <input type="email" value={form.email} disabled={!!user} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
        </label>
        <label className="field">
          Name
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        <label className="field">
          Role
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
            <option value="viewer">Viewer</option>
            <option value="operator">Operator</option>
            <option value="admin">Admin</option>
          </select>
          <span className="hint">{ROLE_HELP[form.role]}</span>
        </label>
        <label className="field">
          {user ? "New password (optional)" : "Initial password"}
          <input type="password" autoComplete="new-password" minLength={8} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required={!user} />
          <span className="hint">At least 8 characters</span>
        </label>
        {user && (
          <label className="check full">
            <input type="checkbox" checked={form.disabled} onChange={(e) => setForm({ ...form, disabled: e.target.checked })} />
            Disabled (cannot sign in)
          </label>
        )}
      </form>
      <ErrorText error={save.error} />
    </Modal>
  );
}

export function AdminUsers() {
  const qc = useQueryClient();
  const { user: me } = useAuth();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/users") });
  const [editing, setEditing] = useState<User | "new" | null>(null);
  const remove = useMutation({
    mutationFn: (id: number) => api(`/users/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });
  return (
    <>
      <div className="page-header">
        <div>
          <h1>Users</h1>
          <div className="sub">Who can sign in, and what they can do</div>
        </div>
        <button className="primary" onClick={() => setEditing("new")}>
          Invite user
        </button>
      </div>
      <ErrorText error={remove.error} />
      <div className="card">
        {users.isLoading && <Loading />}
        {users.data && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>User</th>
                  <th>Role</th>
                  <th>Last sign-in</th>
                  <th>Created</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {users.data.map((u) => (
                  <tr key={u.id} className={u.disabled ? "stale" : undefined}>
                    <td>
                      <div style={{ fontWeight: 500 }}>{u.name || u.email}</div>
                      <div className="small muted">
                        {u.email}
                        {u.disabled ? " · disabled" : ""}
                      </div>
                    </td>
                    <td className="small">{u.role}</td>
                    <td className="small">{formatAge(secondsSince(u.last_login_at))}</td>
                    <td className="small">{formatDateTime(u.created_at)}</td>
                    <td className="num nowrap">
                      <button className="small ghost" onClick={() => setEditing(u)}>
                        Edit
                      </button>
                      {u.id !== me.id && (
                        <button className="small ghost danger" onClick={() => confirm(`Delete ${u.email}?`) && remove.mutate(u.id)}>
                          Delete
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {editing && <UserModal user={editing === "new" ? undefined : editing} onClose={() => setEditing(null)} />}
    </>
  );
}

interface AuditEntry {
  id: number;
  ts: string;
  actor: string;
  action: string;
  target: string;
  details: Record<string, unknown>;
}

export function AdminAudit() {
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => api<AuditEntry[]>("/system/audit?limit=300"), refetchInterval: 30_000 });
  return (
    <>
      <div className="page-header">
        <div>
          <h1>Audit log</h1>
          <div className="sub">Sign-ins, configuration changes, alarm acknowledgements and exports</div>
        </div>
      </div>
      <div className="card">
        {audit.isLoading && <Loading />}
        <ErrorText error={audit.error} />
        {audit.data && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>User</th>
                  <th>Action</th>
                  <th>Target</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {audit.data.map((a) => (
                  <tr key={a.id}>
                    <td className="small nowrap">{formatDateTime(a.ts)}</td>
                    <td className="small">{a.actor}</td>
                    <td className="small mono">{a.action}</td>
                    <td className="small mono">{a.target}</td>
                    <td className="small mono muted" style={{ maxWidth: 380, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={JSON.stringify(a.details)}>
                      {Object.keys(a.details).length ? JSON.stringify(a.details) : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
