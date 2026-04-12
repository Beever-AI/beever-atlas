import { Star } from "lucide-react";
import { cn } from "@/lib/utils";

interface FavoriteButtonProps {
  isFavorite: boolean;
  onToggle: () => void;
}

export function FavoriteButton({ isFavorite, onToggle }: FavoriteButtonProps) {
  return (
    <button
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        onToggle();
      }}
      className={cn(
        "p-0.5 rounded transition-colors shrink-0",
        isFavorite
          ? "text-muted-foreground/45 hover:text-amber-500/80"
          : "text-transparent group-hover:text-muted-foreground/40 hover:!text-amber-500/70"
      )}
      aria-label={isFavorite ? "Unpin from favorites" : "Pin to favorites"}
    >
      <Star size={12} fill={isFavorite ? "currentColor" : "none"} />
    </button>
  );
}
