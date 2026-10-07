/** GSTIN helpers for the billing details form. Mirrors backend/app/services/billing/invoices.py. */

/** 2-digit state code, 10-char PAN, entity number, "Z", checksum. */
const GSTIN_RE = /^(\d{2})[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$/;

/** GST state / UT codes (first two digits of a GSTIN). */
export const GST_STATE_NAMES: Record<string, string> = {
  "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
  "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
  "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
  "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh",
  "23": "Madhya Pradesh", "24": "Gujarat", "26": "Dadra & Nagar Haveli and Daman & Diu", "27": "Maharashtra",
  "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
  "35": "Andaman & Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
  "97": "Other Territory", "99": "Centre Jurisdiction",
};

/** Upper-case and strip whitespace as the user types. */
export function normaliseGstin(value: string): string {
  return value.replace(/\s+/g, "").toUpperCase();
}

/** True for a well-formed GSTIN with a known state code. Blank is "valid" (the field is optional). */
export function isValidGstin(value: string): boolean {
  const v = normaliseGstin(value);
  if (v === "") return true;
  const m = GSTIN_RE.exec(v);
  return !!m && m[1] in GST_STATE_NAMES;
}

export interface GstTreatment {
  stateName: string;
  /** Same state as the seller → CGST + SGST; another state → IGST. */
  intraState: boolean;
}

/** State and tax treatment implied by a valid GSTIN; null when blank/invalid or the seller state is unknown. */
export function gstTreatment(gstin: string, sellerStateCode: string): GstTreatment | null {
  const v = normaliseGstin(gstin);
  if (!sellerStateCode || v === "" || !isValidGstin(v)) return null;
  const code = v.slice(0, 2);
  return { stateName: GST_STATE_NAMES[code], intraState: code === sellerStateCode };
}
