/**
 * Bridge between the API layer and the confirmation dialog for high-value payment approvals.
 *
 * `approvePayment()` (api/chat.ts) is plain async code with no access to React; when the server answers 409
 * `high_value_confirmation_required` it calls `askHighValueConfirmation()`, which resolves once the seller picks
 * "I've verified it" (true) or cancels (false). `<HighValueConfirmHost />` (mounted once in App) renders the dialog.
 * Without a mounted host (tests, an unexpected route) it falls back to the browser's confirm().
 */

export interface HighValueRequest {
  orderNumber: string;
  total: number | null;
  threshold: number | null;
}

type Listener = (req: HighValueRequest, resolve: (ok: boolean) => void) => void;

let listener: Listener | null = null;

/** Called by the host component on mount / unmount. */
export function registerHighValueHost(fn: Listener | null): void {
  listener = fn;
}

/** Ask the seller to confirm they verified the money; resolves true when they did. */
export function askHighValueConfirmation(req: HighValueRequest): Promise<boolean> {
  if (!listener) {
    const amount = req.total != null ? `₹${Math.round(req.total).toLocaleString("en-IN")}` : "This order";
    return Promise.resolve(window.confirm(`Verify ${amount} in your bank / UPI app before approving. Approve now?`));
  }
  return new Promise<boolean>((resolve) => listener!(req, resolve));
}
