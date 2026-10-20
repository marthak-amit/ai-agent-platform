import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Inbox, Rocket } from "lucide-react";
import { useAuth } from "../../context/AuthContext";
import { useSetup } from "../../context/SetupContext";
import { CHECKLIST_ROUTES } from "../../utils/setup";

type Kind = "orders" | "customers" | "chats";

/**
 * The body of an empty list page. Instead of a dead "nothing here", it points at the NEXT UNFINISHED setup step
 * (one clear button), or — once the account is live — tells the owner how to get the first one.
 * Non-owners (no checklist) just see the neutral message.
 */
export default function EmptyStateCta({ kind }: { kind: Kind }) {
  const { t } = useTranslation();
  const { client } = useAuth();
  const { checklist } = useSetup();
  const next = checklist && !checklist.hidden ? checklist.next_key : null;
  const live = checklist?.hidden === true;

  return (
    <div className="flex flex-col items-center text-center px-6 py-14 bg-white rounded-2xl border border-gray-100">
      <span className="w-12 h-12 rounded-full bg-brand-primary/10 flex items-center justify-center mb-3">
        <Inbox size={22} className="text-brand-primaryDark" />
      </span>
      <h2 className="text-base font-bold text-gray-900">{t(`empty.${kind}_title`)}</h2>
      <p className="text-sm text-gray-500 mt-1 max-w-sm">{t(`empty.${kind}_desc`)}</p>

      {next && (
        <div className="mt-5 flex flex-col items-center gap-2">
          <p className="text-xs uppercase tracking-wide font-semibold text-gray-400">{t("empty.next_step")}</p>
          <p className="text-sm text-gray-600">{t("empty.setup_hint")}</p>
          <Link
            to={CHECKLIST_ROUTES[next]}
            className="inline-flex items-center gap-2 bg-brand-primaryDark text-white text-sm font-semibold px-5 py-2.5 rounded-lg hover:opacity-90"
          >
            <Rocket size={15} />
            {t(`setup.cta.${next}`)}
          </Link>
          <span className="text-xs text-gray-400">{t(`setup.item.${next}.title`)}</span>
        </div>
      )}

      {live && (
        <div className="mt-5 flex flex-col items-center gap-2">
          <p className="text-sm text-gray-600">{t("empty.live_hint")}</p>
          {client?.catalogue_slug && (
            <a
              href={`/shop/${client.catalogue_slug}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-2 border border-brand-primary text-brand-primaryDark text-sm font-semibold px-5 py-2.5 rounded-lg hover:bg-brand-primary/10"
            >
              {t("empty.view_catalogue")}
            </a>
          )}
        </div>
      )}
      <p className="text-[11px] text-gray-400 mt-6 max-w-xs">{t("empty.simulator_hint")}</p>
    </div>
  );
}
