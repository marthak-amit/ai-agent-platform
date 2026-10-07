import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Loader2, ShieldCheck } from "lucide-react";
import { adminLogin, problemFrom } from "../../api/admin";
import { useAdminAuth } from "../../context/AdminAuthContext";
import { Button, ErrorNote, Field, inputCls } from "../../components/admin/ui";

export default function AdminLogin() {
  const { admin, signIn } = useAdminAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? "/admin";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [needOtp, setNeedOtp] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (admin) return <Navigate to={admin.must_change_password ? "/admin/account" : from} replace />;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await adminLogin(email.trim(), password, needOtp ? otp.trim() : undefined);
      signIn(result);
      navigate(result.admin.must_change_password ? "/admin/account" : from, { replace: true });
    } catch (err) {
      const p = problemFrom(err);
      if (p.code === "otp_required") {
        setNeedOtp(true);
        setError("");
      } else {
        if (p.code === "invalid_otp") setOtp("");
        setError(p.message);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-brand-secondary px-4 font-sans">
      <form onSubmit={submit} className="w-full max-w-sm bg-white rounded-2xl shadow-xl p-7 space-y-4">
        <div className="flex items-center gap-2">
          <ShieldCheck className="text-brand-primaryDark" />
          <div>
            <h1 className="text-lg font-bold text-gray-900 leading-tight">Admin console</h1>
            <p className="text-xs text-gray-500">SellerTalk24 operators only</p>
          </div>
        </div>
        <Field label="Email">
          <input className={inputCls} type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus disabled={needOtp} />
        </Field>
        <Field label="Password">
          <input className={inputCls} type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required disabled={needOtp} />
        </Field>
        {needOtp && (
          <Field label="Authentication code" hint="6-digit code from your authenticator app">
            <input className={inputCls} inputMode="numeric" autoComplete="one-time-code" pattern="[0-9 ]{6,7}" maxLength={7} value={otp} onChange={(e) => setOtp(e.target.value)} required autoFocus />
          </Field>
        )}
        <ErrorNote message={error} />
        <Button type="submit" className="w-full" disabled={busy}>
          {busy && <Loader2 size={14} className="animate-spin" />} {needOtp ? "Verify" : "Sign in"}
        </Button>
        {needOtp && (
          <button type="button" className="text-xs text-gray-500 underline w-full" onClick={() => { setNeedOtp(false); setOtp(""); setError(""); }}>
            Use a different account
          </button>
        )}
      </form>
    </div>
  );
}
