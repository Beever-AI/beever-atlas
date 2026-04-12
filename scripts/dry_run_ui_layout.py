#!/usr/bin/env python3
"""Dry-run validation for the UI layout redesign.

Checks:
1. Sidebar nav items are correct (Home, Channels, Ask, Activity, Settings)
2. App.tsx routes include /ask and exclude /search, /graph
3. AskCore component exists and accepts channelId prop
4. AskTab is a thin wrapper around AskCore
5. ChannelWorkspace has no Ask tab but has floating button
6. Dashboard links to /ask, not /search
7. AskPage exists with channel selector
"""

import sys
from pathlib import Path

WEB_SRC = Path(__file__).resolve().parent.parent / "web" / "src"

errors: list[str] = []
checks_passed = 0


def check(description: str, condition: bool, detail: str = ""):
    global checks_passed
    if condition:
        checks_passed += 1
        print(f"  ✓ {description}")
    else:
        errors.append(f"{description}: {detail}")
        print(f"  ✗ {description} — {detail}")


def read(rel_path: str) -> str:
    p = WEB_SRC / rel_path
    if not p.exists():
        return ""
    return p.read_text()


print("\n=== UI Layout Redesign Dry Run ===\n")

# 1. Sidebar
print("[1] Sidebar nav items")
sidebar = read("components/layout/Sidebar.tsx")
check("Home icon imported", "Home," in sidebar or "Home\n" in sidebar)
check("MessageCircleQuestion imported", "MessageCircleQuestion" in sidebar)
check("No LayoutDashboard import", "LayoutDashboard" not in sidebar)
check("No Search import (standalone)", 'Search,' not in sidebar and 'Search\n' not in sidebar)
check("No Network import", "Network" not in sidebar)
check('Home nav item exists', 'label: "Home"' in sidebar)
check('Ask nav item exists', 'label: "Ask"' in sidebar)
check('No Search nav item', 'label: "Search"' not in sidebar)
check('No Graph Explorer nav item', 'label: "Graph Explorer"' not in sidebar)
check('/ask route in sidebar', 'to: "/ask"' in sidebar)

# 2. App.tsx routes
print("\n[2] App.tsx routes")
app = read("App.tsx")
check("AskPage imported", "AskPage" in app)
check("No SearchPage import", "SearchPage" not in app)
check("No GraphExplorer import", "GraphExplorer" not in app)
check("No AskTab import", "AskTab" not in app)
check('/ask route exists', '"/ask"' in app)
check('No /search route', 'path="/search"' not in app)
check('No /graph route', 'path="/graph"' not in app)
check('No ask sub-route in channels', 'path="ask"' not in app)

# 3. AskCore component
print("\n[3] AskCore component")
ask_core = read("components/channel/AskCore.tsx")
check("AskCore file exists", len(ask_core) > 0, "File not found")
check("Accepts channelId prop", "channelId" in ask_core)
check("Accepts initialQuery prop", "initialQuery" in ask_core)
check("Uses useAsk hook", "useAsk" in ask_core)
check("No inner ConversationSidebar", "ConversationSidebar" not in ask_core)
check("Uses ChatMessageList", "ChatMessageList" in ask_core)
check("Uses ChatInputBar", "ChatInputBar" in ask_core)
check("No useParams", "useParams" not in ask_core)

# 4. AskTab thin wrapper
print("\n[4] AskTab thin wrapper")
ask_tab = read("components/channel/AskTab.tsx")
check("AskTab file exists", len(ask_tab) > 0, "File not found")
check("Imports AskCore", "AskCore" in ask_tab)
check("Uses useParams", "useParams" in ask_tab)
check("Is thin (< 15 lines)", ask_tab.strip().count("\n") < 15, f"Has {ask_tab.strip().count(chr(10))} lines")

# 5. ChannelWorkspace
print("\n[5] ChannelWorkspace")
cw = read("pages/ChannelWorkspace.tsx")
check('No "ask" in TAB_PATHS', '"ask"' not in cw.split("TAB_LABELS")[0] if "TAB_LABELS" in cw else True)
check("Floating ask button exists", "Ask about this channel" in cw)
check("Routes to /ask?context=", "/ask?context=" in cw)
check("MessageCircleQuestion imported", "MessageCircleQuestion" in cw)

# 6. Dashboard
print("\n[6] Dashboard")
dashboard = read("pages/Dashboard.tsx")
check('Ask bar links to /ask', 'to="/ask"' in dashboard)
check('No link to /search', 'to="/search"' not in dashboard)
check('Suggestion pills use /ask', '/ask?q=' in dashboard)
check('No /search?q= references', '/search?q=' not in dashboard)

# 7. AskPage
print("\n[7] AskPage")
ask_page = read("pages/AskPage.tsx")
check("AskPage file exists", len(ask_page) > 0, "File not found")
check("Imports AskCore", "AskCore" in ask_page)
check("Reads context from URL", '"context"' in ask_page)
check("Reads q from URL", '"q"' in ask_page)
check("Channel selector exists", "Select a channel" in ask_page or "channel" in ask_page.lower())
check("Fetches channels from API", "/api/channels" in ask_page)

# 8. Sidebar conversation history
print("\n[8] Sidebar conversation history")
ask_context = read("contexts/AskSessionsContext.tsx")
check("AskSessionsContext file exists", len(ask_context) > 0, "File not found")
check("Exports AskSessionsProvider", "AskSessionsProvider" in ask_context)
check("Exports useAskSessions", "useAskSessions" in ask_context)
check("Has isActive flag", "isActive" in ask_context)
check(
    "Uses a conversation history hook",
    "useConversationHistory" in ask_context or "useGlobalConversationHistory" in ask_context,
)

sidebar_conv = read("components/layout/SidebarConversationList.tsx")
check("SidebarConversationList file exists", len(sidebar_conv) > 0, "File not found")
check("Consumes useAskSessions", "useAskSessions" in sidebar_conv)
check("Has New chat button", "New chat" in sidebar_conv)
check(
    "Has search input",
    "Search chats" in sidebar_conv or "Search conversations" in sidebar_conv,
)
check("Uses ConversationItem", "ConversationItem" in sidebar_conv)

check("Sidebar imports SidebarConversationList", "SidebarConversationList" in sidebar)
check("Sidebar imports useAskSessions", "useAskSessions" in sidebar)
check("Sidebar has conditional rendering", "isAskActive" in sidebar)
check(
    "Sidebar has ask-active conversation section",
    "SidebarConversationList" in sidebar and "isAskActive" in sidebar,
)

check("App.tsx wraps with AskSessionsProvider", "AskSessionsProvider" in app)
check("AskPage uses useAskSessions", "useAskSessions" in ask_page)
check("AskCore uses useAskSessions", "useAskSessions" in ask_core)
check("AskCore supports channelMode prop", "channelMode" in ask_core)
check("AskCore uses useAskSession (v2 hook)", "useAskSession" in ask_core)

# Summary
print(f"\n{'=' * 40}")
print(f"  Passed: {checks_passed}")
print(f"  Failed: {len(errors)}")

if errors:
    print("\n  FAILURES:")
    for e in errors:
        print(f"    ✗ {e}")
    sys.exit(1)
else:
    print("\n  All checks passed! ✓")
    sys.exit(0)
