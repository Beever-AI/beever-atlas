import { useState, useCallback } from "react";
import type { FeedbackRating } from "../types/askTypes";
import { authFetch } from "../lib/api";

const API_BASE = import.meta.env.VITE_API_URL ?? "";

interface FeedbackEntry {
  rating: FeedbackRating;
  comment?: string;
}

export function useFeedback(channelId: string) {
  // Track feedback per message_id
  const [feedbackMap, setFeedbackMap] = useState<Record<string, FeedbackEntry>>({});

  const submitFeedback = useCallback(async (
    sessionId: string,
    messageId: string,
    rating: FeedbackRating,
    comment?: string,
  ) => {
    // Optimistic update
    setFeedbackMap(prev => ({
      ...prev,
      [messageId]: { rating, comment },
    }));

    // Route to v2 endpoint when no channel (global Ask page), v1 otherwise
    const url = channelId
      ? `${API_BASE}/api/channels/${channelId}/ask/feedback`
      : `${API_BASE}/api/ask/feedback`;

    try {
      await authFetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          message_id: messageId,
          rating,
          comment,
        }),
      });
    } catch (err) {
      console.error("Failed to submit feedback", err);
      // Revert optimistic update on failure
      setFeedbackMap(prev => {
        const updated = { ...prev };
        delete updated[messageId];
        return updated;
      });
    }
  }, [channelId]);

  const getFeedback = useCallback((messageId: string): FeedbackEntry | undefined => {
    return feedbackMap[messageId];
  }, [feedbackMap]);

  return { submitFeedback, getFeedback, feedbackMap, setFeedbackMap };
}
