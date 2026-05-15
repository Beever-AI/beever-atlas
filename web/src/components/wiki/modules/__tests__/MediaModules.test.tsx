/**
 * Smoke tests for the 6 frontend-only media renderers.
 *
 * Verifies:
 *  - Each component mounts without crashing
 *  - Empty/missing data gracefully renders nothing (returns null)
 *  - Realistic synthetic data produces the expected DOM shape
 *  - URLs render as expected (img src, iframe embed transformation,
 *    pdf link, etc.)
 *
 * Privacy guarantee — every fixture uses synthetic / fictional data:
 *  example.com / example.org URLs only, placeholder author names,
 *  no real channel content referenced.
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { MediaHeroModule } from "../MediaHeroModule";
import { MediaInlineModule } from "../MediaInlineModule";
import { MediaGalleryModule } from "../MediaGalleryModule";
import { LinkCardModule } from "../LinkCardModule";
import { PdfPreviewModule } from "../PdfPreviewModule";
import { VideoEmbedModule } from "../VideoEmbedModule";
import type { WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

function makeModule(id: string, data: Record<string, unknown>): WikiPageModule {
  return { id, anchor: `${id}-anchor`, data };
}

const noop = () => undefined;

// ---------------------------------------------------------------------------
// MediaHeroModule
// ---------------------------------------------------------------------------

describe("MediaHeroModule", () => {
  it("renders an image hero with caption + attribution", () => {
    render(
      <MediaHeroModule
        module={makeModule("media_hero", {
          url: "https://example.com/dashboard.png",
          alt: "Dashboard screenshot",
          caption: "Auth dashboard at 50K req/min.",
          source_author: "Alice",
          source_date: "2026-01-20",
          kind: "image",
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const img = screen.getByRole("img");
    expect(img).toHaveAttribute("src", "https://example.com/dashboard.png");
    expect(img).toHaveAttribute("alt", "Dashboard screenshot");
    expect(screen.getByText(/Auth dashboard/)).toBeInTheDocument();
    expect(screen.getByText(/Alice · 2026-01-20/)).toBeInTheDocument();
  });

  it("renders nothing when url is missing", () => {
    const { container } = render(
      <MediaHeroModule
        module={makeModule("media_hero", { url: "", alt: "x" })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders a <video> element when kind=video", () => {
    const { container } = render(
      <MediaHeroModule
        module={makeModule("media_hero", {
          url: "https://example.com/demo.mp4",
          alt: "Demo video",
          kind: "video",
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.querySelector("video")).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// MediaInlineModule
// ---------------------------------------------------------------------------

describe("MediaInlineModule", () => {
  it("renders multiple inline images with captions", () => {
    render(
      <MediaInlineModule
        module={makeModule("media_inline", {
          items: [
            {
              media_id: "m1",
              url: "https://example.com/screenshot-1.png",
              alt: "Screenshot 1",
              caption: "Before migration",
            },
            {
              media_id: "m2",
              url: "https://example.com/screenshot-2.png",
              alt: "Screenshot 2",
              caption: "After migration",
            },
          ],
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByAltText("Screenshot 1")).toBeInTheDocument();
    expect(screen.getByAltText("Screenshot 2")).toBeInTheDocument();
    expect(screen.getByText("Before migration")).toBeInTheDocument();
    expect(screen.getByText("After migration")).toBeInTheDocument();
  });

  it("renders nothing when items is empty", () => {
    const { container } = render(
      <MediaInlineModule
        module={makeModule("media_inline", { items: [] })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// MediaGalleryModule
// ---------------------------------------------------------------------------

describe("MediaGalleryModule", () => {
  it("renders a grid of images with the count badge", () => {
    render(
      <MediaGalleryModule
        module={makeModule("media_gallery", {
          label: "Gallery",
          items: [
            { url: "https://example.com/g1.png", alt: "Gallery 1" },
            { url: "https://example.com/g2.png", alt: "Gallery 2" },
            { url: "https://example.com/g3.png", alt: "Gallery 3" },
          ],
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText(/Gallery/)).toBeInTheDocument();
    expect(screen.getByText(/\(3\)/)).toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(3);
  });

  it("renders nothing when items is empty", () => {
    const { container } = render(
      <MediaGalleryModule
        module={makeModule("media_gallery", { items: [] })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// LinkCardModule
// ---------------------------------------------------------------------------

describe("LinkCardModule", () => {
  it("renders link cards with title + description", () => {
    render(
      <LinkCardModule
        module={makeModule("link_card", {
          items: [
            {
              url: "https://example.org/jwt-best-practices",
              title: "JWT best practices",
              description: "External reference doc on JWT rotation.",
            },
          ],
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const link = screen.getByRole("link", { name: /JWT best practices/ });
    expect(link).toHaveAttribute("href", "https://example.org/jwt-best-practices");
    expect(link).toHaveAttribute("target", "_blank");
    expect(screen.getByText(/JWT rotation/)).toBeInTheDocument();
  });

  it("renders nothing when items is empty", () => {
    const { container } = render(
      <LinkCardModule
        module={makeModule("link_card", { items: [] })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// PdfPreviewModule
// ---------------------------------------------------------------------------

describe("PdfPreviewModule", () => {
  it("renders PDF cards with title", () => {
    render(
      <PdfPreviewModule
        module={makeModule("pdf_preview", {
          items: [
            {
              url: "https://example.org/security-review.pdf",
              title: "Security Review — Q1 2026",
            },
          ],
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "https://example.org/security-review.pdf");
    expect(screen.getByText("Security Review — Q1 2026")).toBeInTheDocument();
  });

  it("derives title from URL when not provided", () => {
    render(
      <PdfPreviewModule
        module={makeModule("pdf_preview", {
          items: [{ url: "https://example.org/spec.pdf" }],
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText("spec.pdf")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// VideoEmbedModule
// ---------------------------------------------------------------------------

describe("VideoEmbedModule", () => {
  it("transforms a YouTube watch URL into an embed iframe", () => {
    const { container } = render(
      <VideoEmbedModule
        module={makeModule("video_embed", {
          items: [
            {
              url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
              kind: "youtube",
              title: "Synthetic demo",
            },
          ],
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const iframe = container.querySelector("iframe");
    expect(iframe).not.toBeNull();
    expect(iframe?.getAttribute("src")).toBe(
      "https://www.youtube.com/embed/dQw4w9WgXcQ",
    );
  });

  it("renders a native <video> element for direct .mp4 URLs", () => {
    const { container } = render(
      <VideoEmbedModule
        module={makeModule("video_embed", {
          items: [
            { url: "https://example.com/demo.mp4", kind: "native" },
          ],
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.querySelector("video")).not.toBeNull();
  });

  it("renders nothing when items is empty", () => {
    const { container } = render(
      <VideoEmbedModule
        module={makeModule("video_embed", { items: [] })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});
