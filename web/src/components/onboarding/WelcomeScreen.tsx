import { useState, useRef, useEffect } from "react";
import { MessageSquare, Monitor, Building2, Send, ChevronRight, Sparkles } from "lucide-react";
import EmojiPicker, { Theme } from "emoji-picker-react";
import type { EmojiClickData } from "emoji-picker-react";
import { cn } from "@/lib/utils";
import { useUserProfile, AVATAR_COLORS } from "@/hooks/useUserProfile";

interface WelcomeScreenProps {
  onConnect: (platform: string) => void;
}

const PLATFORMS: { key: string; label: string; icon: React.ComponentType<{ className?: string }>; desc: string }[] = [
  { key: "slack", label: "Slack", icon: MessageSquare, desc: "Connect a Slack workspace" },
  { key: "discord", label: "Discord", icon: Monitor, desc: "Connect a Discord server" },
  { key: "teams", label: "Teams", icon: Building2, desc: "Connect a Teams tenant" },
  { key: "telegram", label: "Telegram", icon: Send, desc: "Connect a Telegram bot" },
];

export function WelcomeScreen({ onConnect }: WelcomeScreenProps) {
  const { profile, saveProfile } = useUserProfile();
  const [step, setStep] = useState<"profile" | "connect">("profile");
  const [nameValue, setNameValue] = useState(profile.displayName || "");
  const [titleValue, setTitleValue] = useState(profile.jobTitle || "");
  const [chosenColor, setChosenColor] = useState(
    profile.avatarColor || AVATAR_COLORS[0].hsl
  );
  const [chosenEmoji, setChosenEmoji] = useState(
    profile.avatarEmoji || "🦫"
  );
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
    <div className="relative h-full overflow-y-auto bg-background">
      {/* Dynamic Background — sticky to the viewport while the content scrolls */}
      <div className="sticky top-0 left-0 right-0 h-0 z-0 pointer-events-none">
        <div className="absolute inset-x-0 top-0 h-screen overflow-hidden">
          <div className="absolute top-[-20%] left-[-10%] w-[50%] h-[50%] bg-primary/20 rounded-full blur-[120px] animate-pulse" style={{ animationDuration: '8s' }} />
          <div className="absolute bottom-[-20%] right-[-10%] w-[60%] h-[60%] bg-blue-500/10 rounded-full blur-[140px] animate-pulse" style={{ animationDuration: '10s' }} />
          <div className="absolute top-[20%] right-[10%] w-[40%] h-[40%] bg-teal-500/10 rounded-full blur-[100px] animate-pulse" style={{ animationDuration: '12s' }} />
        </div>
      </div>

      <div className="relative z-10 min-h-full flex items-center justify-center p-6 sm:p-8">
       <div className="w-full max-w-lg">
        {step === "profile" ? (
          <div className="animate-fade-in">
            {/* Spark badge */}
            <div className="flex justify-center mb-8">
              <span className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-full bg-primary/10 border border-primary/20 shadow-[0_0_15px_rgba(11,79,108,0.2)] text-primary text-xs font-bold tracking-wider uppercase backdrop-blur-md">
                <Sparkles className="w-3.5 h-3.5" />
                Welcome to Beever Atlas
              </span>
            </div>

            {/* Headline */}
            <div className="text-center mb-8">
              <h1 className="font-heading text-[38px] leading-tight tracking-tight text-transparent bg-clip-text bg-gradient-to-br from-foreground to-foreground/70">
                Let's set up your profile
              </h1>
              <p className="mt-3 text-base text-muted-foreground max-w-sm mx-auto leading-relaxed">
                This helps Beever personalize your experience across your team's knowledge.
              </p>
            </div>

            {/* Profile form */}
            <div className="rounded-[24px] border border-white/10 dark:border-white/5 bg-card/60 backdrop-blur-2xl p-8 space-y-7 shadow-2xl shadow-black/10">
              {/* Avatar preview */}
              <div className="flex flex-col items-center gap-5 relative">
                <button
                  type="button"
                  onClick={() => setShowEmojiPicker(!showEmojiPicker)}
                  className="w-24 h-24 rounded-[28px] flex items-center justify-center text-5xl shadow-[0_8px_30px_rgb(0,0,0,0.12)] transition-all duration-300 hover:scale-105 hover:ring-4 hover:ring-primary/30 hover:shadow-[0_8px_30px_rgba(11,79,108,0.3)] relative group cursor-pointer"
                  style={{ background: chosenColor }}
                >
                  {chosenEmoji}
                  <div className="absolute inset-0 bg-black/30 opacity-0 group-hover:opacity-100 transition-opacity rounded-[28px] flex items-center justify-center backdrop-blur-[2px]">
                    <span className="text-white text-[11px] font-bold tracking-widest uppercase">Change</span>
                  </div>
                </button>

                {showEmojiPicker && (
                  <div ref={emojiPickerRef} className="absolute top-28 z-50 shadow-2xl rounded-2xl custom-emoji-picker border border-border">
                    <EmojiPicker
                      onEmojiClick={(e: EmojiClickData) => {
                        setChosenEmoji(e.emoji);
                        setShowEmojiPicker(false);
                      }}
                      theme={Theme.AUTO}
                      searchPlaceHolder="Search emojis..."
                      width={320}
                      height={400}
                    />
                  </div>
                )}

                {/* Color picker */}
                <div className="grid grid-cols-6 gap-3 max-w-[16rem] justify-center mx-auto">
                  {AVATAR_COLORS.map(({ hsl, label }) => (
                    <button
                      key={label}
                      type="button"
                      aria-label={label}
                      onClick={() => setChosenColor(hsl)}
                      className={cn(
                        "w-8 h-8 rounded-full transition-all duration-200 ring-offset-2 ring-offset-card/0 justify-self-center shadow-inner cursor-pointer",
                        chosenColor === hsl
                          ? "ring-2 ring-foreground scale-110 shadow-md"
                          : "hover:scale-110 opacity-80 hover:opacity-100"
                      )}
                      style={{ background: hsl }}
                    />
                  ))}
                </div>
              </div>

              {/* Name */}
              <div className="space-y-2">
                <label className="text[11px] font-bold uppercase tracking-widest text-muted-foreground/80 pl-1">
                  Your name <span className="text-primary">*</span>
                </label>
                <input
                  ref={nameInputRef}
                  type="text"
                  placeholder="e.g. Alex Johnson"
                  value={nameValue}
                  onChange={(e) => setNameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleProfileContinue();
                  }}
                  className={cn(
                    "w-full px-5 py-3.5 rounded-2xl border border-border/50 bg-background/50 text-[15px] text-foreground font-medium",
                    "placeholder:text-muted-foreground/40 placeholder:font-normal focus:outline-none focus:ring-2 focus:ring-primary/40 focus:border-primary/40 focus:bg-background/80 shadow-inner",
                    "transition-all duration-200"
                  )}
                />
              </div>

              {/* Job title */}
              <div className="space-y-2">
                <label className="text-[11px] font-bold uppercase tracking-widest text-muted-foreground/80 pl-1">
                  Role / Title <span className="text-muted-foreground/40 font-normal normal-case tracking-normal">(optional)</span>
                </label>
                <input
                  type="text"
                  placeholder="e.g. Engineering Lead"
                  value={titleValue}
                  onChange={(e) => setTitleValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleProfileContinue();
                  }}
                  className={cn(
                    "w-full px-5 py-3.5 rounded-2xl border border-border/50 bg-background/50 text-[15px] text-foreground font-medium",
                    "placeholder:text-muted-foreground/40 placeholder:font-normal focus:outline-none focus:ring-2 focus:ring-primary/40 focus:border-primary/40 focus:bg-background/80 shadow-inner",
                    "transition-all duration-200"
                  )}
                />
              </div>

              <div className="pt-2">
                <button
                  type="button"
                  onClick={handleProfileContinue}
                  disabled={!nameValue.trim()}
                  className={cn(
                    "w-full flex items-center justify-center gap-2 px-6 py-4 rounded-2xl text-[15px] font-bold tracking-wide shadow-lg",
                    "bg-gradient-to-r from-primary to-[#18759c] text-primary-foreground",
                    "hover:shadow-[0_8px_30px_rgba(11,79,108,0.4)] hover:-translate-y-0.5 transition-all duration-200",
                    "disabled:opacity-50 disabled:shadow-none disabled:hover:translate-y-0 disabled:cursor-not-allowed"
                  )}
                >
                  Continue
                  <ChevronRight className="w-5 h-5" />
                </button>
              </div>
            </div>

            {/* Step indicator */}
            <div className="flex justify-center gap-2 mt-8">
              <div className="w-8 h-1.5 rounded-full bg-primary shadow-[0_0_8px_rgba(11,79,108,0.5)]" />
              <div className="w-2 h-1.5 rounded-full bg-border" />
            </div>
          </div>
        ) : (
          <div className="animate-fade-in">
            {/* Personal greeting */}
            <div className="text-center mb-8">
              <div className="flex justify-center mb-6">
                <div
                  className="w-16 h-16 rounded-[20px] flex items-center justify-center text-4xl shadow-xl border border-white/10"
                  style={{ background: chosenColor }}
                >
                  {chosenEmoji}
                </div>
              </div>
              <h1 className="font-heading text-[34px] leading-tight tracking-tight text-transparent bg-clip-text bg-gradient-to-br from-foreground to-foreground/70">
                Nice to meet you, {nameValue.split(" ")[0]}!
              </h1>
              <p className="mt-3 text-base text-muted-foreground max-w-sm mx-auto leading-relaxed">
                Now connect a platform to start building your team's knowledge graph.
              </p>
            </div>

            {/* How it works — compact (glassmorphic) */}
            <div className="rounded-[20px] border border-white/5 bg-card/40 backdrop-blur-xl px-7 py-6 mb-6 shadow-lg shadow-black/5">
              <h2 className="font-heading text-[12px] font-bold uppercase tracking-widest text-muted-foreground/70 mb-5">
                How it works
              </h2>
              <div className="space-y-4">
                {[
                  { num: 1, text: "Connect your workspace and pick channels to track." },
                  { num: 2, text: "Beever extracts facts, decisions, and topics automatically." },
                  { num: 3, text: "Search, explore, and ask questions across all your conversations." },
                ].map(({ num, text }) => (
                  <div key={num} className="flex items-start gap-4">
                    <div className="flex h-7 w-7 items-center justify-center rounded-full bg-primary/10 border border-primary/20 text-primary text-[12px] font-bold shrink-0 mt-0.5 shadow-sm">
                      {num}
                    </div>
                    <p className="text-[14.5px] text-muted-foreground leading-relaxed pt-0.5">{text}</p>
                  </div>
                ))}
              </div>
            </div>

            {/* Platform grid */}
            <div className="rounded-[24px] border border-white/10 dark:border-white/5 bg-card/60 backdrop-blur-2xl p-7 shadow-2xl shadow-black/10">
              <h2 className="font-heading text-[12px] font-bold uppercase tracking-widest text-muted-foreground/70 mb-5">
                Connect a platform
              </h2>
              <div className="grid grid-cols-2 gap-3">
                {PLATFORMS.map(({ key, label, icon: Icon, desc }) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => onConnect(key)}
                    className={cn(
                      "flex flex-col items-start gap-3 p-5 rounded-2xl border border-white/10 custom-platform-btn",
                      "bg-background/40 hover:bg-background/80 hover:border-primary/40 hover:shadow-[0_8px_20px_rgba(0,0,0,0.08)]",
                      "text-left transition-all duration-200 group cursor-pointer"
                    )}
                  >
                    <div className="w-10 h-10 rounded-[14px] bg-primary/10 border border-primary/10 text-primary flex items-center justify-center group-hover:bg-primary group-hover:text-primary-foreground group-hover:scale-110 transition-all duration-200 shadow-sm">
                      <Icon className="w-5 h-5" />
                    </div>
                    <div>
                      <p className="text-[15px] font-bold text-foreground tracking-tight group-hover:text-primary transition-colors">{label}</p>
                      <p className="text-[12.5px] text-muted-foreground leading-snug mt-1">{desc}</p>
                    </div>
                  </button>
                ))}
              </div>
            </div>

            {/* Back + step indicator */}
            <div className="flex items-center justify-between mt-8">
              <button
                type="button"
                onClick={() => setStep("profile")}
                className="text-[13px] font-semibold text-muted-foreground hover:text-foreground hover:-translate-x-1 transition-all flex items-center gap-1 cursor-pointer"
              >
                ← Back
              </button>
              <div className="flex gap-2">
                <div className="w-2 h-1.5 rounded-full bg-border" />
                <div className="w-8 h-1.5 rounded-full bg-primary shadow-[0_0_8px_rgba(11,79,108,0.5)]" />
              </div>
            </div>
          </div>
        )}
       </div>
      </div>
    </div>
  );
}
