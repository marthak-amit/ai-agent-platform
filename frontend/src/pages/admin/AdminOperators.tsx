import { useState } from "react";
import { Copy } from "lucide-react";
import { createOperator, listOperators, problemFrom, resetOperator2fa, resetOperatorPassword, unlockOperator, updateOperator } from "../../api/admin";
import { Badge, Button, Card, ErrorNote, Field, fmtDate, inputCls, Modal, PageHeader, Spinner, Table, Td } from "../../components/admin/ui";
import { useAsync } from "../../components/admin/useAsync";
import { useAdminAuth } from "../../context/AdminAuthContext";
import type { AdminRole, Operator } from "../../types/admin";

const ROLES: { value: AdminRole; label: string; help: string }[] = [
  { value: "superadmin", label: "Superadmin", help: "Everything, including operators and the audit log" },
  { value: "support", label: "Support", help: "Clients, chats, impersonation, flags, suspend/activate" },
  { value: "billing", label: "Billing", help: "Subscriptions, grants, plans, payment events" },
  { value: "viewer", label: "Viewer", help: "Read-only (no chats, no audit log)" },
];

function SecretReveal({ title, email, password, onClose }: { title: string; email: string; password: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <Modal title={title} onClose={onClose}>
      <p className="text-sm text-gray-600 mb-3">
        Give this one-time password to <strong>{email}</strong> over a secure channel. It is shown only now, and they must change it at first sign-in.
      </p>
      <div className="flex items-center gap-2 bg-gray-50 rounded-lg px-3 py-2 font-mono text-sm select-all break-all">
        <span className="flex-1">{password}</span>
        <button
          aria-label="Copy password"
          onClick={async () => { try { await navigator.clipboard.writeText(password); setCopied(true); } catch { /* clipboard blocked */ } }}
          className="text-gray-500 hover:text-gray-800"
        >
          <Copy size={15} />
        </button>
      </div>
      {copied && <p className="text-xs text-emerald-700 mt-2">Copied.</p>}
      <div className="flex justify-end mt-4"><Button onClick={onClose}>Done</Button></div>
    </Modal>
  );
}

export default function AdminOperators() {
  const { admin: me } = useAdminAuth();
  const { data, loading, error, reload } = useAsync(listOperators, []);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Operator | null>(null);
  const [secret, setSecret] = useState<{ title: string; email: string; password: string } | null>(null);
  const [note, setNote] = useState("");

  async function run(fn: () => Promise<unknown>, ok: string) {
    setNote("");
    try {
      await fn();
      setNote(ok);
      reload();
    } catch (err) {
      setNote(problemFrom(err).message);
    }
  }

  return (
    <>
      <PageHeader title="Operators" subtitle="People who can sign in to this console" actions={<Button onClick={() => setCreating(true)}>Add operator</Button>} />
      {note && <p className="text-sm text-gray-700 mb-3" role="status">{note}</p>}
      <Card>
        <ErrorNote message={error} onRetry={reload} />
        {loading && !data ? <Spinner /> : (
          <Table head={["Operator", "Role", "Security", "Last sign-in", ""]}>
            {(data ?? []).map((o) => {
              const self = o.id === me?.id;
              const locked = !!o.locked_until && new Date(o.locked_until) > new Date();
              return (
                <tr key={o.id}>
                  <Td>{o.name || o.email}{self && <span className="ml-1"><Badge tone="blue">you</Badge></span>}<div className="text-xs text-gray-500">{o.email}</div></Td>
                  <Td>{o.role} {!o.is_active && <Badge tone="red">disabled</Badge>}</Td>
                  <Td>
                    <Badge tone={o.totp_enabled ? "green" : "amber"}>{o.totp_enabled ? "2FA on" : "2FA off"}</Badge>{" "}
                    {o.must_change_password && <Badge tone="amber">password change pending</Badge>}{" "}
                    {locked && <Badge tone="red">locked</Badge>}
                  </Td>
                  <Td className="whitespace-nowrap text-gray-500">{fmtDate(o.last_login_at)}{o.last_login_ip && <div className="text-xs">{o.last_login_ip}</div>}</Td>
                  <Td className="whitespace-nowrap">
                    <Button variant="ghost" onClick={() => setEditing(o)} disabled={self}>Edit</Button>
                    <Button variant="ghost" onClick={() => { if (window.confirm(`Reset the password for ${o.email}? Their sessions end immediately.`)) void run(async () => { const r = await resetOperatorPassword(o.id); setSecret({ title: "Password reset", email: o.email, password: r.temporary_password }); }, "Password reset."); }}>Reset password</Button>
                    {o.totp_enabled && <Button variant="ghost" onClick={() => { if (window.confirm(`Remove 2FA for ${o.email}?`)) void run(() => resetOperator2fa(o.id), "2FA removed."); }}>Reset 2FA</Button>}
                    {locked && <Button variant="ghost" onClick={() => run(() => unlockOperator(o.id), "Unlocked.")}>Unlock</Button>}
                  </Td>
                </tr>
              );
            })}
          </Table>
        )}
      </Card>

      {creating && (
        <CreateDialog onClose={() => setCreating(false)} onCreated={(email, password) => { setCreating(false); setSecret({ title: "Operator created", email, password }); reload(); }} />
      )}
      {editing && <EditDialog operator={editing} onClose={() => setEditing(null)} onSaved={reload} />}
      {secret && <SecretReveal {...secret} onClose={() => setSecret(null)} />}
    </>
  );
}

function RoleSelect({ value, onChange }: { value: AdminRole; onChange: (r: AdminRole) => void }) {
  return (
    <Field label="Role" hint={ROLES.find((r) => r.value === value)?.help}>
      <select className={inputCls} value={value} onChange={(e) => onChange(e.target.value as AdminRole)}>
        {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
      </select>
    </Field>
  );
}

function CreateDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (email: string, password: string) => void }) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<AdminRole>("support");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await createOperator({ email: email.trim(), name: name.trim(), role });
      onCreated(r.admin.email, r.temporary_password);
    } catch (err) {
      setError(problemFrom(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Add operator" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Email"><input className={inputCls} type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus /></Field>
        <Field label="Name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} maxLength={100} /></Field>
        <RoleSelect value={role} onChange={setRole} />
        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={busy}>Create</Button>
        </div>
      </form>
    </Modal>
  );
}

function EditDialog({ operator, onClose, onSaved }: { operator: Operator; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(operator.name);
  const [role, setRole] = useState<AdminRole>(operator.role);
  const [active, setActive] = useState(operator.is_active);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await updateOperator(operator.id, { name, role, is_active: active });
      onSaved();
      onClose();
    } catch (err) {
      setError(problemFrom(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Edit ${operator.email}`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} maxLength={100} /></Field>
        <RoleSelect value={role} onChange={setRole} />
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> Account enabled</label>
        <p className="text-xs text-gray-500">Changing the role or disabling the account signs the operator out immediately.</p>
        <ErrorNote message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={busy}>Save</Button>
        </div>
      </form>
    </Modal>
  );
}
