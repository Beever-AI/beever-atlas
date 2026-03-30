import { RefreshCw, Clock } from "lucide-react";

// TODO: Replace mock data with real wiki API data
const MOCK_OVERVIEW = `This channel covers backend infrastructure planning, API design decisions, and team coordination for the Atlas platform. Key themes include authentication service refactoring, database migration timelines, and cross-team dependencies.`;

// TODO: Replace with real topics from wiki API
const MOCK_TOPICS = [
  {
    id: "auth",
    title: "Authentication & Authorization",
    description: "Discussions around OAuth 2.0 implementation, token refresh strategies, and role-based access control design.",
    factCount: 14,
    updatedAt: "2024-01-15",
  },
  {
    id: "infra",
    title: "Infrastructure & DevOps",
    description: "Deployment pipelines, Kubernetes cluster configuration, monitoring setup, and incident response procedures.",
    factCount: 9,
    updatedAt: "2024-01-12",
  },
  {
    id: "api",
    title: "API Design & Contracts",
    description: "REST and GraphQL endpoint decisions, versioning strategy, rate limiting policies, and SDK generation.",
    factCount: 21,
    updatedAt: "2024-01-14",
  },
];

export function WikiTab() {
  const lastUpdated = new Date("2024-01-15");
  const daysOld = Math.max(
    0,
    Math.floor((Date.now() - lastUpdated.getTime()) / (1000 * 60 * 60 * 24))
  );
  const isStale = daysOld > 14;

  return (
    <div className="p-4 sm:p-6 space-y-5 animate-fade-in max-w-5xl mx-auto overflow-hidden">
      {/* Wiki status */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between px-4 py-3 bg-card border border-border rounded-xl">
        <div className="flex items-start gap-3">
          <span
            className={`mt-1 inline-block h-2 w-2 rounded-full shrink-0 ${
              isStale ? "bg-amber-500" : "bg-emerald-500"
            }`}
          />
          <div>
            <p className="text-sm font-medium text-foreground">
              {isStale ? "Wiki needs refresh" : "Wiki is up to date"}
            </p>
            <p className="text-sm text-muted-foreground">
              Last sync {lastUpdated.toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
                year: "numeric",
              })}
              {isStale ? ` (${daysOld} days ago)` : ""}
            </p>
          </div>
        </div>
        <button className="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg border border-border text-sm font-medium text-foreground hover:bg-muted transition-colors shrink-0">
          <RefreshCw size={13} />
          Refresh Wiki
        </button>
      </div>

      {/* Overview card */}
      <div className="bg-primary/5 border border-primary/20 rounded-2xl p-4 sm:p-6">
        <h2 className="font-heading text-lg sm:text-[22px] text-foreground mb-2 sm:mb-3">Overview</h2>
        <p className="text-sm text-muted-foreground leading-relaxed">{MOCK_OVERVIEW}</p>
        <div className="flex items-center gap-1.5 mt-4 text-sm text-muted-foreground">
          <Clock size={11} />
          <span>
            Last updated{" "}
            {lastUpdated.toLocaleDateString(undefined, {
              month: "long",
              day: "numeric",
              year: "numeric",
            })}
          </span>
        </div>
      </div>

      {/* Topics section */}
      <div>
        <h2 className="font-heading text-lg sm:text-[22px] text-foreground mb-3 sm:mb-4">Topics</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 sm:gap-4">
          {MOCK_TOPICS.map((topic) => (
            <div
              key={topic.id}
              className="bg-card rounded-2xl border border-border p-4 sm:p-5 hover:shadow-sm transition-shadow duration-200 overflow-hidden"
            >
              <h3 className="text-base font-medium text-foreground mb-2 leading-snug">
                {topic.title}
              </h3>
              <p className="text-sm text-muted-foreground leading-relaxed mb-3">
                {topic.description}
              </p>
              <div className="flex items-center justify-between">
                <span className="text-primary text-sm font-medium">
                  {topic.factCount} facts
                </span>
                <span className="text-muted-foreground text-sm">
                  {new Date(topic.updatedAt).toLocaleDateString(undefined, {
                    month: "short",
                    day: "numeric",
                  })}
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
