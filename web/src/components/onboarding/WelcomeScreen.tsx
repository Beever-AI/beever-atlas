import { useState, useRef, useEffect } from "react";
import {
  MessageSquare,
  Monitor,
  Building2,
  MessagesSquare,
  FileText,
  ChevronRight,
  Sparkles,
  Brain,
  Compass,
  Lock,
} from "lucide-react";
import EmojiPicker, { Theme } from "emoji-picker-react";
import type { EmojiClickData } from "emoji-picker-react";
import { cn } from "@/lib/utils";
import { useUserProfile, AVATAR_COLORS } from "@/hooks/useUserProfile";

interface WelcomeScreenProps {
  onConnect: (platform: string) => void;
}

const PLATFORMS: {
  key: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  desc: string;
}[] = [
  { key: "slack", label: "Slack", icon: MessageSquare, desc: "Slack workspace" },
  { key: "discord", label: "Discord", icon: Monitor, desc: "Discord server" },
  { key: "teams", label: "Teams", icon: Building2, desc: "Teams tenant" },
  { key: "mattermost", label: "Mattermost", icon: MessagesSquare, desc: "Mattermost server" },
  { key: "file", label: "File Import", icon: FileText, desc: "CSV / TSV / JSONL export" },
];

// OSS personal-intelligence positioning (NOT "your team's second brain"
// — that copy is reserved for the enterprise tier).
const HERO_POINTS = [
  { icon: Brain, text: "Compounding memory across every conversation" },
  { icon: Compass, text: "Living wiki that refreshes incrementally" },
  { icon: Lock, text: "Local-first, self-hostable, yours alone" },
];

export function WelcomeScreen({ onConnect }: WelcomeScreenProps) {
  const { profile, saveProfile } = useUserProfile();
  const [step, setStep] = useState<"profile" | "connect">("profile");
  const [nameValue, setNameValue] = useState(profile.displayName || "");
  const [titleValue, setTitleValue] = useState(profile.jobTitle || "");
  const [chosenColor, setChosenColor] = useState(profile.avatarColor || AVATAR_COLORS[0].hsl);
  const [chosenEmoji, setChosenEmoji] = useState(profile.avatarEmoji || "🦫");
  const [showEmojiPicker, setShowEmojiPicker] = useState(false);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const emojiPickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (emojiPickerRef.current && !emojiPickerRef.current.contains(event.target as Node)) {
        setShowEmojiPicker(false);
      }
    }
    if (showEmojiPicker) {
      document.addEventListener("mousedown", handleClickOutside);
    }
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, [showEmojiPicker]);

  function handleProfileContinue() {
    if (!nameValue.trim()) {
      nameInputRef.current?.focus();
      return;
    }
    saveProfile({
      displayName: nameValue.trim(),
      jobTitle: titleValue.trim(),
      avatarColor: chosenColor,
      avatarEmoji: chosenEmoji,
    });
    setStep("connect");
  }

  return (
    // ``overflow-hidden`` instead of ``overflow-y-auto`` — the layout is
    // designed to fit within the main area without scroll. ``h-full``
    // pins to the AppShell main height; the ambient gradient blobs and
    // 2-column grid are sized to land within it on standard laptop
    // viewports (≥720p).
    <div className="relative h-full overflow-hidden bg-background">
      {/* Ambient gradient blobs */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div
          className="absolute -top-[15%] -left-[10%] h-[55%] w-[55%] rounded-full bg-primary/20 blur-[140px] animate-pulse"
          style={{ animationDuration: "9s" }}
        />
        <div
          className="absolute -bottom-[20%] -right-[10%] h-[55%] w-[55%] rounded-full bg-blue-500/15 blur-[160px] animate-pulse"
          style={{ animationDuration: "11s" }}
        />
        <div
          className="absolute top-[25%] right-[20%] h-[35%] w-[35%] rounded-full bg-teal-500/10 blur-[120px] animate-pulse"
          style={{ animationDuration: "13s" }}
        />
      </div>

      <div className="relative z-10 flex h-full items-center justify-center px-6 py-6 sm:px-10 sm:py-10">
        <div className="w-full max-w-5xl animate-fade-in">
          {step === "profile" ? (
            <div className="grid grid-cols-1 items-center gap-10 md:grid-cols-[1fr_1.05fr] lg:gap-14">
              {/* ─── Hero column ────────────────────────────────── */}
              <div className="space-y-6">
                <span className="inline-flex items-center gap-1.5 rounded-full border border-primary/20 bg-primary/10 px-3.5 py-1 text-[10.5px] font-bold uppercase tracking-[0.18em] text-primary shadow-[0_0_14px_rgba(11,79,108,0.18)] backdrop-blur-md">
                  <Sparkles className="h-3 w-3" />
                  Welcome to Beever Atlas
                </span>

                <h1 className="font-heading text-[36px] font-bold leading-[1.05] tracking-tight text-transparent bg-clip-text bg-gradient-to-br from-foreground via-foreground to-foreground/55 sm:text-[44px] lg:text-[52px]">
                  Your personal
                  <br />
                  intelligence,
                  <br />
                  one profile away.
                </h1>

                <p className="max-w-md text-[14.5px] leading-relaxed text-muted-foreground">
                  A compounding LLM wiki that learns from every conversation
                  you save — your second brain that never forgets a thing.
                </p>

                <ul className="space-y-2.5 pt-1">
                  {HERO_POINTS.map(({ icon: Icon, text }) => (
                    <li
                      key={text}
                      className="flex items-center gap-3 text-[13px] text-muted-foreground"
                    >
                      <span className="flex h-7 w-7 items-center justify-center rounded-lg border border-primary/20 bg-primary/10 text-primary">
                        <Icon className="h-3.5 w-3.5" />
                      </span>
                      {text}
                    </li>
                  ))}
                </ul>

                {/* Step indicator */}
                <div className="flex gap-2 pt-2">
                  <div className="h-1.5 w-10 rounded-full bg-primary shadow-[0_0_8px_rgba(11,79,108,0.5)]" />
                  <div className="h-1.5 w-2 rounded-full bg-border" />
                </div>
              </div>

              {/* ─── Form card ────────────────────────────────── */}
              <div className="relative rounded-3xl border border-white/10 bg-card/70 p-6 shadow-2xl shadow-black/10 backdrop-blur-2xl dark:border-white/5">
                <div className="absolute right-5 top-5 inline-flex items-center gap-1.5 rounded-full border border-border/40 bg-background/40 px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Step 1 of 2
                </div>

                <div className="space-y-5">
                  {/* Avatar + colors row — horizontal layout, compact */}
                  <div className="flex items-center gap-5">
                    <button
                      type="button"
                      onClick={() => setShowEmojiPicker(!showEmojiPicker)}
                      className="group relative flex h-16 w-16 shrink-0 items-center justify-center rounded-2xl text-[36px] shadow-2xl transition-all duration-300 hover:scale-[1.05] focus:outline-none focus:ring-4 focus:ring-primary/30"
                      style={{
                        background: chosenColor,
                        boxShadow: `0 12px 30px -8px ${chosenColor}`,
                      }}
                      aria-label="Pick an emoji avatar"
                    >
                      {chosenEmoji}
                      <div className="absolute inset-0 flex items-center justify-center rounded-2xl bg-black/40 opacity-0 backdrop-blur-[3px] transition-opacity group-hover:opacity-100">
                        <span className="text-[9px] font-bold uppercase tracking-[0.18em] text-white">
                          Edit
                        </span>
                      </div>
                    </button>

                    <div className="flex flex-wrap gap-1.5">
                      {AVATAR_COLORS.map(({ hsl, label }) => (
                        <button
                          key={label}
                          type="button"
                          aria-label={label}
                          onClick={() => setChosenColor(hsl)}
                          className={cn(
                            "h-5 w-5 rounded-full transition-all duration-200 focus:outline-none cursor-pointer",
                            chosenColor === hsl
                              ? "ring-2 ring-foreground ring-offset-2 ring-offset-card scale-110 shadow-md"
                              : "opacity-70 hover:scale-110 hover:opacity-100"
                          )}
                          style={{ background: hsl }}
                        />
                      ))}
                    </div>

                    {showEmojiPicker && (
                      <div
                        ref={emojiPickerRef}
                        className="custom-emoji-picker absolute left-6 top-24 z-50 rounded-2xl border border-border shadow-2xl"
                      >
                        <EmojiPicker
                          onEmojiClick={(e: EmojiClickData) => {
                            setChosenEmoji(e.emoji);
                            setShowEmojiPicker(false);
                          }}
                          theme={Theme.AUTO}
                          searchPlaceHolder="Search emojis..."
                          width={300}
                          height={360}
                        />
                      </div>
                    )}
                  </div>

                  <div className="h-px bg-border/50" />

                  {/* Name */}
                  <div className="space-y-1.5">
                    <label
                      htmlFor="welcome-name"
                      className="pl-0.5 text-[10.5px] font-bold uppercase tracking-widest text-muted-foreground/80"
                    >
                      Your name <span className="text-primary">*</span>
                    </label>
                    <input
                      id="welcome-name"
                      ref={nameInputRef}
                      type="text"
                      placeholder="e.g. Alex Johnson"
                      value={nameValue}
                      onChange={(e) => setNameValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") handleProfileContinue();
                      }}
                      className={cn(
                        "w-full rounded-xl border border-border/50 bg-background/60 px-4 py-2.5 text-[14px] font-medium text-foreground shadow-inner",
                        "placeholder:font-normal placeholder:text-muted-foreground/40",
                        "focus:border-primary/40 focus:bg-background/80 focus:outline-none focus:ring-2 focus:ring-primary/40",
                        "transition-all duration-200"
                      )}
                      autoFocus
                    />
                  </div>

                  {/* Job title */}
                  <div className="space-y-1.5">
                    <label
                      htmlFor="welcome-title"
                      className="pl-0.5 text-[10.5px] font-bold uppercase tracking-widest text-muted-foreground/80"
                    >
                      Role / Title{" "}
                      <span className="font-normal normal-case tracking-normal text-muted-foreground/40">
                        (optional)
                      </span>
                    </label>
                    <input
                      id="welcome-title"
                      type="text"
                      placeholder="e.g. Engineering Lead"
                      value={titleValue}
                      onChange={(e) => setTitleValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") handleProfileContinue();
                      }}
                      className={cn(
                        "w-full rounded-xl border border-border/50 bg-background/60 px-4 py-2.5 text-[14px] font-medium text-foreground shadow-inner",
                        "placeholder:font-normal placeholder:text-muted-foreground/40",
                        "focus:border-primary/40 focus:bg-background/80 focus:outline-none focus:ring-2 focus:ring-primary/40",
                        "transition-all duration-200"
                      )}
                    />
                  </div>

                  <button
                    type="button"
                    onClick={handleProfileContinue}
                    disabled={!nameValue.trim()}
                    className={cn(
                      "flex w-full items-center justify-center gap-2 rounded-xl px-5 py-3 text-[14px] font-bold tracking-wide shadow-lg",
                      "bg-gradient-to-r from-primary to-[#18759c] text-primary-foreground",
                      "transition-all duration-200 hover:-translate-y-0.5 hover:shadow-[0_10px_32px_rgba(11,79,108,0.45)]",
                      "disabled:cursor-not-allowed disabled:opacity-50 disabled:shadow-none disabled:hover:translate-y-0"
                    )}
                  >
                    Continue
                    <ChevronRight className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-1 items-center gap-10 md:grid-cols-[1fr_1.15fr] lg:gap-14">
              {/* ─── Greeting column ──────────────────────────── */}
              <div className="space-y-5">
                <div className="flex items-center gap-3">
                  <div
                    className="flex h-12 w-12 items-center justify-center rounded-2xl text-[28px] shadow-lg"
                    style={{
                      background: chosenColor,
                      boxShadow: `0 10px 26px -6px ${chosenColor}`,
                    }}
                  >
                    {chosenEmoji}
                  </div>
                  <div>
                    <p className="text-[10.5px] font-bold uppercase tracking-widest text-muted-foreground/80">
                      Welcome
                    </p>
                    <p className="text-[15px] font-semibold tracking-tight text-foreground">
                      {nameValue.split(" ")[0]}
                    </p>
                  </div>
                </div>

                <h1 className="font-heading text-[32px] font-bold leading-[1.05] tracking-tight text-transparent bg-clip-text bg-gradient-to-br from-foreground via-foreground to-foreground/55 sm:text-[40px] lg:text-[44px]">
                  Connect your
                  <br />
                  first source.
                </h1>

                <p className="max-w-md text-[14px] leading-relaxed text-muted-foreground">
                  Pick where your conversations live. Beever extracts facts,
                  decisions, and topics automatically — your wiki populates as
                  the channel syncs.
                </p>

                <div className="flex items-center gap-4 pt-1">
                  <button
                    type="button"
                    onClick={() => setStep("profile")}
                    className="flex cursor-pointer items-center gap-1 text-[12.5px] font-semibold text-muted-foreground transition-all hover:-translate-x-1 hover:text-foreground"
                  >
                    ← Back
                  </button>
                  <div className="flex gap-2">
                    <div className="h-1.5 w-2 rounded-full bg-border" />
                    <div className="h-1.5 w-10 rounded-full bg-primary shadow-[0_0_8px_rgba(11,79,108,0.5)]" />
                  </div>
                </div>
              </div>

              {/* ─── Platform grid ────────────────────────────── */}
              <div className="relative rounded-3xl border border-white/10 bg-card/70 p-5 shadow-2xl shadow-black/10 backdrop-blur-2xl dark:border-white/5">
                <div className="absolute right-5 top-5 inline-flex items-center gap-1.5 rounded-full border border-border/40 bg-background/40 px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Step 2 of 2
                </div>

                <p className="mb-4 pl-0.5 text-[10.5px] font-bold uppercase tracking-widest text-muted-foreground/80">
                  Connect a platform
                </p>

                <div className="grid grid-cols-2 gap-2.5">
                  {PLATFORMS.map(({ key, label, icon: Icon, desc }) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => onConnect(key)}
                      className={cn(
                        "custom-platform-btn group flex flex-col items-start gap-2 rounded-2xl border border-white/10 p-3.5 text-left",
                        "bg-background/40 transition-all duration-200 cursor-pointer",
                        "hover:-translate-y-0.5 hover:border-primary/40 hover:bg-background/80 hover:shadow-[0_10px_24px_rgba(0,0,0,0.10)]"
                      )}
                    >
                      <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-primary/15 bg-primary/10 text-primary shadow-sm transition-all duration-200 group-hover:scale-110 group-hover:bg-primary group-hover:text-primary-foreground">
                        <Icon className="h-4 w-4" />
                      </div>
                      <div>
                        <p className="text-[13.5px] font-bold tracking-tight text-foreground transition-colors group-hover:text-primary">
                          {label}
                        </p>
                        <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
                          {desc}
                        </p>
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
