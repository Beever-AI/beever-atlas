import { Search, X } from "lucide-react";

interface SidebarSearchProps {
  value: string;
  onChange: (value: string) => void;
}

export function SidebarSearch({ value, onChange }: SidebarSearchProps) {
  return (
    <div className="relative px-2 py-2">
      <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground/60 pointer-events-none" />
      <input
        type="text"
        placeholder="Find a channel..."
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full pl-8 pr-7 py-1.5 rounded-lg bg-muted/60 border border-border/40 text-[13px] text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:border-primary/40 focus:bg-muted/80 focus:ring-1 focus:ring-primary/20 transition-all"
      />
      {value && (
        <button
          onClick={() => onChange("")}
          className="absolute right-4 top-1/2 -translate-y-1/2 p-0.5 rounded hover:bg-muted-foreground/10 text-muted-foreground"
        >
          <X size={12} />
        </button>
      )}
    </div>
  );
}
