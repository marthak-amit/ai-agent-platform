import { ExternalLink, Phone } from "lucide-react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { ChatDetail, OrderBrief } from "../../types/chat";
import { dateTime, rupees } from "../../utils/time";
import ChannelIcon from "./ChannelIcon";

const STATUS_TONE: Record<string, string> = {
  paid: "bg-emerald-100 text-emerald-800",
  payment_submitted: "bg-brand-warning/20 text-amber-900",
  pending_payment: "bg-gray-100 text-gray-700",
  cancelled: "bg-red-100 text-red-700",
  delivered: "bg-emerald-100 text-emerald-800",
  dispatched: "bg-blue-100 text-blue-800",
};

function StatusChip({ status }: { status: string }) {
  const { t } = useTranslation();
  const key = ["paid", "payment_submitted", "pending_payment", "cancelled", "delivered", "dispatched", "new", "confirmed"].includes(status)
    ? status
    : "other";
  return (
    <span className={`text-[11px] font-semibold px-2 py-0.5 rounded-full ${STATUS_TONE[status] ?? "bg-gray-100 text-gray-700"}`}>
      {t(`chat.order_status.${key}`, { status })}
    </span>
  );
}

function OrderCard({ order, highlight }: { order: OrderBrief; highlight?: boolean }) {
  return (
    <div className={`rounded-xl border p-3 ${highlight ? "border-brand-primary/40 bg-brand-primary/5" : "border-gray-100 bg-white"}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-semibold text-brand-secondary">#{order.order_number}</span>
        <StatusChip status={order.status} />
      </div>
      <ul className="mt-1.5 space-y-0.5">
        {order.items.map((it, i) => (
          <li key={i} className="text-xs text-gray-600 truncate">{it}</li>
        ))}
      </ul>
      <div className="flex items-center justify-between mt-2 text-xs text-gray-500">
        <span>{dateTime(order.created_at)}</span>
        <span className="font-semibold text-gray-800 tabular-nums">{rupees(order.total_amount)}</span>
      </div>
    </div>
  );
}

/** Right column: who the customer is, their current order and order history. */
export default function CustomerPanel({ detail }: { detail: ChatDetail }) {
  const { t } = useTranslation();
  const name = detail.customer_name?.trim() || t("chat.unknown_customer");
  const past = detail.orders.filter((o) => o.id !== detail.current_order?.id);

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="p-5 border-b border-gray-100 text-center">
        <div className="w-16 h-16 mx-auto rounded-full bg-brand-secondary text-white flex items-center justify-center text-xl font-bold">
          {name.slice(0, 2).toUpperCase()}
        </div>
        <h3 className="mt-3 font-semibold text-brand-secondary">{name}</h3>
        <div className="mt-1 flex items-center justify-center gap-1.5 text-sm text-gray-500">
          <Phone size={13} /> {detail.phone_number}
        </div>
        <div className="mt-2 inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full bg-brand-bg border border-gray-200 text-gray-600">
          <ChannelIcon channel={detail.channel} size={13} />
          <span className="capitalize">{detail.channel}</span>
        </div>
      </div>

      <section className="p-4">
        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2">{t("chat.current_order")}</h4>
        {detail.current_order ? (
          <OrderCard order={detail.current_order} highlight />
        ) : (
          <p className="text-sm text-gray-400">{t("chat.no_current_order")}</p>
        )}
      </section>

      <section className="px-4 pb-6">
        <div className="flex items-center justify-between mb-2">
          <h4 className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">{t("chat.past_orders")}</h4>
          <Link to="/orders" className="text-[11px] text-brand-primaryDark hover:underline flex items-center gap-1">
            {t("chat.all_orders")} <ExternalLink size={10} />
          </Link>
        </div>
        {past.length === 0 ? (
          <p className="text-sm text-gray-400">{t("chat.no_past_orders")}</p>
        ) : (
          <div className="space-y-2">
            {past.map((o) => (
              <OrderCard key={o.id} order={o} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
