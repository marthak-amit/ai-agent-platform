import { forwardRef } from "react";
import { Plus } from "lucide-react";

const AddToolCard = forwardRef<HTMLDivElement>((_, ref) => {
  return (
    <div
      ref={ref}
      className="w-40 h-28 shrink-0 rounded-2xl border-2 border-dashed border-gray-300 flex flex-col items-center justify-center gap-1.5 text-gray-400 hover:border-brand-primary/40 hover:text-brand-primary transition-colors cursor-not-allowed bg-white/60"
    >
      <Plus size={18} />
      <span className="text-xs font-medium">Add Tool</span>
    </div>
  );
});
AddToolCard.displayName = "AddToolCard";

export default AddToolCard;
