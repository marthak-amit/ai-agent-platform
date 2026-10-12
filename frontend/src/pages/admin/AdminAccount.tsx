import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import QRCode from "qrcode";
import { changeAdminPassword, problemFrom, totpDisable, totpEnable, totpSetup } from "../../api/admin";
import { useAdminAuth } from "../../context/AdminAuthContext";
import { Badge, Button, Card, ErrorNote, Field, fmtDate, inputCls, PageHeader } from "../../components/admin/ui";

export default function AdminAccount() {
  const { admin, signIn, signOut, refresh } = useAdminAuth();
  const navigate = useNavigate();
  if (!admin) return null;

  const isOperator = admin.id !== null;
  async function afterSecurityChange() {
    // Enabling/disabling 2FA revokes every session server-side, so sign in again.
    await signOut();
    navigate("/admin/login");
  }

  return (
    <>
      <PageHeader title="My account" subtitle={`${admin.email} · ${admin.role} · last sign-in ${fmtDate(admin.last_login_at)}`} />
      {admin.must_change_password && (
        <div className="mb-4 rounded-xl bg-amber-50 border border-amber-200 text-amber-800 text-sm px-4 py-3" role="alert">
          You're using a temporary password. Set a new one to continue.
        </div>
      )}
      {!isOperator ? (
        <Card><p className="text-sm text-gray-600">You're signed in with the shared admin key, which has no personal account. Ask a superadmin to create an operator account for you.</p></Card>
      ) : (
        <div className="grid gap-6 lg:grid-cols-2">
          <PasswordCard onChanged={(t) => { signIn(t); navigate("/admin"); }} />
          <TotpCard enabled={admin.totp_enabled} email={admin.email} onChanged={afterSecurityChange} onRefresh={refresh} />
        </div>
      )}
      <Card title="Permissions" className="mt-6">
        <div className="flex flex-wrap gap-1.5">{admin.permissions.map((p) => <Badge key={p} tone="gray">{p}</Badge>)}</div>
      </Card>
    </>
  );
}

function PasswordCard({ onChanged }: { onChanged: (token: Awaited<ReturnType<typeof changeAdminPassword>>) => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (next !== again) return setError("The new passwords don't match.");
    setBusy(true);
    setError("");
    try {
      onChanged(await changeAdminPassword(current, next));
    } catch (err) {
      setError(problemFrom(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Change password">
      <form onSubmit={submit} className="space-y-3">
        <Field label="Current password"><input className={inputCls} type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required /></Field>
        <Field label="New password" hint="At least 12 characters with letters and digits"><input className={inputCls} type="password" autoComplete="new-password" minLength={12} value={next} onChange={(e) => setNext(e.target.value)} required /></Field>
        <Field label="Repeat new password"><input className={inputCls} type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} required /></Field>
        <ErrorNote message={error} />
        <Button type="submit" disabled={busy}>Change password</Button>
      </form>
    </Card>
  );
}

function TotpCard({ enabled, email, onChanged, onRefresh }: { enabled: boolean; email: string; onChanged: () => Promise<void>; onRefresh: () => Promise<void> }) {
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [qr, setQr] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!setup) return setQr("");
    let alive = true;
    QRCode.toDataURL(setup.otpauth_uri, { margin: 1, width: 192 })
      .then((url) => alive && setQr(url))
      .catch(() => alive && setQr(""));
    return () => { alive = false; };
  }, [setup]);

  async function wrap(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (err) {
      setError(problemFrom(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Two-factor authentication" actions={<Badge tone={enabled ? "green" : "amber"}>{enabled ? "On" : "Off"}</Badge>}>
      {!enabled && !setup && (
        <>
          <p className="text-sm text-gray-600 mb-3">Protect your account with a code from an authenticator app (Google Authenticator, 1Password, Authy…).</p>
          <Button onClick={() => wrap(async () => { setSetup(await totpSetup()); await onRefresh(); })} disabled={busy}>Set up 2FA</Button>
        </>
      )}
      {!enabled && setup && (
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); void wrap(async () => { await totpEnable(code.trim()); await onChanged(); }); }}>
          <p className="text-sm text-gray-600">Scan this QR code, or enter the key manually for <strong>{email}</strong>.</p>
          {qr && <img src={qr} alt="2FA QR code" width={192} height={192} className="border rounded-lg" />}
          <code className="block text-xs bg-gray-50 rounded p-2 break-all select-all">{setup.secret}</code>
          <Field label="Code from the app"><input className={inputCls} inputMode="numeric" autoComplete="one-time-code" maxLength={7} value={code} onChange={(e) => setCode(e.target.value)} required /></Field>
          <ErrorNote message={error} />
          <Button type="submit" disabled={busy}>Turn on 2FA</Button>
          <p className="text-xs text-gray-400">You'll be signed out and asked for a code at your next sign-in.</p>
        </form>
      )}
      {enabled && (
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); if (window.confirm("Turn off two-factor authentication?")) void wrap(async () => { await totpDisable(password, code.trim()); await onChanged(); }); }}>
          <p className="text-sm text-gray-600">To turn 2FA off, confirm with your password and a current code.</p>
          <Field label="Password"><input className={inputCls} type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></Field>
          <Field label="Code"><input className={inputCls} inputMode="numeric" autoComplete="one-time-code" maxLength={7} value={code} onChange={(e) => setCode(e.target.value)} required /></Field>
          <ErrorNote message={error} />
          <Button type="submit" variant="danger" disabled={busy}>Turn off 2FA</Button>
        </form>
      )}
    </Card>
  );
}
